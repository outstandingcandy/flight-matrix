"""WSGI middleware and request-scoped helpers for the Flask app."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class TTLCache:
    """Tiny in-process TTL cache. Not thread-safe, but the web app is

    single-threaded per Lambda invocation, so it's fine here.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> tuple[Any, bool]:
        if key in self._cache:
            value, expiry = self._cache[key]
            if time.time() < expiry:
                return value, True
            del self._cache[key]
        return None, False

    def set(self, key: str, value: Any, ttl_seconds: int = 3600) -> None:
        self._cache[key] = (value, time.time() + ttl_seconds)


class CustomDomainMiddleware:
    """Inject X-Forwarded-Host so Flask's url_for() uses the custom domain.

    API Gateway HTTP API does not propagate the Host header when a custom
    domain is configured. Without this middleware, `url_for()` would build
    URLs against the raw API Gateway execute-api URL.
    """

    def __init__(self, app: Callable, domain: str | None = None) -> None:
        import os

        self.app = app
        self.domain = domain or os.environ.get("APP_DOMAIN", "")

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> Any:
        if self.domain:
            environ["HTTP_X_FORWARDED_HOST"] = self.domain
            environ["HTTP_X_FORWARDED_PROTO"] = "https"
        return self.app(environ, start_response)
