"""Async facade around :class:`src.scraper.task_queue.TaskQueue`.

Implements :class:`resilient_scraper.queue.TaskQueue` so the submodule Worker
can drive the flight-matrix Postgres/SQLite queue without knowing which
dialect is underneath.

All methods wrap the sync underlying implementation via ``asyncio.to_thread``;
the Worker stays on its event loop while the actual DB work runs in the
default executor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.scraper.models import TaskStatus
from src.scraper.task_queue import TaskQueue

logger = logging.getLogger("scraper.async_task_queue")


class AsyncTaskQueue:
    """Async adapter around the sync flight-matrix TaskQueue."""

    def __init__(self, inner: TaskQueue) -> None:
        self._q = inner

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    async def claim_task(
        self,
        worker_id: str,
        task_types: list[str],
        stale_minutes: int = 5,
    ) -> dict[str, Any] | None:
        tasks = await asyncio.to_thread(
            self._q.claim_tasks,
            worker_id,
            task_types,
            1,
            stale_minutes,
        )
        if not tasks:
            return None
        task = tasks[0]
        # Normalise to the dict shape the submodule Worker expects.
        return {
            "id": task.id,
            "task_type": task.task_type,
            "task_key": task.task_key,
            "payload": task.payload or {},
            "priority": task.priority,
            "status": task.status,
            "attempts": task.attempts,
            "max_attempts": task.max_attempts,
            "scheduled_for": task.scheduled_for,
            "claimed_by": task.claimed_by,
            "claimed_at": task.claimed_at,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
        }

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        def _get() -> dict[str, Any] | None:
            session = self._q.get_session()
            try:
                from sqlalchemy import text  # local import keeps top clean

                row = session.execute(
                    text("SELECT * FROM scraper_tasks WHERE id = :id"),
                    {"id": task_id},
                ).mappings().fetchone()
                return dict(row) if row else None
            finally:
                session.close()

        return await asyncio.to_thread(_get)

    async def update_status(self, task_id: int, status: str) -> None:
        await asyncio.to_thread(
            self._q.update_task_status, task_id, TaskStatus(status)
        )

    async def update_heartbeat(self, task_id: int) -> None:
        await asyncio.to_thread(self._q.update_heartbeat_at, task_id)

    async def complete_task(
        self,
        task_id: int,
        result_data: dict[str, Any],
        worker_id: str,
        duration: float,
    ) -> None:
        await asyncio.to_thread(
            self._q.complete_task, task_id, result_data, worker_id, duration
        )

    async def complete_task_no_data(
        self,
        task_id: int,
        reason: str,
        worker_id: str,
        duration: float,
    ) -> None:
        await asyncio.to_thread(
            self._q.complete_task_no_data, task_id, reason, worker_id, duration
        )

    async def fail_task(
        self,
        task_id: int,
        error: str,
        worker_id: str,
        duration: float,
        retry: bool = True,
    ) -> None:
        await asyncio.to_thread(
            self._q.fail_task, task_id, error, worker_id, duration, retry
        )

    # ------------------------------------------------------------------
    # Worker registry
    # ------------------------------------------------------------------

    async def register_worker(
        self, worker_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        await asyncio.to_thread(self._q.register_worker, worker_id, metadata)

    async def deactivate_worker(self, worker_id: str) -> None:
        await asyncio.to_thread(self._q.deactivate_worker, worker_id)

    async def update_worker_heartbeat(
        self, worker_id: str, current_task_id: int | None = None
    ) -> None:
        # Main-repo signature is (worker_id, status=..., current_task_id=...);
        # only pass current_task_id by keyword so `status` keeps its default.
        def _call() -> None:
            self._q.update_worker_heartbeat(
                worker_id, current_task_id=current_task_id
            )

        await asyncio.to_thread(_call)

    async def increment_worker_completed(self, worker_id: str) -> None:
        await asyncio.to_thread(self._q.increment_worker_completed, worker_id)

    # ------------------------------------------------------------------
    # Login interaction — flight-matrix scrapers don't use these (only XHS
    # does, and XHS lives in the submodule with its own DB layer). For
    # tasks that don't need login support, log + no-op is fine.
    # ------------------------------------------------------------------

    async def set_login_required(
        self, task_id: int, screenshot_data: bytes, phase: str = "qr_scan"
    ) -> None:
        logger.debug(
            "set_login_required called but flight-matrix TaskQueue "
            "doesn't persist login screenshots (task_id=%s, phase=%s)",
            task_id, phase,
        )

    async def update_login_screenshot(
        self, task_id: int, screenshot_url: str
    ) -> None:
        logger.debug(
            "update_login_screenshot ignored by flight-matrix TaskQueue (task_id=%s)",
            task_id,
        )

    async def clear_login_screenshot(self, task_id: int) -> None:
        # no-op
        return None

    async def submit_user_input(self, task_id: int, value: str) -> int:
        raise NotImplementedError(
            "flight-matrix TaskQueue does not support user-input submission"
        )

    async def consume_user_input(self, task_id: int) -> str | None:
        return None
