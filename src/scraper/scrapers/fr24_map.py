"""Backward-compatible re-export of the FR24 map scraper.

Canonical implementation has moved to
``resilient_scraper.scrapers.aviation.fr24_map``. DB persistence is now owned
by flight-matrix sinks (see ``src/scraper/sinks/fr24_map_sink.py``) and
injected via ``scraper.on_success``.

This stub will be removed once callers import from the submodule directly.
"""

from resilient_scraper.scrapers.aviation.fr24_map import (
    FR24MapAircraftData,
    FR24MapResult,
    FR24MapScraper,
)

__all__ = ["FR24MapAircraftData", "FR24MapResult", "FR24MapScraper"]
