"""
Distributed Web Scraper Framework

A general-purpose distributed web scraping architecture with PostgreSQL-backed task queue,
browser pooling, and worker node management.

Supports two operating modes:
- Distributed: Uses PostgreSQL task queue tables (scraper_tasks, etc.)
- Local: Uses task-type-specific LocalTaskSource implementations

Example usage:
    from src.scraper import ScraperWorker, JetPhotosScraper

    # Distributed mode (default)
    worker = ScraperWorker(config, mode="distributed")
    worker.register_scraper(JetPhotosScraper)
    await worker.run()

    # Local mode - dynamically creates task sources based on registered scrapers
    worker = ScraperWorker(config, mode="local")
    worker.register_scraper(JetPhotosScraper)
    worker.register_scraper(FR24AirportScraper)
    await worker.run()
"""

from src.scraper.base import BaseScraper
from src.scraper.browser_pool import BrowserPool
from src.scraper.local_task_provider import LocalTaskProvider
from src.scraper.local_task_source import LocalTaskSource
from src.scraper.models import (
    ScraperResult,
    ScraperTask,
    TaskStatus,
    WorkerStatus,
)
from src.scraper.sources import FR24AirportTaskSource, JetPhotosTaskSource
from src.scraper.task_provider import TaskProvider
from src.scraper.task_queue import TaskQueue
from src.scraper.worker import ScraperWorker

__all__ = [
    "BaseScraper",
    "BrowserPool",
    "FR24AirportTaskSource",
    "JetPhotosTaskSource",
    "LocalTaskProvider",
    "LocalTaskSource",
    "ScraperResult",
    "ScraperTask",
    "ScraperWorker",
    "TaskProvider",
    "TaskQueue",
    "TaskStatus",
    "WorkerStatus",
]
