"""In-process HTTPS fixture adapted from Scrapy PR #6546 mockserver helpers.

The upstream tests launch ``SimpleMockServer`` in a child process.  Candidate
tests are not allowed to create subprocesses, so this keeps the same TLS
version arguments and response contract while serving the fixture in the
Twisted reactor thread.
"""

from __future__ import annotations

import asyncio
import ipaddress
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from OpenSSL.crypto import FILETYPE_PEM, load_certificate, load_privatekey
from twisted.internet import asyncioreactor
from twisted.internet.ssl import CertificateOptions
from twisted.web.resource import Resource
from twisted.web.server import Site

from scrapy.core.downloader.tls import _TWISTED_VERSION_MAP
from scrapy.utils.defer import deferred_to_future
from scrapy.utils.ssl import _get_cert_options_version_kwargs


_REACTOR_LOOP = asyncio.new_event_loop()
try:
    asyncioreactor.install(_REACTOR_LOOP)
except Exception:  # pragma: no cover - another candidate module installed it first
    pass

from twisted.internet import reactor  # noqa: E402


_REACTOR_READY = threading.Event()
_REACTOR_LOCK = threading.Lock()
_REACTOR_THREAD: threading.Thread | None = None


def _run_reactor() -> None:
    asyncio.set_event_loop(_REACTOR_LOOP)
    reactor.callWhenRunning(_REACTOR_READY.set)
    reactor.run(installSignalHandlers=False)


def ensure_reactor() -> None:
    """Start one in-process reactor used by all TLS matrix cases."""

    global _REACTOR_THREAD
    with _REACTOR_LOCK:
        if _REACTOR_THREAD is None:
            _REACTOR_THREAD = threading.Thread(
                target=_run_reactor,
                name="pr6546-twisted-reactor",
                daemon=True,
            )
            _REACTOR_THREAD.start()
    _REACTOR_READY.wait(timeout=10)
    if not _REACTOR_READY.is_set():
        raise RuntimeError("Twisted reactor did not start")


def run_on_reactor(coro):
    """Run a native coroutine on the reactor's asyncio event loop."""

    ensure_reactor()
    future = asyncio.run_coroutine_threadsafe(coro, _REACTOR_LOOP)
    return future.result(timeout=120)


def write_localhost_certificate(root: Path) -> tuple[Path, Path]:
    """Create an isolated localhost certificate/key pair for one test run."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(
            datetime.now(timezone.utc) + timedelta(days=1)
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    keyfile = root / "localhost.key"
    certfile = root / "localhost.crt"
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return keyfile, certfile


def ssl_context_factory(
    keyfile: Path,
    certfile: Path,
    *,
    tls_min_version: str | None = None,
    tls_max_version: str | None = None,
) -> CertificateOptions:
    """Merge the PR's ``http_base`` TLS min/max support into an in-process server."""

    tls_min = _TWISTED_VERSION_MAP.get(tls_min_version) if tls_min_version else None
    tls_max = _TWISTED_VERSION_MAP.get(tls_max_version) if tls_max_version else None
    version_kwargs = _get_cert_options_version_kwargs(tls_min, tls_max)
    return CertificateOptions(
        privateKey=load_privatekey(FILETYPE_PEM, keyfile.read_bytes()),
        certificate=load_certificate(FILETYPE_PEM, certfile.read_bytes()),
        acceptableProtocols=[b"h2", b"http/1.1"],
        **version_kwargs,
    )


class _Root(Resource):
    isLeaf = True

    def render_GET(self, request):  # type: ignore[no-untyped-def]
        request.setHeader(b"content-type", b"text/plain")
        return b"0123456789"


@asynccontextmanager
async def simple_https_server(
    keyfile: Path,
    certfile: Path,
    *,
    tls_min_version: str | None = None,
    tls_max_version: str | None = None,
) -> AsyncIterator[str]:
    """Serve the upstream ``/file`` response without a child process."""

    listening_port = reactor.listenSSL(
        0,
        Site(_Root()),
        ssl_context_factory(
            keyfile,
            certfile,
            tls_min_version=tls_min_version,
            tls_max_version=tls_max_version,
        ),
        interface="127.0.0.1",
    )
    port = listening_port.getHost().port
    try:
        yield f"https://localhost:{port}/file"
    finally:
        await deferred_to_future(listening_port.stopListening())
