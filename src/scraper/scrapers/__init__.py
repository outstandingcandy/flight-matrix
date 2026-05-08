"""
Scraper implementations.

Each scraper inherits from BaseScraper and implements the scrape() method.
"""

from src.scraper.scrapers.airport_data import AirportDataScraper
from src.scraper.scrapers.fr24_aircraft import FR24AircraftScraper
from src.scraper.scrapers.fr24_airport import (
    FR24AirportArrivalsScraper,
    FR24AirportDeparturesScraper,
    FR24AirportScraper,
)
from src.scraper.scrapers.fr24_map import FR24MapScraper
from src.scraper.scrapers.jetphotos import JetPhotosScraper

__all__ = [
    "AirportDataScraper",
    "FR24AircraftScraper",
    "FR24AirportArrivalsScraper",
    "FR24AirportDeparturesScraper",
    "FR24AirportScraper",
    "FR24MapScraper",
    "JetPhotosScraper",
]
