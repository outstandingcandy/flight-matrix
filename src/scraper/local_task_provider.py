"""
Local task provider for single-machine scraping.

A generic task provider that delegates to task-type-specific LocalTaskSource
implementations. This allows different scrapers to have different local task
acquisition strategies without hardcoding logic in this provider.
"""

import logging
from typing import Any

from src.scraper.local_task_source import LocalTaskSource
from src.scraper.models import ScraperTask, TaskStatus, WorkerStatus

logger = logging.getLogger("scraper.local_task_provider")


class LocalTaskProvider:
    """Generic local task provider that delegates to task sources.

    This provider manages multiple LocalTaskSource instances, one for each
    scraper type. It implements the TaskProvider protocol by delegating
    to the appropriate task source.

    Features:
        - Supports multiple task types simultaneously
        - Each task type has its own LocalTaskSource implementation
        - Provides unified interface for the ScraperWorker

    Usage:
        provider = LocalTaskProvider()
        provider.register_source(JetPhotosTaskSource(...))
        provider.register_source(FR24AirportTaskSource(...))
        tasks = provider.claim_tasks("worker-1", task_types=["jetphotos"])
    """

    def __init__(self) -> None:
        """Initialize the local task provider."""
        self._sources: dict[str, LocalTaskSource] = {}
        self._task_to_source: dict[int, str] = {}  # task_id -> task_type

        logger.info("LocalTaskProvider initialized (generic mode)")

    def register_source(self, source: LocalTaskSource) -> None:
        """Register a task source.

        Args:
            source: LocalTaskSource implementation for a specific task type.
        """
        task_type = source.task_type
        self._sources[task_type] = source
        logger.info(f"Registered task source: {task_type}")

    def get_source(self, task_type: str) -> LocalTaskSource | None:
        """Get a registered task source by type.

        Args:
            task_type: The task type to look up.

        Returns:
            The registered LocalTaskSource or None if not found.
        """
        return self._sources.get(task_type)

    def ensure_tables_exist(self) -> None:
        """No-op for local mode - no task queue tables needed."""
        logger.debug("LocalTaskProvider: ensure_tables_exist is no-op")

    def claim_tasks(
        self,
        worker_id: str,
        task_types: list[str] | None = None,
        limit: int = 1,
        stale_timeout_minutes: int = 5,
        max_concurrent_by_type: dict[str, int] | None = None,
    ) -> list[ScraperTask]:
        """Claim tasks from registered sources.

        Args:
            worker_id: ID of the claiming worker.
            task_types: Optional filter for specific task types.
            limit: Maximum number of tasks to claim.
            stale_timeout_minutes: Not used in local mode.
            max_concurrent_by_type: Not used in local mode (single worker).

        Returns:
            List of claimed tasks.
        """
        tasks: list[ScraperTask] = []

        # Determine which sources to query
        source_types = task_types if task_types else list(self._sources.keys())

        for task_type in source_types:
            if len(tasks) >= limit:
                break

            source = self._sources.get(task_type)
            if not source:
                continue

            # Get tasks from this source
            remaining = limit - len(tasks)
            source_tasks = source.get_pending_tasks(limit=remaining)

            # Track task_id -> task_type mapping
            for task in source_tasks:
                task.claimed_by = worker_id
                if task.id is not None:
                    self._task_to_source[task.id] = task_type

            tasks.extend(source_tasks)

        return tasks

    def _get_source_for_task(self, task_id: int) -> LocalTaskSource | None:
        """Get the source that owns a task.

        Args:
            task_id: The task ID to look up.

        Returns:
            The LocalTaskSource that created this task, or None.
        """
        task_type = self._task_to_source.get(task_id)
        if task_type:
            return self._sources.get(task_type)
        return None

    def _find_task_in_sources(
        self, task_id: int
    ) -> tuple[LocalTaskSource, ScraperTask] | tuple[None, None]:
        """Find a task across all sources.

        Args:
            task_id: The task ID to find.

        Returns:
            Tuple of (source, task) if found, (None, None) otherwise.
        """
        # First try the tracked source
        task_type = self._task_to_source.get(task_id)
        if task_type:
            source = self._sources.get(task_type)
            if source:
                # Check if source has the task in its active tasks
                if hasattr(source, "_active_tasks"):
                    task = source._active_tasks.get(task_id)
                    if task:
                        return source, task

        # Fallback: search all sources
        for source in self._sources.values():
            if hasattr(source, "_active_tasks"):
                task = source._active_tasks.get(task_id)
                if task:
                    return source, task

        return None, None

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
    ) -> None:
        """Update task status.

        Args:
            task_id: ID of the task.
            status: New status.
        """
        source, task = self._find_task_in_sources(task_id)
        if task:
            task.status = status

    def update_task_heartbeat(self, task_id: int) -> None:
        """No-op for local mode - no heartbeat tracking needed."""
        pass

    def complete_task(
        self,
        task_id: int,
        result: dict[str, Any] | None = None,
        worker_id: str | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        """Mark a task as completed.

        Args:
            task_id: ID of the completed task.
            result: Result data.
            worker_id: ID of the worker that completed it.
            duration_seconds: Time taken to process.
        """
        source, task = self._find_task_in_sources(task_id)
        if source and task:
            source.mark_completed(task, result)
            self._task_to_source.pop(task_id, None)
            logger.debug(f"Task {task_id} completed in {duration_seconds:.2f}s")
        else:
            logger.warning(f"Task {task_id} not found for completion")

    def complete_task_no_data(
        self,
        task_id: int,
        reason: str = "No data found",
        worker_id: str | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        """Mark a task as no_data.

        Args:
            task_id: ID of the task.
            reason: Reason why there's no data.
            worker_id: ID of the worker that processed it.
            duration_seconds: Time taken to process.
        """
        source, task = self._find_task_in_sources(task_id)
        if source and task:
            source.mark_no_data(task, reason)
            self._task_to_source.pop(task_id, None)
            logger.debug(f"Task {task_id} marked as no_data in {duration_seconds:.2f}s")
        else:
            logger.warning(f"Task {task_id} not found for no_data")

    def fail_task(
        self,
        task_id: int,
        error: str,
        worker_id: str | None = None,
        duration_seconds: float = 0.0,
        retry: bool = True,
    ) -> None:
        """Mark a task as failed.

        Args:
            task_id: ID of the failed task.
            error: Error message.
            worker_id: ID of the worker that processed it.
            duration_seconds: Time taken before failure.
            retry: Whether to allow retry.
        """
        source, task = self._find_task_in_sources(task_id)
        if source and task:
            source.mark_failed(task, error, retry)
            self._task_to_source.pop(task_id, None)
            logger.debug(f"Task {task_id} failed in {duration_seconds:.2f}s: {error}")
        else:
            logger.warning(f"Task {task_id} not found for failure")

    def register_worker(
        self,
        worker_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """No-op for local mode - no worker registration needed."""
        logger.debug(f"LocalTaskProvider: register_worker({worker_id}) is no-op")

    def update_worker_heartbeat(
        self,
        worker_id: str,
        status: WorkerStatus = WorkerStatus.ACTIVE,
        current_task_id: int | None = None,
    ) -> None:
        """No-op for local mode - no worker heartbeat needed."""
        pass

    def increment_worker_completed(self, worker_id: str) -> None:
        """No-op for local mode - stats tracked by sources."""
        pass

    def deactivate_worker(self, worker_id: str) -> None:
        """No-op for local mode - no worker management needed."""
        logger.debug(f"LocalTaskProvider: deactivate_worker({worker_id}) is no-op")

    def get_stats(self) -> dict[str, Any]:
        """Get provider statistics aggregated from all sources.

        Returns:
            Dictionary with provider statistics.
        """
        stats: dict[str, Any] = {
            "mode": "local",
            "sources": {},
            "total_pending": 0,
            "total_processing": 0,
            "total_completed": 0,
            "total_failed": 0,
            "active_workers": 1,
        }

        for task_type, source in self._sources.items():
            source_stats = source.get_stats()
            stats["sources"][task_type] = source_stats

            # Aggregate totals
            stats["total_pending"] += source_stats.get("total_pending", 0)
            stats["total_processing"] += source_stats.get("total_processing", 0)
            stats["total_completed"] += source_stats.get("total_completed", 0)
            stats["total_failed"] += source_stats.get("total_failed", 0)

        return stats

    def get_queue_stats(self) -> dict[str, Any]:
        """Alias for get_stats() for compatibility."""
        return self.get_stats()
