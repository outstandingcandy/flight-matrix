"""Common Sink interface and binding helper."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from resilient_scraper.models import ScraperResult, ScraperTask

logger = logging.getLogger("scraper.sinks")


class Sink(Protocol):
    """Post-scrape side effect handler.

    Sinks get bound to scrapers via :func:`bind_sink` so the scraper's
    ``on_success`` / ``on_failure`` hooks fire the sink methods.
    """

    def on_success(self, task: ScraperTask, result: ScraperResult) -> None: ...
    def on_failure(self, task: ScraperTask, error: Exception) -> None: ...


def bind_sink(scraper: Any, sink: Sink) -> None:
    """Wire sink.on_success / sink.on_failure onto an instantiated scraper.

    Preserves the scraper's own default ``super().on_success`` logging by
    chaining through the existing bound methods.
    """
    inner_success = scraper.on_success
    inner_failure = scraper.on_failure

    def _on_success(task: ScraperTask, result: ScraperResult) -> None:
        inner_success(task, result)
        try:
            sink.on_success(task, result)
        except Exception as e:
            logger.error(
                f"[{task.task_key}] sink.on_success failed for {task.task_type}: {e}",
                exc_info=True,
            )

    def _on_failure(task: ScraperTask, error: Exception) -> None:
        inner_failure(task, error)
        try:
            sink.on_failure(task, error)
        except Exception as e:
            logger.error(
                f"[{task.task_key}] sink.on_failure failed for {task.task_type}: {e}",
                exc_info=True,
            )

    scraper.on_success = _on_success
    scraper.on_failure = _on_failure
