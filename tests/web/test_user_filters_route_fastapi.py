"""Filter-CRUD coverage for the ``/api/v1/user/{email}/filters*`` routes.

The user route file is 10 endpoints across 7 paths — the smoke sweep in
:mod:`tests.web.test_route_smoke_fastapi` covers the read-only GETs.
This file covers the mutations plus the trickier state transitions:

- Filter CRUD (POST / PUT / DELETE)
- ``/filters/test`` dry-run of arbitrary SQL
- ``/settings`` PUT (subscription feature overrides)
- Cross-user access: filter 42 owned by user A returns 404 for user B
- Filter-limit enforcement — the ``max_filters`` cap actually applies
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers


def _seed_user(client: Any, email: str, *, tier: str = "premium") -> int:
    """Create a user via /api/admin/users and return the numeric id.

    Uses ``premium`` by default because that tier has ``max_filters=-1``
    (unlimited) in ``config/subscription.yaml``; the ``basic`` cap is
    small and would foul the CRUD happy path unintentionally. The tier
    cap itself is exercised in :func:`test_max_filters_cap_rejects_over_limit`.
    """
    r = client.post(
        "/api/v1/admin/users",
        json={"email": email, "tier": tier, "generate_api_key": False},
    )
    assert r.status_code == 200, r.text
    return int(r.json()["user"]["id"])


def _valid_filter_sql() -> str:
    """A WHERE clause that survives the dangerous-pattern check *and*
    the trial ``EXPLAIN`` against ``aircraft_snapshots``. Uses only
    columns known to exist on that table.
    """
    return "is_military = 1 AND hex IS NOT NULL"


# ---------------------------------------------------------------------------
# Missing user → 404


def test_missing_user_returns_404(app_client_fastapi: Any) -> None:
    r = app_client_fastapi.get("/api/v1/user/ghost@example.com/profile")
    assert r.status_code == 404
    assert r.json() == {"success": False, "error": "User not found"}


# ---------------------------------------------------------------------------
# Filter CRUD


class TestFilterCRUD:
    def test_create_valid_filter_then_list_it(self, app_client_fastapi: Any) -> None:
        email = "crud@example.com"
        _seed_user(app_client_fastapi, email)

        r_create = app_client_fastapi.post(
            f"/api/v1/user/{email}/filters",
            json={
                "name": "military",
                "filter_sql": _valid_filter_sql(),
                "description": "military aircraft",
                "priority": 5,
            },
        )
        assert r_create.status_code == 200, r_create.text
        created = r_create.json()["filter"]
        assert created["name"] == "military"
        assert created["priority"] == 5
        filter_id = created["id"]

        r_list = app_client_fastapi.get(f"/api/v1/user/{email}/filters")
        assert r_list.status_code == 200
        assert any(f["id"] == filter_id for f in r_list.json()["filters"])

    def test_create_missing_fields_returns_400(self, app_client_fastapi: Any) -> None:
        email = "missing@example.com"
        _seed_user(app_client_fastapi, email)

        r = app_client_fastapi.post(f"/api/v1/user/{email}/filters", json={"name": "only-name"})
        assert r.status_code == 400
        assert r.json() == {
            "success": False,
            "error": "Name and filter_sql are required",
        }

    def test_create_rejects_dangerous_sql(self, app_client_fastapi: Any) -> None:
        email = "danger@example.com"
        _seed_user(app_client_fastapi, email)

        r = app_client_fastapi.post(
            f"/api/v1/user/{email}/filters",
            json={"name": "evil", "filter_sql": "1=1; DROP TABLE users"},
        )
        assert r.status_code == 400
        # `;` is caught before DROP because DANGEROUS_PATTERNS lists them
        # in different orders; assert that the response mentions "prohibited".
        assert "prohibited" in r.json()["error"].lower()

    def test_update_filter(self, app_client_fastapi: Any) -> None:
        email = "update@example.com"
        _seed_user(app_client_fastapi, email)
        create = app_client_fastapi.post(
            f"/api/v1/user/{email}/filters",
            json={"name": "orig", "filter_sql": _valid_filter_sql()},
        )
        filter_id = create.json()["filter"]["id"]

        r_upd = app_client_fastapi.put(
            f"/api/v1/user/{email}/filters/{filter_id}",
            json={"name": "renamed", "is_active": False, "priority": 9},
        )
        assert r_upd.status_code == 200
        assert r_upd.json() == {"success": True}

        r_get = app_client_fastapi.get(f"/api/v1/user/{email}/filters/{filter_id}")
        got = r_get.json()["filter"]
        assert got["name"] == "renamed"
        assert got["priority"] == 9
        assert got["is_active"] is False

    def test_delete_filter(self, app_client_fastapi: Any) -> None:
        email = "delete@example.com"
        _seed_user(app_client_fastapi, email)
        create = app_client_fastapi.post(
            f"/api/v1/user/{email}/filters",
            json={"name": "doomed", "filter_sql": _valid_filter_sql()},
        )
        filter_id = create.json()["filter"]["id"]

        r_del = app_client_fastapi.delete(f"/api/v1/user/{email}/filters/{filter_id}")
        assert r_del.status_code == 200

        # 404 on subsequent GET.
        r_get = app_client_fastapi.get(f"/api/v1/user/{email}/filters/{filter_id}")
        assert r_get.status_code == 404

    def test_cross_user_filter_access_returns_404(self, app_client_fastapi: Any) -> None:
        """User A creates a filter; user B trying to read it MUST 404.

        The check is done by the handler comparing ``user_filter.user_id
        != user.id``. If that check ever weakens, users see (or can
        mutate) each other's filters.
        """
        _seed_user(app_client_fastapi, "owner@example.com")
        _seed_user(app_client_fastapi, "outsider@example.com")

        create = app_client_fastapi.post(
            "/api/v1/user/owner@example.com/filters",
            json={"name": "mine", "filter_sql": _valid_filter_sql()},
        )
        assert create.status_code == 200
        filter_id = create.json()["filter"]["id"]

        # Outsider fetches → 404 (never 200, never someone-else's-data).
        r_get = app_client_fastapi.get(f"/api/v1/user/outsider@example.com/filters/{filter_id}")
        assert r_get.status_code == 404

        # Outsider PUT → 404
        r_put = app_client_fastapi.put(
            f"/api/v1/user/outsider@example.com/filters/{filter_id}",
            json={"name": "hijacked"},
        )
        assert r_put.status_code == 404

        # Outsider DELETE → 404
        r_del = app_client_fastapi.delete(f"/api/v1/user/outsider@example.com/filters/{filter_id}")
        assert r_del.status_code == 404

        # Owner still sees intact filter.
        r_owner = app_client_fastapi.get(f"/api/v1/user/owner@example.com/filters/{filter_id}")
        assert r_owner.status_code == 200
        assert r_owner.json()["filter"]["name"] == "mine"


# ---------------------------------------------------------------------------
# max_filters cap enforcement


def test_max_filters_cap_rejects_over_limit(app_client_fastapi: Any) -> None:
    """A subscription's ``max_filters`` cap is enforced by
    ``api_user_create_filter``. Enforcement is load-bearing to the
    paid-tier upsell so this test is the guard.

    Note the cap must be *explicitly* set on the subscription row —
    ``UserService.create_user`` writes ``tier="basic"`` but leaves the
    numeric limits at the model defaults (``-1``, unlimited). That's a
    latent design quirk (the yaml tier defaults aren't applied on user
    creation), but not this test's concern; we set the cap directly
    via the admin PUT so we're checking the *enforcement*, not the
    *provisioning*.
    """
    email = "cap@example.com"
    user_id = _seed_user(app_client_fastapi, email, tier="basic")

    # Pin the cap explicitly to 3 via the admin PUT.
    r_cap = app_client_fastapi.put(
        f"/api/v1/admin/users/{user_id}",
        json={"subscription": {"max_filters": 3}},
    )
    assert r_cap.status_code == 200, r_cap.text

    for i in range(3):
        r = app_client_fastapi.post(
            f"/api/v1/user/{email}/filters",
            json={"name": f"cap-{i}", "filter_sql": _valid_filter_sql()},
        )
        assert r.status_code == 200, f"filter {i}: {r.text}"

    # Fourth create must trip the cap.
    r_over = app_client_fastapi.post(
        f"/api/v1/user/{email}/filters",
        json={"name": "cap-3", "filter_sql": _valid_filter_sql()},
    )
    assert r_over.status_code == 400
    assert "limit reached" in r_over.json()["error"].lower()


# ---------------------------------------------------------------------------
# /filters/test dry-run


def test_filter_dry_run_accepts_valid_sql(app_client_fastapi: Any) -> None:
    email = "dryrun@example.com"
    _seed_user(app_client_fastapi, email)

    r = app_client_fastapi.post(
        f"/api/v1/user/{email}/filters/test",
        json={"filter_sql": _valid_filter_sql(), "limit": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    # empty DB → empty results, but the shape is what we care about
    assert "results" in body


def test_filter_dry_run_rejects_bad_sql(app_client_fastapi: Any) -> None:
    email = "dryrun-bad@example.com"
    _seed_user(app_client_fastapi, email)

    r = app_client_fastapi.post(
        f"/api/v1/user/{email}/filters/test",
        json={"filter_sql": "SELECT * FROM users; DROP TABLE users"},
    )
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_filter_dry_run_missing_sql_returns_400(app_client_fastapi: Any) -> None:
    email = "dryrun-empty@example.com"
    _seed_user(app_client_fastapi, email)

    r = app_client_fastapi.post(f"/api/v1/user/{email}/filters/test", json={"limit": 5})
    assert r.status_code == 400
    assert r.json() == {"success": False, "error": "filter_sql is required"}


# ---------------------------------------------------------------------------
# Settings PUT


def test_settings_put_updates_features(app_client_fastapi: Any) -> None:
    email = "settings@example.com"
    _seed_user(app_client_fastapi, email)

    r = app_client_fastapi.put(
        f"/api/v1/user/{email}/settings",
        json={
            "enable_maps": True,
            "enable_aircraft_images": False,
            "cooldown_hours": 3.5,
            "daily_report_limit": 100,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"success": True}

    # Re-fetch profile — the returned features reflect the update.
    r_profile = app_client_fastapi.get(f"/api/v1/user/{email}/profile")
    assert r_profile.status_code == 200
    features = r_profile.json()["features"]
    assert features["enable_maps"] is True
    assert features["enable_aircraft_images"] is False
    assert float(features["cooldown_hours"]) == 3.5
    assert int(features["daily_report_limit"]) == 100


def test_settings_put_missing_user_returns_404(app_client_fastapi: Any) -> None:
    r = app_client_fastapi.put(
        "/api/v1/user/nobody@example.com/settings",
        json={"enable_maps": True},
    )
    assert r.status_code == 404
