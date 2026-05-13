"""Flight-matrix scraper package.

The core scraping framework lives in the `resilient_scraper` submodule. This
package holds flight-matrix-specific glue — TaskQueue adapters, domain Sinks,
schedulers, and task sources.

Typical entrypoint:
    python -m src.scraper_main --config config/config.yaml

Programmatic wiring happens inside :mod:`src.scraper_main`: it composes an
:class:`~resilient_scraper.service.worker.Worker` with a TaskQueue
(:class:`~src.scraper.async_task_queue.AsyncTaskQueue` for queue-backed mode,
:class:`~src.scraper.local_task_queue.LocalTaskQueue` for domain-table polling,
:class:`~src.scraper.cli_task_queue.CLITaskQueue` for one-shot runs).
"""

from resilient_scraper.models import (
    ScraperResult,
    ScraperTask,
    TaskStatus,
    WorkerInfo,
    WorkerStatus,
)

from src.scraper.async_task_queue import AsyncTaskQueue
from src.scraper.cli_task_queue import CLITaskQueue
from src.scraper.local_task_queue import LocalTaskQueue
from src.scraper.sources import FR24AirportTaskSource, JetPhotosTaskSource
from src.scraper.task_queue import TaskQueue

__all__ = [
    "AsyncTaskQueue",
    "CLITaskQueue",
    "FR24AirportTaskSource",
    "JetPhotosTaskSource",
    "LocalTaskQueue",
    "ScraperResult",
    "ScraperTask",
    "TaskQueue",
    "TaskStatus",
    "WorkerInfo",
    "WorkerStatus",
]
