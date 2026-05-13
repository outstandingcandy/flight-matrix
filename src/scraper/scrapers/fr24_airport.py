"""Backward-compatible re-export of FR24 airport arrivals/departures scrapers.

Canonical implementation is in
``resilient_scraper.scrapers.aviation.fr24_airport``.
"""

from resilient_scraper.scrapers.aviation.fr24_airport import (
    FlightData,
    FR24AirportArrivalsScraper,
    FR24AirportDeparturesScraper,
    FR24AirportScraper,
    FR24FlightsResult,
)

__all__ = [
    "FR24AirportArrivalsScraper",
    "FR24AirportDeparturesScraper",
    "FR24AirportScraper",
    "FR24FlightsResult",
    "FlightData",
]
