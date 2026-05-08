"""
Queue task source for pulling tasks from the database queue in local mode.

Pulls pending tasks from the scraper_tasks table for a specific task type.
Useful for testing distributed tasks locally.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from src.scraper.local_task_source import BaseTaskSource
from src.scraper.models import ScraperTask, TaskStatus

logger = logging.getLogger("scraper.sources.queue")


class QueueTaskSource(BaseTaskSource):
    """Task source that pulls from the database queue.

    Pulls pending tasks from scraper_tasks table for a specific task type.
    Updates task status in the database as tasks are processed.

    Attributes:
        task_type: The type of task to pull (e.g., "airport_data").
        database_url: Database connection URL.
    """

    def __init__(
        self,
        task_type: str,
        database_url: str,
        limit: int = 10,
    ) -> None:
        """Initialize the queue task source.

        Args:
            task_type: The task type to pull (e.g., "airport_data").
            database_url: Database connection URL.
            limit: Maximum tasks to pull per batch.
        """
        super().__init__(task_type=task_type, max_attempts=3)
        self._database_url = database_url
        self._limit = limit

        # Database engine
        self._engine = create_engine(database_url, pool_pre_ping=True)

        logger.info(f"QueueTaskSource({task_type}) initialized, pulling from database queue")

    def get_pending_tasks(self, limit: int = 10) -> list[ScraperTask]:
        """Get pending tasks from the database queue.

        Uses SELECT FOR UPDATE SKIP LOCKED to safely claim tasks.

        Args:
            limit: Maximum number of tasks to return.

        Returns:
            List of ScraperTask objects ready for processing.
        """
        effective_limit = min(limit, self._limit)
        tasks: list[ScraperTask] = []

        try:
            with self._engine.connect() as conn:
                # Claim tasks atomically
                result = conn.execute(
                    text(
                        """
                        UPDATE scraper_tasks
                        SET status = 'claimed',
                            claimed_at = CURRENT_TIMESTAMP,
                            attempts = attempts + 1
                        WHERE id IN (
                            SELECT id FROM scraper_tasks
                            WHERE task_type = :task_type
                            AND status = 'pending'
                            AND scheduled_for <= CURRENT_TIMESTAMP
                            ORDER BY priority DESC, created_at ASC
                            LIMIT :limit
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING id, task_type, task_key, status, priority,
                                  payload, attempts, max_attempts, created_at
                        """
                    ),
                    {"task_type": self._task_type, "limit": effective_limit},
                )
                conn.commit()

                with self._lock:
                    for row in result:
                        task = ScraperTask(
                            id=row[0],
                            task_type=row[1],
                            task_key=row[2],
                            status=TaskStatus.CLAIMED,
                            priority=row[4],
                            payload=row[5] or {},
                            attempts=row[6],
                            max_attempts=row[7],
                            created_at=row[8],
                            claimed_at=datetime.now(UTC),
                        )
                        tasks.append(task)
                        self._active_tasks[task.id] = task

                if tasks:
                    logger.info(
                        f"QueueTaskSource claimed {len(tasks)} tasks: {[t.task_key for t in tasks]}"
                    )

        except SQLAlchemyError as e:
            logger.error(f"Error claiming tasks from queue: {e}")

        return tasks

    def _on_completed(self, task: ScraperTask, result: dict[str, Any] | None) -> None:
        """Update task status in database on completion."""
        try:
            with self._engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE scraper_tasks
                        SET status = 'completed',
                            completed_at = CURRENT_TIMESTAMP,
                            result = :result
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": task.id,
                        "result": json.dumps(result) if result else None,
                    },
                )
                conn.commit()

            logger.info(f"Queue task {task.task_key} completed")

        except SQLAlchemyError as e:
            logger.error(f"Error completing task {task.id}: {e}")

    def _on_failed(self, task: ScraperTask, error: str, retry: bool) -> None:
        """Update task status in database on failure."""
        try:
            # Determine new status based on retry and attempts
            if retry and task.attempts < task.max_attempts:
                new_status = "pending"  # Will be retried
            else:
                new_status = "failed"

            with self._engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE scraper_tasks
                        SET status = :status,
                            last_error = :error,
                            completed_at = CASE WHEN :status = 'failed'
                                THEN CURRENT_TIMESTAMP ELSE NULL END
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": task.id,
                        "status": new_status,
                        "error": error,
                    },
                )
                conn.commit()

            logger.error(f"Queue task {task.task_key} failed: {error}")

        except SQLAlchemyError as e:
            logger.error(f"Error failing task {task.id}: {e}")

    def _on_no_data(self, task: ScraperTask, reason: str) -> None:
        """Update task status in database on no-data."""
        try:
            with self._engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE scraper_tasks
                        SET status = 'no_data',
                            completed_at = CURRENT_TIMESTAMP,
                            last_error = :reason
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": task.id,
                        "reason": reason,
                    },
                )
                conn.commit()

            logger.info(f"Queue task {task.task_key} no data: {reason}")

        except SQLAlchemyError as e:
            logger.error(f"Error marking task {task.id} as no_data: {e}")

    def get_stats(self) -> dict[str, Any]:
        """Get source statistics.

        Returns:
            Dictionary with statistics.
        """
        stats = super().get_stats()
        stats.update(
            {
                "source": "database_queue",
                "total_pending": self._get_pending_count(),
            }
        )
        return stats

    def _get_pending_count(self) -> int:
        """Get count of pending tasks in the queue."""
        try:
            with self._engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM scraper_tasks
                        WHERE task_type = :task_type AND status = 'pending'
                        """
                    ),
                    {"task_type": self._task_type},
                )
                return result.scalar() or 0
        except SQLAlchemyError:
            return 0
