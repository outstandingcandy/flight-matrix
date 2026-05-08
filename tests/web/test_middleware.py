"""Tests for src.web.middleware."""

from __future__ import annotations

import time

import pytest

from src.web.middleware import CustomDomainMiddleware, TTLCache


class TestTTLCache:
    def test_miss_on_empty(self) -> None:
        cache = TTLCache()
        value, hit = cache.get("nope")
        assert value is None
        assert hit is False

    def test_hit_after_set(self) -> None:
        cache = TTLCache()
        cache.set("k", "v", ttl_seconds=60)
        value, hit = cache.get("k")
        assert value == "v"
        assert hit is True

    def test_expired_entry_is_purged(self) -> None:
        cache = TTLCache()
        cache.set("k", "v", ttl_seconds=0)
        time.sleep(0.01)
        value, hit = cache.get("k")
        assert value is None
        assert hit is False
        # The expired entry should also be removed.
        assert "k" not in cache._cache

    def test_overwrite_replaces_value_and_ttl(self) -> None:
        cache = TTLCache()
        cache.set("k", "v1", ttl_seconds=60)
        cache.set("k", "v2", ttl_seconds=60)
        value, hit = cache.get("k")
        assert value == "v2"
        assert hit is True

    def test_independent_keys(self) -> None:
        cache = TTLCache()
        cache.set("a", 1)
        cache.set("b", 2)
        assert cache.get("a") == (1, True)
        assert cache.get("b") == (2, True)
        assert cache.get("c") == (None, False)


class TestCustomDomainMiddleware:
    def test_no_domain_is_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APP_DOMAIN", raising=False)

        received: dict = {}

        def wsgi_app(environ, start_response):
            received.update(environ)
            return []

        mw = CustomDomainMiddleware(wsgi_app)
        environ: dict = {"HTTP_HOST": "original.example"}
        mw(environ, lambda *_: None)
        assert "HTTP_X_FORWARDED_HOST" not in received
        assert "HTTP_X_FORWARDED_PROTO" not in received

    def test_explicit_domain_sets_forwarded_headers(self) -> None:
        received: dict = {}

        def wsgi_app(environ, start_response):
            received.update(environ)
            return []

        mw = CustomDomainMiddleware(wsgi_app, domain="custom.example")
        environ: dict = {"HTTP_HOST": "original.example"}
        mw(environ, lambda *_: None)
        assert received["HTTP_X_FORWARDED_HOST"] == "custom.example"
        assert received["HTTP_X_FORWARDED_PROTO"] == "https"

    def test_env_var_supplies_domain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_DOMAIN", "from-env.example")

        received: dict = {}

        def wsgi_app(environ, start_response):
            received.update(environ)
            return []

        mw = CustomDomainMiddleware(wsgi_app)
        mw({}, lambda *_: None)
        assert received["HTTP_X_FORWARDED_HOST"] == "from-env.example"
