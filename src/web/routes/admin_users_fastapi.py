"""FastAPI port of the ``/api/admin/users*`` routes.

Stage 0 lift-and-shift, plus **one deliberate fix**: every handler
here is gated with ``Depends(require_admin)``. In ``web_app.py`` the
seven ``/api/admin/users*`` routes lost their ``@admin_required``
decorator at some point during the blueprint split — the sibling
``/api/admin/aircraft*`` group has it, this group didn't. That's a
CVE-shaped hole: any authenticated user (even a free-tier one) could
list, create, mutate and rotate API keys for other users. The migration
closes it.

Seven handlers across four URL paths:

- GET  /api/admin/users
- POST /api/admin/users
- GET  /api/admin/users/stats
- GET  /api/admin/users/{user_id}
- PUT  /api/admin/users/{user_id}
- DELETE /api/admin/users/{user_id}
- POST /api/admin/users/{user_id}/api-key
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from src.auth.dependencies import require_admin

logger = logging.getLogger("web.admin.users")

router = APIRouter(prefix="/api/v1", tags=["admin-users"], dependencies=[Depends(require_admin)])


@router.get("/admin/users", name="api_admin_list_users")
async def api_admin_list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=500),
    status: str | None = Query(None),
    search: str | None = Query(None),
    tier: str | None = Query(None),
) -> dict[str, Any]:
    """List users with pagination, plus optional ``search`` (email/name
    substring) and ``tier`` filters. Same query surface as
    ``web_app.py:3181``."""
    from src.web.service_factory import get_multi_user_services

    us, _ss, _fs = get_multi_user_services()

    offset = (page - 1) * limit
    users = us.list_users(status=status, limit=limit, offset=offset)

    if search:
        search_lower = search.lower()
        users = [
            u
            for u in users
            if search_lower in (u.get("email") or "").lower()
            or search_lower in (u.get("name") or "").lower()
        ]

    if tier:
        users = [u for u in users if u.get("subscription", {}).get("tier") == tier]

    total = us.get_user_count(status=status)
    pages = (total + limit - 1) // limit

    return {
        "success": True,
        "users": users,
        "total": total,
        "page": page,
        "pages": pages,
    }


@router.get("/admin/users/stats", name="api_admin_user_stats")
async def api_admin_user_stats() -> dict[str, Any]:
    """Users total / active / premium / enterprise counts.

    Same as ``web_app.py:3221`` — tier counts still done via a full
    ``list_users(limit=10000)`` scan; a later optimisation, not
    stage 0's concern.
    """
    from src.web.service_factory import get_multi_user_services

    us, _ss, _fs = get_multi_user_services()

    total = us.get_user_count()
    active = us.get_user_count(status="active")

    all_users = us.list_users(limit=10000)
    premium = len([u for u in all_users if u.get("subscription", {}).get("tier") == "premium"])
    enterprise = len(
        [u for u in all_users if u.get("subscription", {}).get("tier") == "enterprise"]
    )

    return {
        "success": True,
        "stats": {
            "total": total,
            "active": active,
            "premium": premium,
            "enterprise": enterprise,
        },
    }


@router.post("/admin/users", name="api_admin_create_user")
async def api_admin_create_user(data: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create a new user. Same as ``web_app.py:3253``."""
    from src.web.service_factory import get_multi_user_services

    us, _ss, _fs = get_multi_user_services()

    email = data.get("email")
    name = data.get("name")
    tier = data.get("tier", "basic")
    generate_api_key = data.get("generate_api_key", False)

    if not email:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "Email is required"}
        )

    user = us.create_user(email, name, tier, generate_api_key)
    if user:
        return {"success": True, "user": user.to_dict()}
    raise HTTPException(
        status_code=400,
        detail={
            "success": False,
            "error": "Failed to create user (email may already exist)",
        },
    )


@router.get("/admin/users/{user_id}", name="api_admin_get_user")
async def api_admin_get_user(user_id: int) -> dict[str, Any]:
    """Get a user's full profile + subscription. Same as ``web_app.py:3280``."""
    from src.web.service_factory import get_multi_user_services

    us, _ss, _fs = get_multi_user_services()
    user = us.get_user_with_subscription(user_id)
    if user:
        return {"success": True, "user": user}
    raise HTTPException(status_code=404, detail={"success": False, "error": "User not found"})


@router.put("/admin/users/{user_id}", name="api_admin_update_user")
async def api_admin_update_user(
    user_id: int,
    data: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Update user profile + subscription. Same as ``web_app.py:3296``.

    Empty strings in numeric subscription fields (``cooldown_hours``,
    ``daily_report_limit``, ``monthly_report_limit``, ``max_filters``)
    are treated as "reset to default", matching the Flask version's
    ``value != ""`` guard.
    """
    from src.web.service_factory import get_multi_user_services

    us, ss, _fs = get_multi_user_services()

    if not us.update_user(user_id, name=data.get("name"), status=data.get("status")):
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "Failed to update user"}
        )

    subscription_data = data.get("subscription")
    if subscription_data:
        subscription = ss.get_user_active_subscription(user_id)
        if subscription:
            feature_overrides: dict[str, Any] = {}

            if "enable_maps" in subscription_data:
                feature_overrides["enable_maps"] = subscription_data["enable_maps"]
            if "enable_aircraft_images" in subscription_data:
                feature_overrides["enable_aircraft_images"] = subscription_data[
                    "enable_aircraft_images"
                ]

            if "cooldown_hours" in subscription_data:
                value = subscription_data["cooldown_hours"]
                feature_overrides["cooldown_hours"] = (
                    float(value) if value is not None and value != "" else 12.0
                )
            if "daily_report_limit" in subscription_data:
                value = subscription_data["daily_report_limit"]
                feature_overrides["daily_report_limit"] = (
                    int(value) if value is not None and value != "" else -1
                )
            if "monthly_report_limit" in subscription_data:
                value = subscription_data["monthly_report_limit"]
                feature_overrides["monthly_report_limit"] = (
                    int(value) if value is not None and value != "" else -1
                )
            if "max_filters" in subscription_data:
                value = subscription_data["max_filters"]
                feature_overrides["max_filters"] = (
                    int(value) if value is not None and value != "" else -1
                )

            tier = subscription_data.get("tier")
            ss.update_subscription(subscription.id, tier=tier, **feature_overrides)

    return {"success": True}


@router.delete("/admin/users/{user_id}", name="api_admin_delete_user")
async def api_admin_delete_user(user_id: int) -> dict[str, Any]:
    """Soft-delete a user. Same as ``web_app.py:3359`` (``hard_delete=False``)."""
    from src.web.service_factory import get_multi_user_services

    us, _ss, _fs = get_multi_user_services()
    if us.delete_user(user_id, hard_delete=False):
        return {"success": True}
    raise HTTPException(
        status_code=400, detail={"success": False, "error": "Failed to delete user"}
    )


@router.post("/admin/users/{user_id}/api-key", name="api_admin_regenerate_api_key")
async def api_admin_regenerate_api_key(user_id: int) -> dict[str, Any]:
    """Regenerate a user's API key. Same as ``web_app.py:3375``."""
    from src.web.service_factory import get_multi_user_services

    us, _ss, _fs = get_multi_user_services()
    new_key = us.regenerate_api_key(user_id)
    if new_key:
        return {"success": True, "api_key": new_key}
    raise HTTPException(
        status_code=400,
        detail={"success": False, "error": "Failed to regenerate API key"},
    )
