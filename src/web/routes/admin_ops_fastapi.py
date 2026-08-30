"""FastAPI port of the remaining ``/api/admin/*`` operational routes.

Stage 0 lift-and-shift. Four handlers:

  Scraper monitoring (3, session-gated via require_admin):
    GET /api/admin/scraper/stats
    GET /api/admin/scraper/workers
    GET /api/admin/scraper/recent-tasks

  Bulk import (1, machine-to-machine via X-Admin-Secret header):
    POST /api/admin/import-data

The bulk-import endpoint uses a shared-secret header rather than the
admin session because it's invoked by data-migration scripts that
don't hold a Cognito / Google cookie. It's the same shape as
``/api/ingest/*`` — an authenticated write path with a token, not a
user.
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy import text

from src.auth.dependencies import require_admin

logger = logging.getLogger("web.admin.ops")


# Two routers because two gates. Session-based admin gate for the
# scraper-monitoring endpoints, header-token gate for bulk import.
scraper_router = APIRouter(prefix="/api/v1", tags=["admin-scraper"], dependencies=[Depends(require_admin)])
import_router = APIRouter(prefix="/api/v1", tags=["admin-import"])


@scraper_router.get("/admin/scraper/stats", name="api_admin_scraper_stats")
async def api_admin_scraper_stats() -> dict[str, Any]:
    """Queue depth by status + active-worker count. Same as ``web_app.py:5756``."""
    from src.web.service_factory import get_scraper_db_session

    session = get_scraper_db_session()
    try:
        status_counts_rows = session.execute(
            text(
                """
                SELECT status, COUNT(*) as count
                FROM scraper_tasks
                GROUP BY status
                """
            )
        )
        status_counts = {row.status: row.count for row in status_counts_rows}

        # Compute the 5-minute cutoff in Python — Postgres's NOW() -
        # INTERVAL and SQLite's datetime() have incompatible syntax.
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
        active_workers = (
            session.execute(
                text(
                    """
                    SELECT COUNT(*) as count
                    FROM scraper_workers
                    WHERE status = 'active'
                      AND last_heartbeat > :cutoff
                    """
                ),
                {"cutoff": cutoff},
            )
            .fetchone()
            .count
        )

        pending_rows = session.execute(
            text(
                """
                SELECT task_type, COUNT(*) as count
                FROM scraper_tasks
                WHERE status = 'pending'
                GROUP BY task_type
                """
            )
        )
        pending_by_type = {row.task_type: row.count for row in pending_rows}

        stats = {
            "status_counts": status_counts,
            "active_workers": active_workers,
            "pending_by_type": pending_by_type,
            "total_pending": status_counts.get("pending", 0),
            "total_processing": (
                status_counts.get("claimed", 0) + status_counts.get("processing", 0)
            ),
            "total_completed": status_counts.get("completed", 0),
            "total_no_data": status_counts.get("no_data", 0),
            "total_failed": status_counts.get("failed", 0),
        }
        return {"success": True, "stats": stats}
    finally:
        session.close()


@scraper_router.get("/admin/scraper/workers", name="api_admin_scraper_workers")
async def api_admin_scraper_workers() -> dict[str, Any]:
    """Recent-heartbeat worker list. Same as ``web_app.py:5825``."""
    from src.web.service_factory import get_scraper_db_session
    from src.web.time_helpers import _to_datetime, _to_iso

    session = get_scraper_db_session()
    try:
        result = session.execute(
            text(
                """
                SELECT worker_id, status, last_heartbeat, tasks_completed,
                       current_task_id, metadata
                FROM scraper_workers
                ORDER BY last_heartbeat DESC
                LIMIT 50
                """
            )
        )
        # `seconds_since_heartbeat` computed in Python — no cross-dialect
        # EXTRACT(EPOCH FROM ...).
        now = datetime.now(UTC).replace(tzinfo=None)
        workers: list[dict[str, Any]] = []
        for row in result:
            heartbeat = _to_datetime(row.last_heartbeat)
            secs = (now - heartbeat).total_seconds() if heartbeat else None
            workers.append(
                {
                    "worker_id": row.worker_id,
                    "status": row.status,
                    "last_heartbeat": _to_iso(row.last_heartbeat),
                    "tasks_completed": row.tasks_completed,
                    "current_task_id": row.current_task_id,
                    "metadata": row.metadata or {},
                    "seconds_since_heartbeat": secs,
                }
            )
        return {"success": True, "workers": workers}
    finally:
        session.close()


@scraper_router.get("/admin/scraper/recent-tasks", name="api_admin_scraper_recent_tasks")
async def api_admin_scraper_recent_tasks(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=500),
    status: str | None = Query(None, description="Filter tasks by status"),
) -> dict[str, Any]:
    """Recent scraper task list with duration + last result. Same as
    ``web_app.py:5870``."""
    from src.web.service_factory import get_scraper_db_session
    from src.web.time_helpers import _to_iso

    offset = (page - 1) * limit
    session = get_scraper_db_session()
    try:
        status_clause = ""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            status_clause = "WHERE t.status = :status"
            params["status"] = status

        # Correlated subqueries instead of LATERAL — SQLite doesn't have LATERAL,
        # Postgres accepts both.
        result = session.execute(
            text(
                f"""
                SELECT t.id, t.task_type, t.task_key, t.status, t.attempts,
                       t.max_attempts, t.last_error, t.created_at, t.completed_at,
                       t.claimed_by,
                       (SELECT duration_seconds FROM scraper_results
                        WHERE task_id = t.id
                        ORDER BY created_at DESC LIMIT 1) AS duration_seconds,
                       (SELECT success FROM scraper_results
                        WHERE task_id = t.id
                        ORDER BY created_at DESC LIMIT 1) AS result_success
                FROM scraper_tasks t
                {status_clause}
                ORDER BY COALESCE(t.completed_at, t.created_at) DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        tasks: list[dict[str, Any]] = []
        for row in result:
            tasks.append(
                {
                    "id": row.id,
                    "task_type": row.task_type,
                    "task_key": row.task_key,
                    "status": row.status,
                    "attempts": row.attempts,
                    "max_attempts": row.max_attempts,
                    "last_error": row.last_error,
                    "created_at": _to_iso(row.created_at),
                    "completed_at": _to_iso(row.completed_at),
                    "claimed_by": row.claimed_by,
                    "duration_seconds": (
                        float(row.duration_seconds) if row.duration_seconds else None
                    ),
                }
            )

        total = (
            session.execute(
                text(f"SELECT COUNT(*) as total FROM scraper_tasks t {status_clause}"),
                params,
            )
            .fetchone()
            .total
        )
        pages = (total + limit - 1) // limit

        return {
            "success": True,
            "tasks": tasks,
            "total": total,
            "page": page,
            "pages": pages,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Bulk import — shared-secret gate, not the admin session
# ---------------------------------------------------------------------------


async def verify_admin_secret(
    x_admin_secret: Annotated[str, Header(alias="X-Admin-Secret")] = "",
) -> None:
    """Compare ``X-Admin-Secret`` to the ``ADMIN_SECRET`` env var in
    constant time. 401 when it's missing or wrong.

    Same semantics as the Flask handler's inline check; ``ADMIN_SECRET``
    still defaults to the legacy value the migration scripts already
    know, so no operational change.
    """
    expected = os.environ.get("ADMIN_SECRET", "flight-matrix-admin-2026")
    if not x_admin_secret or not hmac.compare_digest(x_admin_secret, expected):
        raise HTTPException(status_code=401, detail={"success": False, "error": "Unauthorized"})


@import_router.post(
    "/admin/import-data",
    name="import_data_api",
    dependencies=[Depends(verify_admin_secret)],
)
async def import_data_api(data: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Bulk-import many rows across a whitelisted set of tables. Same
    as ``web_app.py:2395``.

    Body:
        ``{"aircraft_snapshots": [{...}, ...], "airports": [...], ...}``

    Rows for unknown tables are skipped with a log line. Row-level
    failures are counted but don't fail the whole request; commits
    every 500 rows so a mid-batch crash keeps most of the write.
    """
    from src.data.models import AircraftSnapshot, AircraftStaticInfo, Airport, GeographicRegion
    from src.web.runtime import db_manager

    if not data:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "Missing request body"}
        )

    table_models = {
        "snapshots": AircraftSnapshot,  # Legacy alias — same target as aircraft_snapshots
        "aircraft_snapshots": AircraftSnapshot,
        "airports": Airport,
        "aircraft_static_info": AircraftStaticInfo,
        "geographic_regions": GeographicRegion,
    }

    session = db_manager.get_session()
    try:
        imported = 0
        errors = 0
        total = 0

        for table_name, records in data.items():
            if table_name not in table_models:
                logger.warning("Unknown table: %s, skipping", table_name)
                continue

            model_class = table_models[table_name]
            total += len(records)

            for record_data in records:
                try:
                    record_data.pop("id", None)

                    if table_name in ("snapshots", "aircraft_snapshots"):
                        if record_data.get("altitude_baro") == "ground":
                            record_data["altitude_baro"] = None
                        if record_data.get("altitude_geom") == "ground":
                            record_data["altitude_geom"] = None

                    obj = model_class(**record_data)
                    session.add(obj)
                    imported += 1

                    if imported % 500 == 0:
                        session.commit()
                except Exception:
                    errors += 1
                    logger.exception("Error importing %s record", table_name)
                    session.rollback()

        session.commit()
        return {"success": True, "imported": imported, "errors": errors, "total": total}
    finally:
        session.close()
