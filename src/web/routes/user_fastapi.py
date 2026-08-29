"""FastAPI port of the ``/api/user/{email}/*`` routes.

Stage 0 lift-and-shift. Same delegation pattern as the sibling modules
— ``get_multi_user_services`` and the helper functions stay on
``web_app``, this file only owns the framework layer.

Ten endpoints across seven URL paths (some have GET/POST/PUT/DELETE
variants):

- GET  /api/user/{email}/profile
- GET  /api/user/{email}/usage
- PUT  /api/user/{email}/settings
- GET  /api/user/{email}/cooldowns
- GET  /api/user/{email}/filters
- POST /api/user/{email}/filters
- GET  /api/user/{email}/filters/{filter_id}
- PUT  /api/user/{email}/filters/{filter_id}
- DELETE /api/user/{email}/filters/{filter_id}
- POST /api/user/{email}/filters/test
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from sqlalchemy import text

logger = logging.getLogger("web.user")

router = APIRouter(tags=["user"])


def _user_or_404(email: str) -> tuple[Any, Any, Any, Any]:
    """Shared header: resolve services + lookup user, 404 if missing.

    Every handler in this file starts with the same three lines
    (get_multi_user_services → get_user_by_email → 404 if not found).
    Factored out both to shrink the handlers and to keep the 404 body
    identical across them.
    """
    from src.web.service_factory import get_multi_user_services

    us, ss, fs = get_multi_user_services()
    user = us.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail={"success": False, "error": "User not found"})
    return us, ss, fs, user


@router.get("/api/user/{email}/profile", name="api_user_profile")
async def api_user_profile(email: str) -> dict[str, Any]:
    """User profile + subscription features + active filter count.

    Same as ``web_app.py:4832``.
    """
    us, ss, fs, user = _user_or_404(email)
    user_data = us.get_user_with_subscription(user.id)
    features = ss.get_user_features(user.id)
    filters = fs.get_user_filters(user.id, active_only=True)
    return {
        "success": True,
        "user": user_data,
        "features": features,
        "active_filters_count": len(filters),
    }


@router.get("/api/user/{email}/usage", name="api_user_usage")
async def api_user_usage(email: str) -> dict[str, Any]:
    """User usage statistics. Same as ``web_app.py:4861``."""
    _us, ss, _fs, user = _user_or_404(email)
    return {"success": True, "usage": ss.get_usage_stats(user.id)}


@router.put("/api/user/{email}/settings", name="api_user_update_settings")
async def api_user_update_settings(
    email: str,
    data: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Update user's per-subscription feature overrides.

    Same as ``web_app.py:4879``. Keys accepted match the Flask version;
    unknown keys are silently ignored.
    """
    _us, ss, _fs, user = _user_or_404(email)

    subscription = ss.get_user_active_subscription(user.id)
    if not subscription:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "No active subscription"},
        )

    feature_overrides: dict[str, Any] = {}
    if "enable_maps" in data:
        feature_overrides["enable_maps"] = data["enable_maps"]
    if "enable_aircraft_images" in data:
        feature_overrides["enable_aircraft_images"] = data["enable_aircraft_images"]

    if "cooldown_hours" in data:
        value = data["cooldown_hours"]
        feature_overrides["cooldown_hours"] = float(value) if value is not None else 12.0
    if "daily_report_limit" in data:
        value = data["daily_report_limit"]
        feature_overrides["daily_report_limit"] = int(value) if value is not None else -1
    if "monthly_report_limit" in data:
        value = data["monthly_report_limit"]
        feature_overrides["monthly_report_limit"] = int(value) if value is not None else -1
    if "max_filters" in data:
        value = data["max_filters"]
        feature_overrides["max_filters"] = int(value) if value is not None else -1

    if ss.update_subscription(subscription.id, **feature_overrides):
        return {"success": True}
    raise HTTPException(
        status_code=400, detail={"success": False, "error": "Failed to update settings"}
    )


@router.get("/api/user/{email}/cooldowns", name="api_user_cooldowns")
async def api_user_cooldowns(email: str) -> dict[str, Any]:
    """Recent per-aircraft report cooldown windows. Same as ``web_app.py:4930``."""
    from src.web.runtime import db_manager
    from src.web.time_helpers import _to_datetime, _to_iso

    _us, _ss, _fs, user = _user_or_404(email)

    session = db_manager.get_session()
    try:
        result = session.execute(
            text(
                """
                SELECT aircraft_hex, last_report_time, last_latitude, last_longitude, report_count
                FROM user_cooldowns
                WHERE user_id = :user_id
                ORDER BY last_report_time DESC
                LIMIT 20
                """
            ),
            {"user_id": user.id},
        ).fetchall()

        cooldowns: list[dict[str, Any]] = []
        for row in result:
            reported_at = _to_datetime(row[1])
            hours_since = (
                (datetime.now() - reported_at).total_seconds() / 3600 if reported_at else None
            )
            cooldowns.append(
                {
                    "aircraft_hex": row[0],
                    "last_report_time": _to_iso(row[1]),
                    "hours_since_last_report": hours_since,
                    "last_latitude": float(row[2]) if row[2] else None,
                    "last_longitude": float(row[3]) if row[3] else None,
                    "report_count": row[4],
                }
            )
        return {"success": True, "cooldowns": cooldowns}
    finally:
        session.close()


@router.get("/api/user/{email}/filters", name="api_user_list_filters")
async def api_user_list_filters(
    email: str,
    active_only: str = Query("false", description="'true' filters to active-only"),
) -> dict[str, Any]:
    """List a user's filters. Same as ``web_app.py:4982``.

    ``active_only`` is a string, not a typed bool, to match the Flask
    version's parse (any non-'true' value → False).
    """
    _us, _ss, fs, user = _user_or_404(email)
    filters = fs.get_user_filters(user.id, active_only=active_only.lower() == "true")
    return {"success": True, "filters": [f.to_dict() for f in filters]}


@router.post("/api/user/{email}/filters", name="api_user_create_filter")
async def api_user_create_filter(
    email: str,
    data: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Create a new user filter. Same as ``web_app.py:5001``.

    Enforces the subscription's ``max_filters`` cap.
    """
    _us, ss, fs, user = _user_or_404(email)

    features = ss.get_user_features(user.id)
    max_filters = features.get("max_filters", 3)
    current_filters = fs.get_user_filters(user.id, active_only=False)

    if max_filters != -1 and len(current_filters) >= max_filters:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": (
                    f"Filter limit reached ({max_filters}). "
                    "Upgrade your subscription for more filters."
                ),
            },
        )

    name = data.get("name")
    filter_sql = data.get("filter_sql")
    description = data.get("description")
    priority = data.get("priority", 0)

    if not name or not filter_sql:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "Name and filter_sql are required"},
        )

    user_filter, error_msg = fs.create_filter(user.id, name, filter_sql, description, priority)
    if user_filter:
        return {"success": True, "filter": user_filter.to_dict()}
    raise HTTPException(
        status_code=400,
        detail={"success": False, "error": error_msg or "Invalid filter SQL"},
    )


@router.get("/api/user/{email}/filters/{filter_id}", name="api_user_get_filter")
async def api_user_get_filter(email: str, filter_id: int) -> dict[str, Any]:
    """Get one filter by ID. Same as ``web_app.py:5044``. 404 for a
    filter that isn't the current user's."""
    _us, _ss, fs, user = _user_or_404(email)
    user_filter = fs.get_filter(filter_id)
    if not user_filter or user_filter.user_id != user.id:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Filter not found"})
    return {"success": True, "filter": user_filter.to_dict()}


@router.put("/api/user/{email}/filters/{filter_id}", name="api_user_update_filter")
async def api_user_update_filter(
    email: str,
    filter_id: int,
    data: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Update a filter. Same as ``web_app.py:5064``."""
    _us, _ss, fs, user = _user_or_404(email)
    user_filter = fs.get_filter(filter_id)
    if not user_filter or user_filter.user_id != user.id:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Filter not found"})

    if fs.update_filter(
        filter_id,
        name=data.get("name"),
        filter_sql=data.get("filter_sql"),
        description=data.get("description"),
        is_active=data.get("is_active"),
        priority=data.get("priority"),
    ):
        return {"success": True}
    raise HTTPException(
        status_code=400,
        detail={"success": False, "error": "Failed to update filter (invalid SQL?)"},
    )


@router.delete("/api/user/{email}/filters/{filter_id}", name="api_user_delete_filter")
async def api_user_delete_filter(email: str, filter_id: int) -> dict[str, Any]:
    """Delete a filter. Same as ``web_app.py:5099``."""
    _us, _ss, fs, user = _user_or_404(email)
    user_filter = fs.get_filter(filter_id)
    if not user_filter or user_filter.user_id != user.id:
        raise HTTPException(status_code=404, detail={"success": False, "error": "Filter not found"})
    if fs.delete_filter(filter_id):
        return {"success": True}
    raise HTTPException(
        status_code=400, detail={"success": False, "error": "Failed to delete filter"}
    )


@router.post("/api/user/{email}/filters/test", name="api_user_test_filter")
async def api_user_test_filter(
    email: str,
    data: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Dry-run a filter SQL without saving. Same as ``web_app.py:5124``."""
    _us, _ss, fs, _user = _user_or_404(email)

    filter_sql = data.get("filter_sql")
    limit = data.get("limit", 10)
    if not filter_sql:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "filter_sql is required"},
        )

    success, results, error = fs.test_filter(filter_sql, limit)
    if success:
        return {"success": True, "results": results}
    raise HTTPException(status_code=400, detail={"success": False, "error": error})
