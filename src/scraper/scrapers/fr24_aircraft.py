"""Backward-compatible re-export of the FR24 aircraft schedule scraper.

Canonical implementation is in
``resilient_scraper.scrapers.aviation.fr24_aircraft``. DB persistence is now
owned by flight-matrix sinks (see ``src/scraper/sinks/``).
"""

from resilient_scraper.scrapers.aviation.fr24_aircraft import (
    FlightData,
    FR24AircraftResult,
    FR24AircraftScraper,
)

__all__ = ["FR24AircraftResult", "FR24AircraftScraper", "FlightData"]
