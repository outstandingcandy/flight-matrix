"""Auth-gate coverage for ``/api/v1/user/{email}/*``.

Before this test file existed, every one of the ten user-facing
endpoints was publicly reachable — a non-admin (or unauthenticated)
caller could read another user's filters, settings, cooldowns, and
even dry-run arbitrary filter SQL. See issue #36 for the CVE-shaped
hole.

The fix put a ``_require_self_or_admin`` dependency at router level.
This file is the tripwire that keeps someone from taking it back off:
a non-admin authenticated caller must be able to hit their OWN
``{email}`` route (200) but must be refused (403) on a different
user's route.

Anonymous 401 isn't asserted here because the shared test fixture
runs with ``SKIP_AUTH=true`` — there's no way to represent "no
session" in this harness without rebuilding it. That case is
already exercised transitively by the unit tests in
``tests/auth/`` which construct ``require_login`` in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

# The email a plain non-admin session will identify as. Matches
# ``LOCAL_DEV_EMAIL`` from ``conftest.py`` so the "self" case works
# without also touching that env var.
NON_ADMIN_EMAIL = "test@example.com"


# ---------------------------------------------------------------------------
# The default ``app_client_fastapi`` fixture already spins up a SKIP_AUTH
# TestClient. Its mock user reads ``LOCAL_DEV_GROUPS`` *per request* (see
# ``src/auth/dependencies._mock_user``) — so clearing that env var in each
# test body demotes the caller to a plain user for the request that
# follows. Matches the pattern in ``test_admin_users_route_fastapi.py``.


# ---------------------------------------------------------------------------
# 403 sweep: every endpoint refuses a cross-user request.

# One tuple per (method, path template, body). ``{email}`` is replaced
# with an email the non-admin caller does NOT own.
CROSS_USER_CASES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/user/other@example.com/profile", None),
    ("GET", "/api/user/other@example.com/usage", None),
    ("PUT", "/api/user/other@example.com/settings", {"name": "X"}),
    ("GET", "/api/user/other@example.com/cooldowns", None),
    ("GET", "/api/user/other@example.com/filters", None),
    ("POST", "/api/user/other@example.com/filters", {"name": "n"}),
    ("GET", "/api/user/other@example.com/filters/1", None),
    ("PUT", "/api/user/other@example.com/filters/1", {"name": "n"}),
    ("DELETE", "/api/user/other@example.com/filters/1", None),
    ("POST", "/api/user/other@example.com/filters/test", {"criteria": {}}),
]


@pytest.mark.parametrize(("method", "path", "body"), CROSS_USER_CASES)
def test_cross_user_returns_403(
    app_client_fastapi: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """A non-admin caller hitting a different user's route gets 403.

    This is the whole point of #36. Regressing this — for instance by
    dropping ``_require_self_or_admin`` from the router — should turn
    every one of these into a 200 (or a data-shape error), and this
    parametrisation catches it uniformly.
    """
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "")
    r = app_client_fastapi.request(method, path, json=body)
    assert r.status_code == 403, (
        f"{method} {path} returned {r.status_code}, expected 403 for cross-user. "
        f"The router-level Depends(_require_self_or_admin) may have been removed."
    )
    assert r.json() == {"success": False, "error": "forbidden"}


# ---------------------------------------------------------------------------
# 200 (or benign non-403) on self-scope: same non-admin caller can still
# reach their own ``{email}`` route. We assert "not 403", not "== 200",
# because some of the handlers 404 when there's no seeded user row —
# what matters is that the auth gate isn't rejecting them.


SELF_CASES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", f"/api/user/{NON_ADMIN_EMAIL}/profile", None),
    ("GET", f"/api/user/{NON_ADMIN_EMAIL}/usage", None),
    ("GET", f"/api/user/{NON_ADMIN_EMAIL}/cooldowns", None),
    ("GET", f"/api/user/{NON_ADMIN_EMAIL}/filters", None),
]


@pytest.mark.parametrize(("method", "path", "body"), SELF_CASES)
def test_self_scope_is_not_gated_off(
    app_client_fastapi: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """A non-admin caller CAN reach their own ``{email}`` route.

    The exact status code varies (200 if the mock user exists in the
    tmp DB, 404 if not — both are OK from the auth perspective). What
    must never happen is 403: that would mean the gate is over-eager.
    """
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "")
    r = app_client_fastapi.request(method, path, json=body)
    assert r.status_code != 403, (
        f"{method} {path} returned 403 for the user's OWN email — "
        f"_require_self_or_admin is rejecting legitimate self-access."
    )
