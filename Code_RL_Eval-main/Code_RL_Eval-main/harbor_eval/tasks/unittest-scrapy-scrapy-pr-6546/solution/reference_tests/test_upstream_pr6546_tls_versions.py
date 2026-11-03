"""Selected TLS min/max matrix from Scrapy PR #6546.

The upstream ``TestHttpsTLSVersionBase`` is exercised against each of the
three affected download handlers.  The only harness adaptation is the
in-process server supplied by ``pr6546_mockserver_support``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest

from scrapy import Request
from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler
from scrapy.exceptions import DownloadConnectionRefusedError, DownloadFailedError
from scrapy.core.downloader.handlers._httpx import HttpxDownloadHandler
from scrapy.utils._deps_compat import TWISTED_TLS_LIMITS_OFFBY1
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from twisted.web.http import H2_ENABLED

if H2_ENABLED:
    from scrapy.core.downloader.handlers.http2 import H2DownloadHandler
else:
    H2DownloadHandler = None

from pr6546_mockserver_support import (
    run_on_reactor,
    simple_https_server,
    write_localhost_certificate,
)


@asynccontextmanager
async def _get_download_handler(
    handler_cls,
    client_tls_min: str | None,
    client_tls_max: str | None,
) -> AsyncIterator[object]:
    settings: dict[str, str] = {}
    if client_tls_min is not None:
        settings["DOWNLOAD_TLS_MIN_VERSION"] = client_tls_min
    if client_tls_max is not None:
        settings["DOWNLOAD_TLS_MAX_VERSION"] = client_tls_max
    crawler = get_crawler(DefaultSpider, settings_dict=settings)
    crawler.spider = crawler._create_spider()
    handler = build_from_crawler(handler_cls, crawler)
    try:
        yield handler
    finally:
        await handler.close()


async def _download_case(
    keyfile,
    certfile,
    handler_cls,
    server_tls_min: str | None,
    server_tls_max: str | None,
    client_tls_min: str | None,
    client_tls_max: str | None,
    expect_success: bool,
) -> None:
    async with simple_https_server(
        keyfile,
        certfile,
        tls_min_version=server_tls_min,
        tls_max_version=server_tls_max,
    ) as url:
        request = Request(url)
        async with _get_download_handler(
            handler_cls, client_tls_min, client_tls_max
        ) as handler:
            if expect_success:
                response = await handler.download_request(request)
                assert response.body == b"0123456789"
            else:
                with pytest.raises(
                    (DownloadConnectionRefusedError, DownloadFailedError)
                ):
                    await handler.download_request(request)


@pytest.mark.parametrize(
    (
        "server_tls_min",
        "server_tls_max",
        "client_tls_min",
        "client_tls_max",
        "expect_success",
    ),
    [
        pytest.param(None, None, None, None, True, id="no-limits"),
        pytest.param(None, None, None, "TLSv1.2", True, id="client-max-tls1.2"),
        pytest.param(None, None, "TLSv1.3", None, True, id="client-min-tls1.3"),
        pytest.param(
            "TLSv1.3",
            None,
            None,
            "TLSv1.2",
            False,
            id="client-max-below-server-min",
        ),
        pytest.param(
            None,
            "TLSv1.2",
            "TLSv1.3",
            None,
            False,
            id="client-min-above-server-max",
        ),
        pytest.param(None, "TLSv1.2", None, "TLSv1.2", True, id="both-tls1.2"),
        pytest.param("TLSv1.3", None, "TLSv1.3", None, True, id="both-tls1.3"),
        pytest.param(
            None,
            None,
            "TLSv1.0",
            None,
            True,
            id="client-min-tls1.0",
            marks=pytest.mark.filterwarnings(
                r"ignore:ssl\.TLSVersion\.TLSv1 is deprecated:DeprecationWarning"
            ),
        ),
        pytest.param(
            "TLSv1.0",
            None,
            "TLSv1.0",
            None,
            True,
            id="both-min-tls1.0",
            marks=pytest.mark.filterwarnings(
                r"ignore:ssl\.TLSVersion\.TLSv1 is deprecated:DeprecationWarning"
            ),
        ),
        pytest.param(
            "TLSv1.2",
            "TLSv1.3",
            "TLSv1.2",
            "TLSv1.3",
            True,
            id="both-tls1.2-1.3",
            marks=pytest.mark.xfail(
                TWISTED_TLS_LIMITS_OFFBY1,
                reason="Can't set max to 1.3 on this Twisted version",
                strict=True,
            ),
        ),
    ],
)
@pytest.mark.parametrize(
    "handler_cls",
    [
        pytest.param(HTTP11DownloadHandler, id="twisted-http11"),
        pytest.param(HttpxDownloadHandler, id="httpx"),
        pytest.param(
            H2DownloadHandler,
            id="twisted-http2",
            marks=pytest.mark.skipif(
                not H2_ENABLED, reason="HTTP/2 support in Twisted is not enabled"
            ),
        ),
    ],
)
def test_tls_version_matrix(
    tmp_path,
    handler_cls,
    server_tls_min: str | None,
    server_tls_max: str | None,
    client_tls_min: str | None,
    client_tls_max: str | None,
    expect_success: bool,
) -> None:
    keyfile, certfile = write_localhost_certificate(tmp_path)
    run_on_reactor(
        _download_case(
            keyfile,
            certfile,
            handler_cls,
            server_tls_min,
            server_tls_max,
            client_tls_min,
            client_tls_max,
            expect_success,
        )
    )


@pytest.mark.parametrize(
    "setting_name",
    ["DOWNLOAD_TLS_MIN_VERSION", "DOWNLOAD_TLS_MAX_VERSION"],
)
def test_invalid_tls_version_setting_is_rejected(setting_name: str) -> None:
    with pytest.raises(ValueError, match=f"Unknown {setting_name} value: invalid"):
        run_on_reactor(
            _invalid_setting_case(setting_name)
        )


async def _invalid_setting_case(setting_name: str) -> None:
    settings = {setting_name: "invalid"}
    crawler = get_crawler(DefaultSpider, settings_dict=settings)
    crawler.spider = crawler._create_spider()
    build_from_crawler(HTTP11DownloadHandler, crawler)
