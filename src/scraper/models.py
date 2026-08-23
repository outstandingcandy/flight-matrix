"""Backward-compat re-exports of framework-level scraper models.

Aviation-specific result classes (JetPhotosResult, FR24*Result, AirportDataResult, …)
have moved into ``resilient_scraper.scrapers.aviation.<name>.models``; all sinks
import them from there. This module now exists only so that in-repo callers of
``src.scraper.models.ScraperTask`` / ``TaskStatus`` / ``WorkerStatus`` keep
working — new code should import from ``resilient_scraper.models`` directly.
"""

from resilient_scraper.models import (
    ScraperConfig,
    ScraperResult,
    ScraperTask,
    TaskStatus,
    WorkerInfo,
    WorkerStatus,
    utc_now,
)

__all__ = [
    "ScraperConfig",
    "ScraperResult",
    "ScraperTask",
    "TaskStatus",
    "WorkerInfo",
    "WorkerStatus",
    "utc_now",
]
