"""
Abstract TaskProvider protocol for the scraper system.

Defines the interface that both distributed (TaskQueue) and local
(LocalTaskProvider) task providers must implement.
"""

from typing import Any, Protocol, runtime_checkable

from src.scraper.models import ScraperTask, TaskStatus, WorkerStatus


@runtime_checkable
class TaskProvider(Protocol):
    """Protocol defining the task provider interface.

    This interface is implemented by both:
    - TaskQueue: Distributed mode using PostgreSQL scraper_tasks table
    - LocalTaskProvider: Local mode polling aircraft_static_info directly

    Required Methods:
        claim_tasks: Claim tasks for processing
        complete_task: Mark a task as completed with result
        complete_task_no_data: Mark a task as having no data
        fail_task: Mark a task as failed
        update_task_status: Update task status

    Optional Methods (with default no-op implementations in LocalTaskProvider):
        update_task_heartbeat: Update task heartbeat
        register_worker: Register a worker
        update_worker_heartbeat: Update worker heartbeat
        deactivate_worker: Deactivate a worker
        get_stats: Get provider statistics
    """

    def claim_tasks(
        self,
        worker_id: str,
        task_types: list[str] | None = None,
        limit: int = 1,
        stale_timeout_minutes: int = 5,
        max_concurrent_by_type: dict[str, int] | None = None,
    ) -> list[ScraperTask]:
        """Claim tasks for processing.

        Args:
            worker_id: ID of the claiming worker.
            task_types: Optional filter for specific task types.
            limit: Maximum number of tasks to claim.
            stale_timeout_minutes: Reset tasks with no heartbeat for this long.
            max_concurrent_by_type: Optional dict mapping task_type to max concurrent
                workers. -1 means no limit. Task types that have reached their
                limit will be excluded from claiming.

        Returns:
            List of claimed tasks.
        """
        ...

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
            result: Result data to store.
            worker_id: ID of the worker that completed it.
            duration_seconds: Time taken to process.
        """
        ...

    def complete_task_no_data(
        self,
        task_id: int,
        reason: str = "No data found",
        worker_id: str | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        """Mark a task as no_data (target has no data, not a failure).

        Args:
            task_id: ID of the task.
            reason: Reason why there's no data.
            worker_id: ID of the worker that processed it.
            duration_seconds: Time taken to process.
        """
        ...

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
            retry: Whether to schedule a retry.
        """
        ...

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
        ...

    def update_task_heartbeat(self, task_id: int) -> None:
        """Update task heartbeat to indicate it's still being processed.

        Args:
            task_id: ID of the task.
        """
        ...

    def register_worker(
        self,
        worker_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a worker.

        Args:
            worker_id: Unique worker identifier.
            metadata: Optional worker metadata.
        """
        ...

    def update_worker_heartbeat(
        self,
        worker_id: str,
        status: WorkerStatus = WorkerStatus.ACTIVE,
        current_task_id: int | None = None,
    ) -> None:
        """Update worker heartbeat and status.

        Args:
            worker_id: Worker identifier.
            status: Current worker status.
            current_task_id: ID of task being processed.
        """
        ...

    def deactivate_worker(self, worker_id: str) -> None:
        """Mark a worker as stopped.

        Args:
            worker_id: Worker identifier.
        """
        ...

    def increment_worker_completed(self, worker_id: str) -> None:
        """Increment worker's completed task count.

        Args:
            worker_id: Worker identifier.
        """
        ...

    def get_stats(self) -> dict[str, Any]:
        """Get provider statistics.

        Returns:
            Dictionary with provider statistics.
        """
        ...

    def ensure_tables_exist(self) -> None:
        """Create required tables if they don't exist."""
        ...
