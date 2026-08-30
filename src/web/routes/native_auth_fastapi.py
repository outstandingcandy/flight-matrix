"""Native-client login and self-service endpoints (stage 1d).

Five endpoints, one router, mounted under ``/api``:

- ``POST /api/auth/wechat/login`` — mini-program + iOS App exchange.
- ``POST /api/auth/google/native`` — iOS / Android Google Sign-In.
- ``POST /api/auth/apple/native`` — Sign in with Apple.
- ``GET  /api/me`` — return the caller's user + subscription.
- ``POST /api/me/api-key/rotate`` — self-service api_key rotation.

**Contract with the hosted-UI flow.** Native login endpoints do *not*
seed a session cookie — the mobile client keeps its api_key in
Keychain / ``wx.setStorageSync`` and sends ``Authorization: Bearer
<api_key>`` on every subsequent request. The hosted-UI flow
(``/auth/callback``) is what writes the session cookie. Both paths
converge on the same OIDC-shaped user (see :mod:`src.auth.CLAUDE.md`)
so downstream authorisation reads the same fields.

**Response body**. Login endpoints return
``{"success": True, "api_key": "...", "user": {...UserPublic...}}``.
Errors: ``{"success": False, "error": "..."}`` with HTTP 400 / 401 /
503 depending on cause — the 401 shape matches
:mod:`src.auth.dependencies` so mobile clients can share a single
error handler.

**Free-tier subscription**. All three login endpoints delegate to
``UserService.find_or_create_by_{wechat,google,apple}``, which
guarantees an active free-tier subscription (via
:meth:`_ensure_free_tier_subscription`) before returning. Without this
step the freshly-minted api_key would 401 on the very next request
because :meth:`authenticate_by_api_key` refuses users with no active
subscription.

**Not in this file**:

- User-info refresh from an SDK-provided access_token (WeChat has one;
  we don't need it — we're not calling ``/sns/userinfo``).
- Refresh-token replay for bearer tokens (api_keys don't expire; the
  rotation endpoint is the only replacement path).
- Cross-device session invalidation (out of scope for stage 1).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.auth.dependencies import require_login

logger = logging.getLogger("web.native_auth")

router = APIRouter(prefix="/api/v1", tags=["auth-native"])


# ---------- Request bodies -----------------------------------------------


class WechatLoginRequest(BaseModel):
    """Payload the mini-program / iOS app POSTs.

    ``code`` comes from ``wx.login()`` on mp, from ``SendAuthReq``
    callback on app. ``platform`` picks the AppID / endpoint pair —
    "mp" -> jscode2session, "app" -> oauth2/access_token.
    """

    code: str = Field(min_length=1, description="Weixin one-time code")
    platform: Literal["mp", "app"] = Field(
        default="mp",
        description='"mp" for mini-program, "app" for iOS App',
    )
    name: str | None = Field(
        default=None,
        description="Optional display name (nickname). Not required — WeChat "
        "doesn't hand back a name in the code exchange; the client can pass "
        "one from wx.getUserProfile()/getSelfInfo if the user has consented.",
    )


class GoogleNativeRequest(BaseModel):
    """Payload iOS / Android GoogleSignIn SDKs POST after their local flow.

    ``id_token`` is verified against the JWKS at googleapis.com with an
    ``aud`` allowlist of {web_client_id, ios_client_id,
    android_client_id} — the native SDKs mint tokens whose ``aud`` is
    their *own* client_id, not the web client's, so the allowlist is
    load-bearing.
    """

    id_token: str = Field(min_length=1)


class AppleNativeRequest(BaseModel):
    """Payload iOS AuthenticationServices SDK POSTs.

    Apple's authorization response gives the client both an
    ``identityToken`` (JWT, what we verify) and an
    ``authorizationCode`` (for server-to-server refresh, which we
    don't do). Accept both fields for shape parity with a
    server-to-server flow the app might grow later, but only
    ``identity_token`` is actually consumed today.

    ``name`` is optional and only sent on the *very first* sign-in
    from a given Apple ID — Apple deliberately does not include it in
    the JWT and the client has to pass it through if it wants us to
    persist it.
    """

    identity_token: str = Field(min_length=1)
    authorization_code: str | None = None
    name: str | None = None


# ---------- Helpers ------------------------------------------------------


def _get_user_service(request: Request):
    """Instantiate :class:`UserService` against the request's shared DB."""
    from src.services.user_service import UserService

    return UserService(request.app.state.db_manager)


def _user_public(user) -> dict[str, Any]:
    """Trim the ``User`` ORM object to a body the client should see.

    ``User.to_dict()`` includes ``api_key`` — the login response body
    returns that separately at the top level so this helper strips it
    from the nested ``user`` dict to avoid duplication (and to make
    "don't log user.api_key" a one-line rule downstream).
    """
    d: dict[str, Any] = user.to_dict()
    d.pop("api_key", None)
    return d


def _auth_success(user) -> dict[str, Any]:
    """Shared response body for all three native login endpoints."""
    return {
        "success": True,
        "api_key": user.api_key,
        "user": _user_public(user),
    }


def _unauthenticated(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"success": False, "error": message},
    )


def _service_unavailable(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"success": False, "error": message},
    )


# ---------- Endpoints ----------------------------------------------------


@router.post("/auth/wechat/login")
async def wechat_login(request: Request, body: WechatLoginRequest) -> dict[str, Any]:
    """Exchange a WeChat code for a flight-matrix api_key.

    Match order (:meth:`UserService.find_or_create_by_wechat`):
    ``unionid`` → ``(openid, platform)`` → new row.
    """
    from src.auth.factory import get_wechat_auth

    wechat = get_wechat_auth()
    if wechat is None:
        raise _service_unavailable("wechat auth is not configured")

    session = wechat.code_to_session(body.code, body.platform)
    if session is None:
        raise _unauthenticated("wechat code exchange failed")

    svc = _get_user_service(request)
    user = svc.find_or_create_by_wechat(
        openid=session["openid"],
        unionid=session.get("unionid"),
        platform=body.platform,
        name=body.name,
    )
    if user is None:
        # Database error inside the service. Log level is already ERROR
        # over there; the client gets a plain 500-shaped body but under a
        # 401 code because "we couldn't authenticate you" is what the
        # client can act on.
        raise _unauthenticated("could not resolve wechat account")

    return _auth_success(user)


@router.post("/auth/google/native")
async def google_native(request: Request, body: GoogleNativeRequest) -> dict[str, Any]:
    """Verify a Google-issued id_token and mint a flight-matrix api_key.

    Accepts tokens minted for the web client and any registered native
    client (iOS / Android) — the audience allowlist is in
    :class:`GoogleAuth.__init__`.
    """
    from src.auth.factory import get_google_auth

    google = get_google_auth()
    if google is None:
        raise _service_unavailable("google auth is not configured")

    claims = google.get_user_from_token(body.id_token)
    if claims is None:
        raise _unauthenticated("invalid google id_token")

    sub = claims.get("sub")
    if not sub:
        # get_user_from_token guarantees the OIDC shape includes ``sub``;
        # falling in here means the token was verified but the claim is
        # empty — treat as an auth failure, not a 500.
        raise _unauthenticated("google id_token missing sub")

    svc = _get_user_service(request)
    user = svc.find_or_create_by_google_sub(
        sub=str(sub),
        email=str(claims.get("email") or ""),
        name=claims.get("name"),
    )
    if user is None:
        raise _unauthenticated("could not resolve google account")

    return _auth_success(user)


@router.post("/auth/apple/native")
async def apple_native(request: Request, body: AppleNativeRequest) -> dict[str, Any]:
    """Verify an Apple ``identityToken`` and mint a flight-matrix api_key.

    ``email`` may be absent on all sign-ins after the first one; that's
    handled by :meth:`UserService.find_or_create_by_apple_sub` matching
    on ``apple_sub`` first. ``name`` is only present when the iOS
    client passes it explicitly (Apple never includes it in the JWT).
    """
    from src.auth.factory import get_apple_auth

    apple = get_apple_auth()
    if apple is None:
        raise _service_unavailable("apple auth is not configured")

    claims = apple.get_user_from_token(body.identity_token)
    if claims is None:
        raise _unauthenticated("invalid apple identity_token")

    sub = claims.get("sub")
    if not sub:
        raise _unauthenticated("apple identity_token missing sub")

    svc = _get_user_service(request)
    user = svc.find_or_create_by_apple_sub(
        sub=str(sub),
        email=claims.get("email"),
        # Prefer the JWT's absent-by-design ``name`` fallback (None) and
        # let the client-passed value win when it was included in the
        # first-login payload.
        name=body.name or claims.get("name"),
    )
    if user is None:
        raise _unauthenticated("could not resolve apple account")

    return _auth_success(user)


@router.get("/me")
async def get_me(
    request: Request,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    """Return the caller's user record with active subscription.

    Both bearer and cookie-session callers hit this. For bearer users
    the adapter in :mod:`src.auth.dependencies` has already stapled
    the ``subscription`` blob onto the OIDC dict, so we return that
    directly. For session users we look up the row by email (Google /
    Cognito) — bearer users have their DB id in ``sub``.
    """
    svc = _get_user_service(request)

    sub = user.get("sub", "")
    email = user.get("email", "")

    db_user = None
    # Bearer path: sub is the users.id as a string
    if sub and sub.isdigit():
        db_user = svc.get_user_by_id(int(sub))
    # Cookie-session path: match on email
    if db_user is None and email:
        db_user = svc.get_user_by_email(email)

    if db_user is None:
        # OIDC user with no local row (Cognito / Google first-time cookie
        # login when the wire-up doesn't provision users). Return the
        # in-memory claim shape without a subscription so the client can
        # at least render its own user profile.
        return {"success": True, "user": user, "subscription": None}

    return {
        "success": True,
        "user": _user_public(db_user),
        "subscription": user.get("subscription"),
    }


@router.post("/me/api-key/rotate")
async def rotate_api_key(
    request: Request,
    user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    """Rotate the caller's api_key. Old key stops working immediately.

    Bearer callers rotating their own key MUST update local storage
    before the next request — this endpoint returns the new key in the
    response body; the old one is invalidated in the same transaction.
    Cookie-session callers can rotate too (no bearer needed), which is
    the recovery path for a leaked key.
    """
    svc = _get_user_service(request)

    sub = user.get("sub", "")
    email = user.get("email", "")

    user_id: int | None = None
    if sub and sub.isdigit():
        user_id = int(sub)
    if user_id is None and email:
        db_user = svc.get_user_by_email(email)
        if db_user is not None:
            user_id = db_user.id

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"success": False, "error": "user not found"},
        )

    new_key = svc.regenerate_api_key(user_id)
    if new_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"success": False, "error": "failed to rotate api_key"},
        )

    return {"success": True, "api_key": new_key}
