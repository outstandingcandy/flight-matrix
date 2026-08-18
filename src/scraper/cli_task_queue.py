"""One-shot TaskQueue for ``--task`` CLI execution.

Implements :class:`resilient_scraper.queue.TaskQueue` but serves exactly one
task from constructor arguments. After the task is claimed, all subsequent
``claim_task`` calls return ``None`` so the Worker exits its polling loop.

Used by ``python -m src.scraper_main --task <key> --scrapers <type>`` for
quick single-target debugging without hitting a real queue.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timezone
from typing import Any

logger = logging.getLogger("scraper.cli_task_queue")


class CLITaskQueue:
    """Serves a single in-memory task then reports empty."""

    def __init__(
        self,
        task_type: str,
        task_key: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._task_type = task_type
        self._task_key = task_key
        self._payload = payload or {}
        self._claimed = False
        self._task_id = 1
        self._lock = asyncio.Lock()
        self._completed = asyncio.Event()

    async def wait_for_completion(self) -> None:
        """Block until the injected task has been processed."""
        await self._completed.wait()

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
            if self._claimed:
                return None
            if self._task_type not in task_types:
                return None
            self._claimed = True
            logger.info(f"CLITaskQueue serving one-shot task: {self._task_type}:{self._task_key}")
            now = datetime.now(UTC)
            return {
                "id": self._task_id,
                "task_type": self._task_type,
                "task_key": self._task_key,
                "payload": self._payload,
                "priority": 0,
                "status": "claimed",
                "attempts": 1,
                "max_attempts": 1,
                "scheduled_for": now,
                "claimed_by": worker_id,
                "claimed_at": now,
                "created_at": now,
                "completed_at": None,
            }

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        return None

    async def update_status(self, task_id: int, status: str) -> None:
        return None

    async def update_heartbeat(self, task_id: int) -> None:
        return None

    async def complete_task(
        self,
        task_id: int,
        result_data: dict[str, Any],
        worker_id: str,
        duration: float,
    ) -> None:
        logger.info(f"CLI task {task_id} completed in {duration:.1f}s")
        self._completed.set()

    async def complete_task_no_data(
        self,
        task_id: int,
        reason: str,
        worker_id: str,
        duration: float,
    ) -> None:
        logger.info(f"CLI task {task_id} no data: {reason}")
        self._completed.set()

    async def fail_task(
        self,
        task_id: int,
        error: str,
        worker_id: str,
        duration: float,
        retry: bool = True,
    ) -> None:
        logger.error(f"CLI task {task_id} failed: {error}")
        self._completed.set()

    # ------------------------------------------------------------------
    # Worker registry — CLI mode skips DB persistence
    # ------------------------------------------------------------------

    async def register_worker(self, worker_id: str, metadata: dict[str, Any] | None = None) -> None:
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
    # Login interaction — not supported for CLI one-shot
    # ------------------------------------------------------------------

    async def set_login_required(
        self, task_id: int, screenshot_data: bytes, phase: str = "qr_scan"
    ) -> None:
        logger.warning(
            "CLI one-shot mode does not persist login screenshots; "
            "check the browser window for the QR code."
        )

    async def update_login_screenshot(self, task_id: int, screenshot_url: str) -> None:
        return None

    async def clear_login_screenshot(self, task_id: int) -> None:
        return None

    async def submit_user_input(self, task_id: int, value: str) -> int:
        raise NotImplementedError(
            "CLI one-shot mode cannot relay user input; use queue-backed mode "
            "for tasks that need SMS verification."
        )

    async def consume_user_input(self, task_id: int) -> str | None:
        return None
