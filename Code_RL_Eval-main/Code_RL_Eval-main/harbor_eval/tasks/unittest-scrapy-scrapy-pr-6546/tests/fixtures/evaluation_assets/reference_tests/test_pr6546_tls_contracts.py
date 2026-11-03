from __future__ import annotations

import ssl

import pytest
from OpenSSL import SSL
from twisted.internet.ssl import TLSVersion

from scrapy.core.downloader import tls
from scrapy.core.downloader.contextfactory import (
    _load_context_factory_from_settings,
)
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.settings import Settings
from scrapy.utils._deps_compat import TWISTED_TLS_LIMITS_OFFBY1
from scrapy.utils.ssl import _get_cert_options_version_kwargs, _make_ssl_context
from scrapy.utils.test import get_crawler


def test_stdlib_context_honors_configured_tls_version_range() -> None:
    settings = Settings(
        {
            "DOWNLOAD_TLS_MIN_VERSION": "TLSv1.2",
            "DOWNLOAD_TLS_MAX_VERSION": "TLSv1.3",
        }
    )

    context = _make_ssl_context(settings)

    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    assert context.maximum_version is ssl.TLSVersion.TLSv1_3


def test_stdlib_context_preserves_default_tls_version_range() -> None:
    context = _make_ssl_context(Settings())

    assert context.protocol == ssl.PROTOCOL_TLS_CLIENT
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    assert context.maximum_version is ssl.TLSVersion.MAXIMUM_SUPPORTED


@pytest.mark.parametrize(
    ("setting_name", "expected_message"),
    [
        (
            "DOWNLOAD_TLS_MIN_VERSION",
            "Unknown DOWNLOAD_TLS_MIN_VERSION value: invalid",
        ),
        (
            "DOWNLOAD_TLS_MAX_VERSION",
            "Unknown DOWNLOAD_TLS_MAX_VERSION value: invalid",
        ),
    ],
)
def test_invalid_tls_version_setting_is_rejected(
    setting_name: str,
    expected_message: str,
) -> None:
    settings = Settings({setting_name: "invalid"})

    with pytest.raises(ValueError, match=expected_message):
        _make_ssl_context(settings)


def test_twisted_context_factory_uses_version_limits_over_legacy_method() -> None:
    crawler = get_crawler(
        settings_dict={
            "DOWNLOADER_CLIENT_TLS_METHOD": "TLSv1.2",
            "DOWNLOAD_TLS_MIN_VERSION": "TLSv1.2",
            "DOWNLOAD_TLS_MAX_VERSION": "TLSv1.3",
        }
    )

    with pytest.warns(
        ScrapyDeprecationWarning,
        match="DOWNLOADER_CLIENT_TLS_METHOD",
    ):
        context_factory = _load_context_factory_from_settings(crawler)

    assert context_factory._ssl_method is None
    assert context_factory.tls_min_version is TLSVersion.TLSv1_2
    assert context_factory.tls_max_version is TLSVersion.TLSv1_3
    kwargs = context_factory._get_cert_options_kwargs()
    assert kwargs["lowerMaximumSecurityTo"] is TLSVersion.TLSv1_3


@pytest.mark.parametrize(
    "maximum",
    [TLSVersion.TLSv1_2, TLSVersion.TLSv1_3],
    ids=["tls12", "tls13"],
)
def test_current_twisted_maximum_is_translated_directly(
    maximum: TLSVersion,
) -> None:
    assert TWISTED_TLS_LIMITS_OFFBY1 is False

    kwargs = _get_cert_options_version_kwargs(None, maximum)

    assert kwargs == {"lowerMaximumSecurityTo": maximum}


def test_legacy_method_without_new_bounds_uses_real_compatibility_path() -> None:
    crawler = get_crawler(
        settings_dict={
            "DOWNLOADER_CLIENT_TLS_METHOD": "TLSv1.2",
        }
    )

    with pytest.warns(
        ScrapyDeprecationWarning,
        match="DOWNLOADER_CLIENT_TLS_METHOD",
    ):
        context_factory = _load_context_factory_from_settings(crawler)

    assert context_factory._ssl_method == SSL.TLSv1_2_METHOD
    assert context_factory.tls_min_version is None
    assert context_factory.tls_max_version is None


def test_legacy_method_tls_export_remains_available_with_warning() -> None:
    with pytest.warns(
        ScrapyDeprecationWarning,
        match=r"scrapy\.core\.downloader\.tls\.METHOD_TLS is deprecated",
    ):
        assert tls.METHOD_TLS == "TLS"


def test_legacy_openssl_methods_export_remains_available_with_warning() -> None:
    with pytest.warns(
        ScrapyDeprecationWarning,
        match=r"scrapy\.core\.downloader\.tls\.openssl_methods is deprecated",
    ):
        assert tls.openssl_methods == {
            "TLS": SSL.SSLv23_METHOD,
            "TLSv1.0": SSL.TLSv1_METHOD,
            "TLSv1.1": SSL.TLSv1_1_METHOD,
            "TLSv1.2": SSL.TLSv1_2_METHOD,
        }
