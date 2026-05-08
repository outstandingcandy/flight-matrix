"""
Base task source for local (single-machine) scraping.

Provides an abstract base class with shared state management (locks, counters,
active task tracking) that all task sources inherit from. Subclasses only need
to implement get_pending_tasks() and optionally override hooks for
completion/failure/no-data handling.
"""

import logging
import threading
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from src.scraper.models import ScraperTask, TaskStatus

logger = logging.getLogger("scraper.sources.base")


class BaseTaskSource(ABC):
    """Abstract base class for all local task sources.

    Provides:
        - Thread-safe task counter and active task tracking
        - Common mark_completed/mark_failed/mark_no_data with lock management
        - Base get_stats() with common counters
        - _create_task() helper for consistent task creation

    Subclasses must implement:
        - get_pending_tasks(): Return next batch of tasks

    Subclasses may override:
        - _on_completed(): Hook called after task marked completed
        - _on_failed(): Hook called after task marked failed
        - _on_no_data(): Hook called after task marked no-data
        - get_stats(): Extend with source-specific statistics
    """

    def __init__(self, task_type: str, max_attempts: int = 3) -> None:
        """Initialize the base task source.

        Args:
            task_type: Identifier for this source's task type.
            max_attempts: Default max attempts for tasks created by this source.
        """
        self._task_type = task_type
        self._max_attempts = max_attempts

        # Thread safety
        self._lock = threading.Lock()

        # Task tracking
        self._task_counter = 0
        self._active_tasks: dict[int, ScraperTask] = {}

        # Counters
        self._completed_count = 0
        self._failed_count = 0
        self._no_data_count = 0

    @property
    def task_type(self) -> str:
        """Return the task type this source provides."""
        return self._task_type

    @abstractmethod
    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        """Get pending tasks for processing.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of ScraperTask objects ready for processing.
        """
        ...

    def _create_task(
        self,
        task_key: str,
        payload: dict[str, Any] | None = None,
        max_attempts: int | None = None,
    ) -> ScraperTask:
        """Create a new task with consistent defaults.

        Must be called within self._lock context.

        Args:
            task_key: Unique key for this task.
            payload: Task payload data.
            max_attempts: Override default max_attempts for this task.

        Returns:
            New ScraperTask instance, already tracked in _active_tasks.
        """
        self._task_counter += 1
        task = ScraperTask(
            id=self._task_counter,
            task_type=self._task_type,
            task_key=task_key,
            status=TaskStatus.CLAIMED,
            payload=payload or {},
            claimed_at=datetime.now(UTC),
            attempts=1,
            max_attempts=max_attempts or self._max_attempts,
        )
        self._active_tasks[task.id] = task
        return task

    def _remove_active_task(self, task: ScraperTask) -> None:
        """Remove a task from active tracking.

        Must be called within self._lock context.

        Args:
            task: The task to remove.
        """
        if task.id is not None:
            self._active_tasks.pop(task.id, None)

    def mark_completed(
        self,
        task: ScraperTask,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Mark a task as completed.

        Args:
            task: The completed task.
            result: Optional result data.
        """
        with self._lock:
            self._remove_active_task(task)
            self._completed_count += 1
        self._on_completed(task, result)

    def mark_failed(
        self,
        task: ScraperTask,
        error: str,
        retry: bool = True,
    ) -> None:
        """Mark a task as failed.

        Args:
            task: The failed task.
            error: Error message.
            retry: Whether to allow retry on next poll.
        """
        with self._lock:
            self._remove_active_task(task)
            self._failed_count += 1
        self._on_failed(task, error, retry)

    def mark_no_data(
        self,
        task: ScraperTask,
        reason: str = "No data found",
    ) -> None:
        """Mark a task as having no data (not a failure).

        Args:
            task: The task with no data.
            reason: Reason why there's no data.
        """
        with self._lock:
            self._remove_active_task(task)
            self._no_data_count += 1
        self._on_no_data(task, reason)

    def get_stats(self) -> dict[str, Any]:
        """Get statistics for this task source.

        Returns:
            Dictionary with common statistics. Subclasses should call
            super().get_stats() and merge with source-specific stats.
        """
        with self._lock:
            return {
                "task_type": self._task_type,
                "active": len(self._active_tasks),
                "completed": self._completed_count,
                "failed": self._failed_count,
                "no_data": self._no_data_count,
            }

    # --- Hooks for subclasses ---

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Hook called after task is marked completed. Override for custom logic."""
        logger.info(f"Task {task.id} ({task.task_key}) completed")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Hook called after task is marked failed. Override for custom logic."""
        logger.warning(f"Task {task.id} ({task.task_key}) failed: {error}")

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Hook called after task is marked no-data. Override for custom logic."""
        logger.info(f"Task {task.id} ({task.task_key}) no data: {reason}")


# Backward compatibility alias
LocalTaskSource = BaseTaskSource
