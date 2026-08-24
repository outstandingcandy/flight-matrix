"""FastAPI port of the ``/api/aircraft/*`` routes from ``web_app``.

Stage 0 lift-and-shift. Handler bodies keep the exact SQL, response
shape and error-handling behaviour they had in Flask. What changes:

- Framework primitives — ``request.args`` becomes typed FastAPI query
  parameters; ``jsonify({...})`` becomes a plain ``dict`` return with
  FastAPI serialising it.
- Global-state access — ``db_manager`` and helper functions are still
  defined on the ``web_app`` module and read its module-level globals.
  Rather than duplicating them here, migrated handlers delegate: the
  FastAPI lifespan calls ``web_app.init_app()``, so ``web_app.db_manager``
  and ``web_app.config`` are populated the same way they are when the
  Flask entry runs, and ``batch_get_images_from_static_info`` /
  ``transform_image_paths`` / ``convert_utc_to_beijing`` see valid
  globals. When the whole migration is done, those helpers move to a
  neutral module and the Flask module goes away with them.
- Error responses — a bare ``except Exception`` no longer needs to call
  ``api_error(exc, ctx)``; FastAPI's global exception handler in
  :mod:`app` already returns the same client-safe body, and the logger
  captures the traceback with a request-scoped context line.

Only ``/api/aircraft/recent`` is migrated in this commit. It's the
smallest handler in the group and validates the delegation pattern
before the other ~20 routes follow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query

logger = logging.getLogger("web.aircraft")

router = APIRouter(prefix="/api/aircraft", tags=["aircraft"])


@router.get("/recent", name="aircraft_recent")
async def get_recent_aircraft(
    hours: int = Query(1, ge=1, le=168),
    limit: int = Query(50, ge=1, le=1000),
) -> dict[str, Any]:
    """List aircraft seen in the last ``hours`` hours.

    Behaviour identical to the Flask version at ``web_app.py:853`` —
    same SQL, same 'convert to Beijing time' transform, same batch image
    lookup. Query params get bounds via ``Query(..., ge=..., le=...)``
    that the Flask version enforced only implicitly.
    """
    from web_app import (
        batch_get_images_from_static_info,
        convert_utc_to_beijing,
        db_manager,
        transform_image_paths,
    )

    recent_time = datetime.now() - timedelta(hours=hours)
    recent_time_str = recent_time.strftime("%Y-%m-%d %H:%M:%S")
    where_clause = f"snapshot_time >= '{recent_time_str}'"
    logger.info("Recent aircraft query: %s", where_clause)

    results = db_manager.execute_filter_query(where_clause, limit)
    logger.info("Recent query returned %d results", len(results))

    registrations = [r.get("r") for r in results if r.get("r")]
    static_images = batch_get_images_from_static_info(registrations)

    for result in results:
        if result.get("timestamp"):
            result["timestamp"] = convert_utc_to_beijing(result["timestamp"])
        result["timezone"] = "Asia/Shanghai"
        reg = result.get("r")
        if reg and reg in static_images:
            result["image_path_1"] = static_images[reg].get("image_path_1")
            result["image_path_2"] = static_images[reg].get("image_path_2")
            result["image_path_3"] = static_images[reg].get("image_path_3")
        transform_image_paths(result)

    return {"success": True, "data": results, "count": len(results)}
