"""Aircraft full-text-search index accessor with SQL fallback.

Two-function module. :func:`aircraft_search_index` builds the
OpenSearch client from ``runtime.config`` (or returns ``None`` when
OpenSearch isn't configured / can't be reached).
:func:`with_aircraft_index` is the wrapper every index-backed handler
goes through so the fallback rule lives in one place: an admin page
must never break because search is down; if the index returns
``None`` or raises :exc:`SearchError`, the handler drops through to
its SQL path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from src.core.exceptions import SearchError
from src.search.aircraft_index import AircraftSearchIndex
from src.search.opensearch_client import OpenSearchSettings, get_client
from src.web import runtime

logger = logging.getLogger("web.search_index")

_T = TypeVar("_T")


def aircraft_search_index() -> AircraftSearchIndex | None:
    """Return the aircraft full-text index, or ``None`` if unavailable.

    Reads ``runtime.config`` for the OpenSearch endpoint /
    credentials. Missing config or a failed client construction both
    resolve to ``None`` — callers must be prepared for the SQL
    fallback either way.
    """
    settings = (
        OpenSearchSettings.from_config(runtime.config) if runtime.config else OpenSearchSettings()
    )
    client = get_client(settings)
    if client is None:
        return None
    return AircraftSearchIndex(client, index=settings.index, max_results=settings.max_results)


def with_aircraft_index(operation: Callable[[AircraftSearchIndex], _T], what: str) -> _T | None:
    """Run one query against the aircraft index, or signal SQL fallback.

    Args:
        operation: Receives the index and returns whatever the caller
            needs.
        what: Short description of the query. Used for the fallback
            log line so operators can see which endpoint dropped
            through to SQL and how often.

    Returns:
        The operation's result, or ``None`` when OpenSearch is
        unavailable / unreachable / rejected the query — in which
        case the caller must use its own SQL path.
    """
    index = aircraft_search_index()
    if index is None:
        return None

    try:
        return operation(index)
    except SearchError as e:
        logger.warning("%s fell back to SQL: %s", what, e)
        return None
