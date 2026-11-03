"""Selected regression tests added or changed by Scrapy PR #6546."""

from __future__ import annotations

import warnings

import OpenSSL.SSL
import pytest

from scrapy.core.downloader import tls
from scrapy.core.downloader.contextfactory import (
    _ScrapyClientContextFactory,
    _load_context_factory_from_settings,
)
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.utils.test import get_crawler


def test_setting_none_warns_before_rejecting_legacy_tls_method() -> None:
    crawler = get_crawler(settings_dict={"DOWNLOADER_CLIENT_TLS_METHOD": None})
    with (
        pytest.warns(
            ScrapyDeprecationWarning,
            match="Setting DOWNLOADER_CLIENT_TLS_METHOD to a non-default value is deprecated",
        ),
        pytest.raises(KeyError),
    ):
        _load_context_factory_from_settings(crawler)


def test_setting_bad_warns_before_rejecting_legacy_tls_method() -> None:
    crawler = get_crawler(settings_dict={"DOWNLOADER_CLIENT_TLS_METHOD": "bad"})
    with (
        pytest.warns(
            ScrapyDeprecationWarning,
            match="Setting DOWNLOADER_CLIENT_TLS_METHOD to a non-default value is deprecated",
        ),
        pytest.raises(KeyError),
    ):
        _load_context_factory_from_settings(crawler)


def test_setting_explicit_warns_and_keeps_the_legacy_method() -> None:
    crawler = get_crawler(settings_dict={"DOWNLOADER_CLIENT_TLS_METHOD": "TLSv1.2"})
    with pytest.warns(
        ScrapyDeprecationWarning,
        match="Setting DOWNLOADER_CLIENT_TLS_METHOD to a non-default value is deprecated",
    ):
        client_context_factory = _load_context_factory_from_settings(crawler)
    assert client_context_factory._ssl_method == OpenSSL.SSL.TLSv1_2_METHOD


def test_direct_init_keeps_the_explicit_legacy_method() -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Passing method to twisted\.internet\.ssl\.CertificateOptions:DeprecationWarning",
            category=DeprecationWarning,
        )
        client_context_factory = _ScrapyClientContextFactory(
            OpenSSL.SSL.TLSv1_2_METHOD
        )
    assert client_context_factory._ssl_method == OpenSSL.SSL.TLSv1_2_METHOD


def test_deprecated_tls_module_names() -> None:
    with pytest.warns(
        ScrapyDeprecationWarning,
        match="scrapy.core.downloader.tls.METHOD_TLS is deprecated",
    ):
        assert tls.METHOD_TLS == "TLS"
    with pytest.warns(
        ScrapyDeprecationWarning,
        match="scrapy.core.downloader.tls.openssl_methods is deprecated",
    ):
        assert isinstance(tls.openssl_methods, dict)
