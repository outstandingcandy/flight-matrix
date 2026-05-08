"""
Backward compatibility module - imports from duckduckgo_search.py.

This module is DEPRECATED. Please import from duckduckgo_search instead:

    # Old (deprecated):
    from src.utils.google_search import GoogleSearchClient

    # New (recommended):
    from src.utils.duckduckgo_search import DuckDuckGoSearchClient
"""

import warnings

# Re-export everything from duckduckgo_search for backward compatibility
from src.utils.duckduckgo_search import (
    MAX_CONTENT_LENGTH,
    MAX_RESULTS,
    REQUEST_INTERVAL,
    DuckDuckGoSearchClient,
    GoogleSearchClient,
    duckduckgo_search,
    get_duckduckgo_client,
    get_google_search_client,
    google_search,
)

# Issue deprecation warning on import
warnings.warn(
    "google_search module is deprecated. Use duckduckgo_search instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "MAX_CONTENT_LENGTH",
    "MAX_RESULTS",
    "REQUEST_INTERVAL",
    "DuckDuckGoSearchClient",
    "GoogleSearchClient",
    "duckduckgo_search",
    "get_duckduckgo_client",
    "get_google_search_client",
    "google_search",
]
