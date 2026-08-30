"""Integration coverage for the ``/api/v1/admin/users*`` FastAPI routes.

Two things this file is really checking:

1. **The batch 6a CVE fix stays fixed.** The Flask blueprint had lost its
   ``@admin_required`` decorator, meaning any authenticated user could
   list, create, mutate, and rotate api_keys for other users. The
   FastAPI router applies ``Depends(require_admin)`` at the router
   level; if that ever slips off, seven endpoints simultaneously
   regress. The per-endpoint ``test_*_denies_non_admin`` cases assert
   403 for every one of them.
2. Happy-path behaviour of the seven handlers themselves — list +
   pagination, create, get, update (with subscription tier +
   feature-flag overrides), delete, api-key rotation.

Uses the ``app_client_fastapi`` fixture which starts in SKIP_AUTH mode
with a mock admin user. Non-admin tests flip ``LOCAL_DEV_GROUPS`` at
request time (``_get_mock_user`` reads env on each call).
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# CVE regression guard: every route MUST 403 for a non-admin caller.


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/users"),
        ("GET", "/api/v1/admin/users/stats"),
        ("GET", "/api/v1/admin/users/1"),
        ("POST", "/api/v1/admin/users"),
        ("PUT", "/api/v1/admin/users/1"),
        ("DELETE", "/api/v1/admin/users/1"),
        ("POST", "/api/v1/admin/users/1/api-key"),
    ],
)
def test_non_admin_gets_403(
    app_client_fastapi: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    """Blanket assertion: every route rejects a caller without the
    ``admins`` group. Regressed once already (Flask side); this is the
    tripwire.
    """
    # Mock user carries no groups → require_admin should refuse.
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "")

    r = app_client_fastapi.request(method, path, json={} if method in ("POST", "PUT") else None)
    assert r.status_code == 403, (
        f"{method} {path} returned {r.status_code}, expected 403 for non-admin. "
        f"The router-level Depends(require_admin) may have been removed."
    )


# ---------------------------------------------------------------------------
# Happy path — admin caller drives each endpoint.


class TestAdminUsersHappyPath:
    def test_create_list_and_get(self, app_client_fastapi: Any) -> None:
        # Create.
        r_create = app_client_fastapi.post(
            "/api/v1/admin/users",
            json={
                "email": "alice@example.com",
                "name": "Alice",
                "tier": "basic",
                "generate_api_key": True,
            },
        )
        assert r_create.status_code == 200, r_create.text
        body_create = r_create.json()
        assert body_create["success"] is True
        assert body_create["user"]["email"] == "alice@example.com"
        assert body_create["user"]["api_key"]

        user_id = body_create["user"]["id"]

        # List — the just-created row must appear.
        r_list = app_client_fastapi.get("/api/v1/admin/users")
        assert r_list.status_code == 200
        listing = r_list.json()
        assert listing["success"] is True
        assert listing["total"] >= 1
        assert any(u["id"] == user_id for u in listing["users"])

        # Get one.
        r_one = app_client_fastapi.get(f"/api/v1/admin/users/{user_id}")
        assert r_one.status_code == 200
        body_one = r_one.json()
        assert body_one["user"]["email"] == "alice@example.com"
        # get_user_with_subscription hydrates the subscription list.
        assert "subscriptions" in body_one["user"]

    def test_create_rejects_missing_email(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.post("/api/v1/admin/users", json={"name": "no-email"})
        assert r.status_code == 400
        assert r.json() == {"success": False, "error": "Email is required"}

    def test_create_rejects_duplicate_email(self, app_client_fastapi: Any) -> None:
        payload = {"email": "dup@example.com", "tier": "basic"}
        r1 = app_client_fastapi.post("/api/v1/admin/users", json=payload)
        assert r1.status_code == 200

        r2 = app_client_fastapi.post("/api/v1/admin/users", json=payload)
        assert r2.status_code == 400
        assert r2.json()["success"] is False

    def test_stats_reports_counts(self, app_client_fastapi: Any) -> None:
        # Seed two users of different tiers so the counts are meaningful.
        app_client_fastapi.post(
            "/api/v1/admin/users", json={"email": "s1@example.com", "tier": "basic"}
        )
        app_client_fastapi.post(
            "/api/v1/admin/users", json={"email": "s2@example.com", "tier": "premium"}
        )

        r = app_client_fastapi.get("/api/v1/admin/users/stats")
        assert r.status_code == 200
        stats = r.json()["stats"]
        assert stats["total"] >= 2
        assert stats["active"] >= 2
        # premium seeded above should show up
        assert stats["premium"] >= 1

    def test_search_filter(self, app_client_fastapi: Any) -> None:
        app_client_fastapi.post(
            "/api/v1/admin/users", json={"email": "findme@example.com", "name": "Findable"}
        )
        app_client_fastapi.post(
            "/api/v1/admin/users", json={"email": "other@example.com", "name": "Other"}
        )

        r = app_client_fastapi.get("/api/v1/admin/users", params={"search": "findme"})
        assert r.status_code == 200
        emails = [u["email"] for u in r.json()["users"]]
        assert "findme@example.com" in emails
        assert "other@example.com" not in emails

    def test_update_name_and_tier(self, app_client_fastapi: Any) -> None:
        r_create = app_client_fastapi.post(
            "/api/v1/admin/users",
            json={"email": "upd@example.com", "name": "Old", "tier": "basic"},
        )
        user_id = r_create.json()["user"]["id"]

        r_upd = app_client_fastapi.put(
            f"/api/v1/admin/users/{user_id}",
            json={
                "name": "New",
                "subscription": {
                    "tier": "premium",
                    "enable_maps": True,
                    "cooldown_hours": 6.0,
                },
            },
        )
        assert r_upd.status_code == 200
        assert r_upd.json() == {"success": True}

        r_check = app_client_fastapi.get(f"/api/v1/admin/users/{user_id}")
        assert r_check.status_code == 200
        checked = r_check.json()["user"]
        assert checked["name"] == "New"
        active = checked.get("active_subscription")
        assert active is not None
        assert active["tier"] == "premium"

    def test_delete_soft_removes(self, app_client_fastapi: Any) -> None:
        r_create = app_client_fastapi.post("/api/v1/admin/users", json={"email": "gone@example.com"})
        user_id = r_create.json()["user"]["id"]

        r_del = app_client_fastapi.delete(f"/api/v1/admin/users/{user_id}")
        assert r_del.status_code == 200
        assert r_del.json() == {"success": True}

        # Soft delete: row still exists (get returns 200) but status="deleted".
        r_get = app_client_fastapi.get(f"/api/v1/admin/users/{user_id}")
        assert r_get.status_code == 200
        assert r_get.json()["user"]["status"] == "deleted"

    def test_regenerate_api_key(self, app_client_fastapi: Any) -> None:
        r_create = app_client_fastapi.post(
            "/api/v1/admin/users",
            json={"email": "key@example.com", "generate_api_key": True},
        )
        user_id = r_create.json()["user"]["id"]
        old_key = r_create.json()["user"]["api_key"]

        r_new = app_client_fastapi.post(f"/api/v1/admin/users/{user_id}/api-key")
        assert r_new.status_code == 200
        new_key = r_new.json()["api_key"]
        assert new_key
        assert new_key != old_key

    def test_regenerate_api_key_404_for_missing_user(self, app_client_fastapi: Any) -> None:
        # 99999 doesn't exist. UserService.regenerate_api_key returns None
        # → handler raises 400 (same as Flask, for parity).
        r = app_client_fastapi.post("/api/v1/admin/users/99999/api-key")
        assert r.status_code == 400
        assert r.json()["success"] is False
