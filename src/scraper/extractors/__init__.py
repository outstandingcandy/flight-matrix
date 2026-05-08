"""
Extractors module for parsing HTML and extracting structured fields.

Extractors are pure functions that parse HTML content without network dependencies,
enabling both real-time extraction during scraping and re-extraction from saved HTML.
"""

from src.scraper.extractors.airport_data import AirportDataExtractor
from src.scraper.extractors.base import BaseExtractor
from src.scraper.extractors.jetphotos import JetPhotosExtractor

__all__ = [
    "AirportDataExtractor",
    "BaseExtractor",
    "JetPhotosExtractor",
]
