"""FastAPI dependencies port of :mod:`src.auth.decorators`.

Stage 0 of the migration ports the auth surface to FastAPI dependencies
one behaviour at a time. **This first version deliberately does NOT do**
the bearer-token / API-key branch or the ``Accept: application/json`` →
401 JSON split — those belong to stage 1 of the plan and would change
observable behaviour, which is exactly what a stage 0 lift-and-shift
must not do.

Contract kept identical to :mod:`src.auth.decorators`:

- Reads the id_token from the request session, verifies it with the
  active provider (via ``src.auth.factory.get_user_from_token``), returns
  the same OIDC user shape (``{sub, email, email_verified, name, role,
  groups}``) so template helpers, ``admin_required``, and ``/auth/debug``
  see the same fields as before.
- ``SKIP_AUTH=true`` returns the mock user without touching a provider.
- Missing/invalid session on a route that requires login: 302 to
  ``/login`` (the same behaviour a Flask ``login_required`` produced).

Stage 1 (later) will:
  1. Add the ``Authorization: Bearer <api_key>`` branch, adapting
     ``UserService.authenticate_by_api_key`` output to the OIDC shape.
  2. Return JSON 401 when the request wants JSON (path starts with
     ``/api/`` or ``Accept: application/json``); keep the 302 for HTML.
  3. Add refresh-token replay in ``get_current_user_optional``.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

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


async def get_current_user_optional(request: Request) -> dict[str, Any] | None:
    """Resolve the current user from session; return None if unauthenticated.

    Never raises. Cache on ``request.state.current_user`` so a single
    request that goes through several dependencies (e.g. ``require_login``
    then ``require_admin``) only verifies the token once.
    """
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    if _skip_auth():
        user = _mock_user()
        request.state.current_user = user
        return user

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
    """Reject unauthenticated requests with a 302 to ``/login``.

    Stage 1 will branch on ``Accept: application/json`` / path prefix to
    return JSON 401 for API clients. For now the behaviour matches Flask
    ``login_required`` exactly: unconditional redirect.
    """
    if user is not None:
        return user

    # Preserve the browser's target so we can bounce back after login.
    request.session["next_url"] = str(request.url)

    # FastAPI's HTTPException doesn't natively support 302 with a Location
    # header; raise it and rely on a Response return in the caller — but
    # for a dependency we have to raise. Convert to RedirectResponse via
    # the exception's headers; Starlette will turn this into a normal
    # 302 with body "Temporary Redirect".
    raise HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": "/login"},
    )


async def require_admin(
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    """Require the ``admins`` group. 403 with the same body Flask used."""
    if user.get("role") == "admin" or "admins" in user.get("groups", []):
        return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def require_groups(allowed: list[str]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory: dependency that requires membership in one of ``allowed``.

    Usage::

        @router.get("/premium")
        async def premium(_user = Depends(require_groups(["premium", "admins"]))):
            ...
    """

    async def _dep(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
        user_groups = set(user.get("groups", []))
        if user_groups.intersection(allowed):
            return user
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
