"""FastAPI dependencies port of :mod:`src.auth.decorators`.

Stage 0 ported the auth surface to FastAPI dependencies with cookie
sessions only. **Stage 1a** — this update — adds the two pieces the
non-browser clients (mobile / mini-program / native App) need:

1. ``Authorization: Bearer <api_key>`` branch in
   ``get_current_user_optional``. Delegates to
   ``UserService.authenticate_by_api_key`` and adapts the returned
   ``users`` row to the OIDC-compat ``{sub, email, email_verified,
   name, role, groups}`` shape that :mod:`src.auth.CLAUDE.md` locks in
   as the contract. Without the adapter, ``require_admin`` and
   ``require_groups`` would silently reject bearer users because
   ``user.to_dict()`` has no ``role`` / ``groups`` keys.
2. JSON 401/403 for API-shaped requests, HTML 302 for browsers. The
   split follows ``_wants_json``: ``request.url.path`` starts with
   ``/api/`` OR ``Accept: application/json`` OR ``X-Requested-With:
   XMLHttpRequest``. Browsers still get bounced to ``/login``; a
   mobile client whose bearer token has expired gets a 401 with a
   parseable body and can re-authenticate instead of following the
   redirect.

Contract kept identical to :mod:`src.auth.decorators`:

- The OIDC user shape (``{sub, email, email_verified, name, role,
  groups}``) is the same for both cookie-session and bearer auth so
  template helpers, ``admin_required``, and ``/auth/debug`` read the
  same fields regardless of how the caller authenticated.
- ``SKIP_AUTH=true`` returns the mock user without touching a provider
  or the ``users`` table.
- Missing/invalid session on a route that requires login and wants
  HTML: 302 to ``/login`` (unchanged from stage 0).

Not in stage 1a (comes with the native login endpoints):
- Refresh-token replay for expiring bearer tokens.
- User row creation on first bearer touch (wechat / google / apple
  native login endpoints are what create the row + free-tier
  subscription; a bearer request against a nonexistent row still 401s
  here).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, HTTPException, Request, status

logger = logging.getLogger("auth.dependencies")


def _skip_auth() -> bool:
    """Same predicate as :func:`src.web.auth_shim.is_auth_skipped`."""
    return "--skip-auth" in sys.argv or os.environ.get("SKIP_AUTH", "").lower() == "true"


def _mock_user() -> dict[str, Any]:
    """Same shape as :func:`src.auth.decorators._get_mock_user`.

    Duplicated here rather than imported to keep the decorators module
    free of any FastAPI imports (so the Flask side keeps working during
    the co-existence window).
    """
    email = os.environ.get("LOCAL_DEV_EMAIL", "dev@example.com")
    groups_raw = os.environ.get("LOCAL_DEV_GROUPS", "").strip()
    groups = [g.strip() for g in groups_raw.split(",") if g.strip()] if groups_raw else []
    role = "admin" if "admins" in groups else "user"
    return {
        "sub": "local-dev",
        "email": email,
        "email_verified": True,
        "name": email.split("@")[0],
        "role": role,
        "groups": groups,
    }


def _wants_json(request: Request) -> bool:
    """True when the caller expects a JSON response rather than a page.

    Three signals accepted, matching the Flask-era convention:
    - Path starts with ``/api/`` (mobile clients, JS ``fetch`` targets).
    - ``Accept: application/json`` header (explicit content negotiation).
    - ``X-Requested-With: XMLHttpRequest`` (legacy XHR clients).

    Everything else is treated as a browser navigation and gets the 302
    to ``/login``.
    """
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept:
        return True
    return request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"


def _adapt_api_key_user(user_dict: dict[str, Any]) -> dict[str, Any]:
    """Convert an ``authenticate_by_api_key`` result to the OIDC shape.

    ``UserService.authenticate_by_api_key`` returns ``User.to_dict()``
    with a ``subscription`` key stapled on — that's ``{id, email, name,
    status, api_key, created_at, updated_at, subscription}``. The rest
    of the auth surface (``admin_required``, ``group_required``,
    ``/auth/debug``, the ``is_admin`` template helper) reads
    ``{sub, email, email_verified, name, role, groups}`` and would
    silently fail against the ``to_dict`` output.

    Bearer users have no group membership yet (the ``users`` table
    doesn't store groups; the cookie-session side gets groups from the
    identity provider's claims). Default to ``role='user'`` /
    ``groups=[]`` — this keeps bearer traffic out of admin routes. If
    admin API access via bearer is ever needed, that's a separate
    ``users.role`` / ``users.groups`` column and lives outside this
    adapter.
    """
    email = str(user_dict.get("email") or "")
    return {
        "sub": str(user_dict.get("id") or ""),
        "email": email,
        # An api-key user must have had their row created by an admin
        # (or by the native-login endpoints in stage 1b), so treat the
        # email as verified. Cognito/Google would set this from a claim
        # they don't have.
        "email_verified": True,
        "name": user_dict.get("name") or (email.split("@")[0] if email else None),
        "role": "user",
        "groups": [],
        # Keep the subscription blob accessible — quota / feature
        # dependencies downstream may need it. This is an extension
        # to the OIDC shape, not a contract change.
        "subscription": user_dict.get("subscription"),
    }


async def get_current_user_optional(request: Request) -> dict[str, Any] | None:
    """Resolve the current user; return ``None`` if unauthenticated.

    Never raises. Cache on ``request.state.current_user`` so a single
    request that goes through several dependencies (e.g. ``require_login``
    then ``require_admin``) only verifies the token once.

    Precedence:

    1. ``SKIP_AUTH=true`` → mock user (short-circuits everything).
    2. ``Authorization: Bearer <api_key>`` header → api-key auth via
       ``UserService.authenticate_by_api_key`` (a 40-64-hex value; other
       ``Authorization`` schemes are ignored so we don't try to
       authenticate an OAuth-style ``Bearer <opaque_JWT>``).
    3. ``id_token`` in the request session → cookie-session auth.
    """
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    if _skip_auth():
        user = _mock_user()
        request.state.current_user = user
        return user

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        # Only accept hex-shaped tokens as api keys — anything longer or
        # containing '.' / '-' is more likely an OIDC JWT and belongs
        # to a different auth flow.
        if token and all(c in "0123456789abcdefABCDEF" for c in token) and len(token) in (40, 64):
            try:
                # Deferred import so a broken UserService doesn't fail
                # module import for cookie-session-only deployments.
                from src.services.user_service import UserService
                from src.web.runtime import db_manager

                svc = UserService(db_manager)
                bearer_user = svc.authenticate_by_api_key(token)
                if bearer_user:
                    adapted = _adapt_api_key_user(bearer_user)
                    request.state.current_user = adapted
                    return adapted
                # Malformed / missing / no-subscription api key: fall
                # through to session-based auth so a browser cookie
                # request that accidentally carried a stale bearer
                # still works via its session.
            except Exception:
                logger.exception("Bearer auth failed; falling back to session")

    id_token = request.session.get("id_token")
    if not id_token:
        return None

    # Deferred import: get_user_from_token pulls in the whole provider
    # chain; keeping it out of module import lets the FastAPI side load
    # even when auth is misconfigured (matches the shim's fallback).
    from src.auth.factory import get_user_from_token

    user = get_user_from_token(id_token)
    if user:
        request.state.current_user = user
        return user
    return None


async def require_login(
    request: Request,
    user: dict[str, Any] | None = Depends(get_current_user_optional),
) -> dict[str, Any]:
    """Reject unauthenticated requests. JSON 401 for API, 302 for HTML.

    JSON-shaped clients (``/api/*`` path, ``Accept: application/json``,
    or ``X-Requested-With: XMLHttpRequest``) get a 401 with a body they
    can parse. Everything else — the browser sitting on ``/dashboard``
    with an expired cookie — still gets the 302 to ``/login`` so the
    user can log in and come back.

    ``next_url`` is only stashed on the HTML redirect path; a JSON 401
    doesn't need it because the client will initiate its own re-auth.
    """
    if user is not None:
        return user

    if _wants_json(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"success": False, "error": "unauthenticated"},
        )

    request.session["next_url"] = str(request.url)
    raise HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": "/login"},
    )


async def require_admin(
    request: Request,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    """Require the ``admins`` group. JSON 403 for API, HTML 403 for pages.

    The 403 body for JSON clients is a parseable ``{success, error}``.
    Browsers get FastAPI's default 403 page — same as the Flask side
    served when the group check failed.
    """
    if user.get("role") == "admin" or "admins" in user.get("groups", []):
        return user

    if _wants_json(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"success": False, "error": "forbidden"},
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def require_groups(allowed: list[str]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory: dependency that requires membership in one of ``allowed``.

    Usage::

        @router.get("/premium")
        async def premium(_user = Depends(require_groups(["premium", "admins"]))):
            ...
    """

    async def _dep(
        request: Request,
        user: dict[str, Any] = Depends(require_login),
    ) -> dict[str, Any]:
        user_groups = set(user.get("groups", []))
        if user_groups.intersection(allowed):
            return user

        if _wants_json(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"success": False, "error": "forbidden"},
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    return _dep


async def optional_login_redirect_response(
    request: Request,
    _user: dict[str, Any] | None = Depends(get_current_user_optional),
) -> None:
    """Placeholder for ``@optional_login`` — currently a no-op.

    Left as a named dependency so migrated routes can express intent
    ``Depends(optional_login_redirect_response)`` and stage 1 can hook the
    branch (e.g. inject the user into context if present) without
    rewriting call sites.
    """
    return None
