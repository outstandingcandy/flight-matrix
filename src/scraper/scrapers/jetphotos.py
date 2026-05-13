"""Backward-compatible re-export of the JetPhotos scraper.

Canonical implementation is in
``resilient_scraper.scrapers.aviation.jetphotos``. DB persistence (marking
aircraft_static_info.images_downloaded, inserting aircraft_images rows) is
injected via the ``persist_images_callback`` config key; see
``src/scraper/sinks/jetphotos_sink.py``.
"""

from resilient_scraper.scrapers.aviation.jetphotos import (
    ImageMetadata,
    JetPhotosExtractor,
    JetPhotosResult,
    JetPhotosScraper,
)

__all__ = [
    "ImageMetadata",
    "JetPhotosExtractor",
    "JetPhotosResult",
    "JetPhotosScraper",
]
