"""OpenSearch connection settings and client construction.

This module owns the connection to the project's *own* OpenSearch cluster,
which backs the aircraft full-text index (:mod:`src.search.aircraft_index`).
It is unrelated to :mod:`src.search.base`, the *web* search abstraction
(Tavily and friends) the analysis pipeline uses.

The cluster is optional infrastructure. ``search.opensearch.url`` left empty
means the feature is off, and ``opensearch-py`` is imported lazily so that a
deployment which never installed it behaves exactly like one that never
configured a URL: :func:`get_client` returns ``None`` and callers fall back to
their SQL path.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.core.exceptions import SearchError

if TYPE_CHECKING:
    from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("search.opensearch")

__all__ = [
    "CONFIG_PREFIX",
    "DEFAULT_INDEX",
    "OpenSearchSettings",
    "build_client",
    "get_client",
    "reset_client",
]

CONFIG_PREFIX = "search.opensearch"
DEFAULT_INDEX = "aircraft"

_client: Any = None
_client_settings: OpenSearchSettings | None = None


class OpenSearchSettings(BaseModel):
    """Everything needed to reach the cluster.

    Attributes:
        url: Cluster URL. Empty disables the feature.
        index: Index (or alias) holding the aircraft documents.
        timeout: Per-request timeout in seconds.
        verify_certs: Whether to verify TLS certificates; only meaningful for
            ``https`` URLs.
        username: Basic-auth user, empty when the security plugin is disabled.
        password: Basic-auth password.
        max_results: Most registrations one query may return.
        batch_size: Documents per ``_bulk`` request during a reindex.
    """

    url: str = ""
    index: str = DEFAULT_INDEX
    timeout: float = Field(default=5.0, gt=0)
    verify_certs: bool = True
    username: str = ""
    password: str = ""
    max_results: int = Field(default=1000, gt=0)
    batch_size: int = Field(default=500, gt=0)

    @property
    def enabled(self) -> bool:
        """Whether a cluster is configured at all."""
        return bool(self.url.strip())

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> OpenSearchSettings:
        """Build settings from a mapping, ignoring unset keys.

        Args:
            values: Mapping of setting names to values. ``None`` and empty
                strings are dropped so that an unresolved ``${VAR}`` falls back
                to the field default rather than overriding it with nothing.

        Returns:
            Validated settings.
        """
        supplied = {
            key: value
            for key, value in (values or {}).items()
            if key in cls.model_fields and value is not None and value != ""
        }
        return cls(**supplied)

    @classmethod
    def from_config(cls, yaml_config: YAMLConfig) -> OpenSearchSettings:
        """Build settings from ``search.opensearch`` in the YAML config.

        Each leaf is read individually because :meth:`YAMLConfig.get` only
        interpolates ``${VAR}`` for string leaves — asking for the whole block
        would hand back the literal placeholders.

        Args:
            yaml_config: Configuration manager.

        Returns:
            Validated settings.
        """
        return cls.from_mapping(
            {name: yaml_config.get(f"{CONFIG_PREFIX}.{name}") for name in cls.model_fields}
        )


def build_client(settings: OpenSearchSettings) -> Any:
    """Create an OpenSearch client.

    Constructing a client does not connect; failures surface on the first
    request instead.

    Args:
        settings: Connection settings.

    Returns:
        An ``opensearchpy.OpenSearch`` instance.

    Raises:
        SearchError: If no URL is configured or ``opensearch-py`` is missing.
    """
    if not settings.enabled:
        raise SearchError("OpenSearch is not configured (search.opensearch.url is empty)")

    try:
        from opensearchpy import OpenSearch
    except ImportError as e:  # pragma: no cover - depends on the installed env
        raise SearchError(
            "opensearch-py is not installed; run `uv sync` to enable aircraft search"
        ) from e

    auth = (settings.username, settings.password) if settings.username else None
    return OpenSearch(
        hosts=[settings.url.strip()],
        http_auth=auth,
        use_ssl=settings.url.strip().startswith("https://"),
        verify_certs=settings.verify_certs,
        ssl_show_warn=False,
        timeout=settings.timeout,
        # One retry only: the caller has a SQL fallback and an admin waiting on
        # a page render, so a slow cluster must not become a slow endpoint.
        max_retries=1,
        retry_on_timeout=True,
    )


def get_client(settings: OpenSearchSettings | None = None) -> Any:
    """Return the process-wide client, creating it on demand.

    Args:
        settings: Settings used on first call. When omitted they are read from
            a freshly constructed :class:`~src.utils.yaml_config.YAMLConfig`.

    Returns:
        The shared client, or ``None`` when OpenSearch is unconfigured or
        unavailable. Returning ``None`` rather than raising is deliberate:
        every caller has a SQL path, and search is not worth a 500.
    """
    global _client, _client_settings

    if settings is None:
        if _client is not None:
            return _client
        from src.utils.yaml_config import YAMLConfig

        settings = OpenSearchSettings.from_config(YAMLConfig())

    if _client is not None and _client_settings == settings:
        return _client

    if not settings.enabled:
        return None

    try:
        _client = build_client(settings)
    except SearchError as e:
        logger.warning("Aircraft search disabled: %s", e)
        return None

    _client_settings = settings
    logger.info("OpenSearch client initialised (url=%s, index=%s)", settings.url, settings.index)
    return _client


def reset_client() -> None:
    """Discard the cached client so the next call re-reads configuration.

    Intended for tests, and for long-running processes reloading config.
    """
    global _client, _client_settings
    _client = None
    _client_settings = None
