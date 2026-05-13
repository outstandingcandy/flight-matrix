"""Backward-compatible re-export of the airport-data.com scraper.

Canonical implementation is in
``resilient_scraper.scrapers.aviation.airport_data``. DB persistence and
follow-up task enqueue are injected via config callbacks; see
``src/scraper/sinks/airport_data_sink.py``.
"""

from resilient_scraper.scrapers.aviation.airport_data import (
    AirportDataAircraftData,
    AirportDataExtractor,
    AirportDataResult,
    AirportDataScraper,
)

__all__ = [
    "AirportDataAircraftData",
    "AirportDataExtractor",
    "AirportDataResult",
    "AirportDataScraper",
]
