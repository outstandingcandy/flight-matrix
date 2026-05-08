"""
CLI task source for command-line specified tasks.

Provides a single task specified via the --task command line argument.
Useful for testing and debugging individual scraper tasks.
"""

import logging
from typing import Any

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask

logger = logging.getLogger("scraper.sources.cli")


class CLITaskSource(BaseTaskSource):
    """Task source for command-line specified tasks.

    Provides a single task from the --task command line argument.
    The task is processed once and then the source is exhausted.

    Attributes:
        task_type: The type of task (e.g., "xiaohongshu", "jetphotos").
        task_key: The task key from command line (e.g., account ID).
    """

    def __init__(
        self,
        task_type: str,
        task_key: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the CLI task source.

        Args:
            task_type: The task type (e.g., "xiaohongshu").
            task_key: The task key from command line.
            payload: Optional payload for the task.
        """
        super().__init__(task_type, max_attempts=1)
        self._task_key = task_key
        self._payload = payload or {}

        # CLI-specific state
        self._task_provided = False
        self._task_completed = False

        logger.info(f"CLITaskSource({task_type}) initialized with task_key: {task_key}")

    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        """Get pending task (single task from command line).

        Args:
            limit: Maximum number of tasks (ignored, always returns 0 or 1).

        Returns:
            List with single ScraperTask, or empty list if already provided.
        """
        with self._lock:
            if self._task_provided:
                return []

            self._task_provided = True
            task = self._create_task(self._task_key, self._payload)
            logger.info(f"CLITaskSource providing task: {self._task_key}")
            return [task]

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Hook called after task is marked completed.

        Args:
            task: The completed task.
            result: Optional result data.
        """
        self._task_completed = True
        logger.info(f"CLI task {task.task_key} completed")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Hook called after task is marked failed.

        Args:
            task: The failed task.
            error: Error message.
            retry: Whether to retry (ignored for CLI tasks).
        """
        self._task_completed = True
        logger.error(f"CLI task {task.task_key} failed: {error}")

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Hook called after task is marked no-data.

        Args:
            task: The task with no data.
            reason: Reason why there's no data.
        """
        self._task_completed = True
        logger.info(f"CLI task {task.task_key} no data: {reason}")

    def get_stats(self) -> dict[str, Any]:
        """Get source statistics.

        Returns:
            Dictionary with statistics.
        """
        stats = super().get_stats()
        with self._lock:
            stats.update(
                {
                    "task_key": self._task_key,
                    "task_provided": self._task_provided,
                    "task_completed": self._task_completed,
                    "total_pending": 0 if self._task_provided else 1,
                    "total_processing": 1
                    if (self._task_provided and not self._task_completed)
                    else 0,
                    "total_completed": 1 if self._task_completed else 0,
                }
            )
        return stats
