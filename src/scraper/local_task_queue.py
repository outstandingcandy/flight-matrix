"""Async TaskQueue adapter around the sync LocalTaskSource system.

Preserves flight-matrix's "local polling" mode: instead of pulling from the
``scraper_tasks`` queue, the sources peek at domain tables
(``aircraft_static_info``, ``xiaohongshu_authors``, ...) and hand tasks
straight to the Worker. The Worker doesn't know — it sees a normal
:class:`resilient_scraper.queue.TaskQueue`.

Task lifecycle (claim → complete/fail) is held in memory here; we don't
persist anything to ``scraper_tasks``, matching the old LocalTaskProvider
semantics.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import UTC, datetime, timezone
from typing import Any

from src.scraper.local_task_source import BaseTaskSource, LocalTaskSource
from src.scraper.models import ScraperTask

logger = logging.getLogger("scraper.local_task_queue")

TaskSource = BaseTaskSource | LocalTaskSource


class LocalTaskQueue:
    """Async TaskQueue that serves tasks from registered LocalTaskSources.

    No SQL, no persistence: sources produce ScraperTask objects from their
    own polling logic, this class just bridges them into the Worker via the
    submodule TaskQueue protocol.
    """

    def __init__(self) -> None:
        self._sources: dict[str, TaskSource] = {}
        # in-flight task bookkeeping
        self._by_id: dict[int, tuple[str, ScraperTask]] = {}
        self._id_counter = itertools.count(1)
        self._lock = asyncio.Lock()
        # Rotate the starting task_type on each claim so a fast producer
        # (fr24_map via direct API) can't starve slower ones (adsbx_map) by
        # always being first in the iteration order.
        self._claim_rotation: int = 0

    def register_source(self, source: TaskSource) -> None:
        task_type = source.task_type
        self._sources[task_type] = source
        logger.info(f"LocalTaskQueue registered source for {task_type}")

    @property
    def task_types(self) -> list[str]:
        return list(self._sources.keys())

    # ------------------------------------------------------------------
    # TaskQueue protocol
    # ------------------------------------------------------------------

    async def claim_task(
        self,
        worker_id: str,
        task_types: list[str],
        stale_minutes: int = 5,
    ) -> dict[str, Any] | None:
        async with self._lock:
            # Round-robin across registered types so no single source holds
            # the slot indefinitely. We still respect the caller's
            # task_types order; we just shift the start index each call.
            active = [t for t in task_types if t in self._sources]
            if not active:
                return None
            start = self._claim_rotation % len(active)
            self._claim_rotation = (self._claim_rotation + 1) % max(len(active), 1)
            order = active[start:] + active[:start]
            for task_type in order:
                source = self._sources.get(task_type)
                if source is None:
                    continue
                tasks = await asyncio.to_thread(source.get_pending_tasks, 1)
                if not tasks:
                    continue
                task = tasks[0]
                tid = next(self._id_counter)
                task.id = tid
                self._by_id[tid] = (task_type, task)
                now = datetime.now(UTC)
                logger.debug(
                    f"LocalTaskQueue serving {task_type}:{task.task_key} (id={tid})"
                )
                return {
                    "id": tid,
                    "task_type": task_type,
                    "task_key": task.task_key,
                    "payload": task.payload or {},
                    "priority": task.priority,
                    "status": "claimed",
                    "attempts": (task.attempts or 0) + 1,
                    "max_attempts": task.max_attempts,
                    "scheduled_for": task.scheduled_for or now,
                    "claimed_by": worker_id,
                    "claimed_at": now,
                    "created_at": task.created_at or now,
                    "completed_at": None,
                }
            return None

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._by_id.get(task_id)
        if not entry:
            return None
        task_type, task = entry
        return {
            "id": task_id,
            "task_type": task_type,
            "task_key": task.task_key,
            "payload": task.payload,
            "status": task.status,
            "attempts": task.attempts,
            "max_attempts": task.max_attempts,
        }

    async def update_status(self, task_id: int, status: str) -> None:
        return None  # not meaningful in local mode

    async def update_heartbeat(self, task_id: int) -> None:
        return None

    async def complete_task(
        self,
        task_id: int,
        result_data: dict[str, Any],
        worker_id: str,
        duration: float,
    ) -> None:
        entry = self._pop_in_flight(task_id)
        if not entry:
            return
        _, task = entry
        source = self._sources.get(task.task_type)
        if source is not None:
            await asyncio.to_thread(source.mark_completed, task, result_data)
        logger.info(f"Local task {task_id} completed in {duration:.1f}s")

    async def complete_task_no_data(
        self,
        task_id: int,
        reason: str,
        worker_id: str,
        duration: float,
    ) -> None:
        entry = self._pop_in_flight(task_id)
        if not entry:
            return
        _, task = entry
        source = self._sources.get(task.task_type)
        if source is not None:
            await asyncio.to_thread(source.mark_no_data, task, reason)
        logger.info(f"Local task {task_id} no data: {reason}")

    async def fail_task(
        self,
        task_id: int,
        error: str,
        worker_id: str,
        duration: float,
        retry: bool = True,
    ) -> None:
        entry = self._pop_in_flight(task_id)
        if not entry:
            return
        _, task = entry
        source = self._sources.get(task.task_type)
        if source is not None:
            await asyncio.to_thread(source.mark_failed, task, error, retry)
        logger.warning(f"Local task {task_id} failed (retry={retry}): {error}")

    def _pop_in_flight(
        self, task_id: int
    ) -> tuple[str, ScraperTask] | None:
        return self._by_id.pop(task_id, None)

    # ------------------------------------------------------------------
    # Worker registry — no-op for local mode
    # ------------------------------------------------------------------

    async def register_worker(
        self, worker_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        return None

    async def deactivate_worker(self, worker_id: str) -> None:
        return None

    async def update_worker_heartbeat(
        self, worker_id: str, current_task_id: int | None = None
    ) -> None:
        return None

    async def increment_worker_completed(self, worker_id: str) -> None:
        return None

    # ------------------------------------------------------------------
    # Login interaction — not supported in local mode
    # ------------------------------------------------------------------

    async def set_login_required(
        self, task_id: int, screenshot_data: bytes, phase: str = "qr_scan"
    ) -> None:
        logger.warning(
            "Local task queue cannot persist login screenshots; "
            "check the browser window for the QR code."
        )

    async def update_login_screenshot(
        self, task_id: int, screenshot_url: str
    ) -> None:
        return None

    async def clear_login_screenshot(self, task_id: int) -> None:
        return None

    async def submit_user_input(self, task_id: int, value: str) -> int:
        raise NotImplementedError(
            "Local mode cannot relay user input; use queue-backed mode."
        )

    async def consume_user_input(self, task_id: int) -> str | None:
        return None
