"""FastAPI port of :mod:`src.web.routes.auth`.

Stage 0 lift-and-shift: five routes, same OAuth2 flows, same session
keys, same session-cookie contract. No behaviour change beyond swapping
Flask primitives for FastAPI ones — the bearer / JSON-401 / native
mobile login endpoints all belong to stage 1.

Provider asymmetry unchanged and documented in :mod:`src.auth.CLAUDE.md`:

- **Cognito (aws)** exchanges the code *in the browser* because Lambda
  runs in a VPC with no NAT and cannot call Cognito's token endpoint.
  ``/auth/callback`` renders ``auth_callback.html`` which POSTs to
  ``/auth/set-session``.
- **Google (gcp)** exchanges the code server-side inside
  ``/auth/callback``.

Both flows write the same session keys: ``id_token``, ``access_token``,
``refresh_token``.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.auth.dependencies import get_current_user_optional, require_login
from src.web.auth_shim import AUTH_ENABLED

if TYPE_CHECKING:
    from src.auth.base import OIDCProvider

logger = logging.getLogger("web.auth")

router = APIRouter(tags=["auth"])

# Fallback landing page after login/logout. Flask uses
# ``url_for("flight_schedules_page")`` which resolves to the same path;
# once every non-auth route is migrated to FastAPI this becomes
# ``request.url_for("flight_schedules_page")``. Hard-coded now because
# stage 0 migrates routes bottom-up and that name doesn't exist on the
# FastAPI side yet.
FALLBACK_LANDING = "/flight-schedules"


def _get_auth() -> OIDCProvider | None:
    """Lazy getter — provider config is optional and target-dependent."""
    if not AUTH_ENABLED:
        return None
    from src.auth.factory import get_auth_provider

    return get_auth_provider()


def _is_cognito(auth: OIDCProvider | None) -> bool:
    """True when the resolved provider uses the browser-side token exchange."""
    from src.auth.cognito_auth import CognitoAuth

    return isinstance(auth, CognitoAuth)


def _forbidden(request: Request, message: str) -> HTMLResponse:
    """Render the shared ``403.html`` template with the given message.

    Extracted so all five 403 exit paths use the same body and the same
    Jinja context shape. Uses the request-first ``TemplateResponse`` API
    (Starlette 0.29+); the old ``(name, context)`` signature was removed.
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "403.html",
        {"message": message},
        status_code=403,
    )


@router.get("/login", name="auth_login")
async def login(request: Request):
    """Redirect to the identity provider's hosted login page."""
    if not AUTH_ENABLED:
        return _forbidden(request, "Authentication is not enabled")

    auth = _get_auth()
    if not auth:
        return _forbidden(request, "Authentication is not configured")

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    logger.debug("login state generated=%s...", state[:20])

    return RedirectResponse(auth.get_login_url(state=state), status_code=status.HTTP_302_FOUND)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    """OAuth2 callback. Cognito exchanges client-side; Google server-side."""
    if not AUTH_ENABLED:
        return _forbidden(request, "Authentication is not enabled")

    auth = _get_auth()
    if not auth:
        return _forbidden(request, "Authentication is not configured")

    if _is_cognito(auth):
        expected_state = request.session.get("oauth_state", "")
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "auth_callback.html",
            {
                "cognito_domain": auth.domain,  # type: ignore[attr-defined]
                "cognito_client_id": auth.client_id,
                "cognito_client_secret": auth.client_secret or "",
                "cognito_callback_url": auth.callback_url,
                "expected_state": expected_state,
            },
        )

    return _server_side_callback(request, auth)


def _server_side_callback(request: Request, auth: OIDCProvider):
    """Complete the OAuth2 code flow entirely on the server.

    Used by every provider except Cognito. Nothing secret reaches the
    browser.
    """
    error = request.query_params.get("error")
    if error:
        logger.warning("Provider returned an error at callback: %s", error)
        return _forbidden(request, f"Sign-in failed: {error}")

    code = request.query_params.get("code", "")
    if not code:
        return _forbidden(request, "Sign-in failed: no authorization code")

    expected_state = request.session.pop("oauth_state", "")
    if not expected_state or request.query_params.get("state", "") != expected_state:
        logger.warning("OAuth state mismatch at callback")
        return _forbidden(request, "Sign-in failed: invalid state")

    tokens = auth.exchange_code_for_tokens(code)
    if not tokens or not tokens.get("id_token"):
        logger.error("Token exchange returned no id_token")
        return _forbidden(request, "Sign-in failed: token exchange failed")

    id_token = tokens["id_token"]
    user = auth.get_user_from_token(id_token)
    if not user:
        logger.warning("Rejected sign-in: token invalid or account not permitted")
        return _forbidden(request, "This account is not permitted to sign in")

    request.session["id_token"] = id_token
    request.session["access_token"] = tokens.get("access_token")
    if tokens.get("refresh_token"):
        request.session["refresh_token"] = tokens["refresh_token"]

    logger.info("auth success user=%s groups=%s", user.get("email"), user.get("groups", []))

    next_url = request.session.pop("next_url", None) or FALLBACK_LANDING
    return RedirectResponse(next_url, status_code=status.HTTP_302_FOUND)


@router.post("/auth/set-session", name="auth_set_session")
async def auth_set_session(request: Request) -> JSONResponse:
    """Set server session from the Cognito client-side token-exchange result."""
    if not AUTH_ENABLED:
        return JSONResponse({"error": "Authentication is not enabled"}, status_code=403)

    auth = _get_auth()
    if not auth:
        return JSONResponse({"error": "Authentication is not configured"}, status_code=403)

    try:
        data: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "No token data provided"}, status_code=400)

    if not isinstance(data, dict):
        return JSONResponse({"error": "No token data provided"}, status_code=400)

    id_token = data.get("id_token")
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not id_token:
        return JSONResponse({"error": "No id_token provided"}, status_code=400)

    user = auth.get_user_from_token(id_token)
    if not user:
        logger.error("Failed to verify id_token")
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    request.session.pop("oauth_state", None)
    request.session["id_token"] = id_token
    request.session["access_token"] = access_token
    request.session["refresh_token"] = refresh_token

    logger.info("auth success user=%s groups=%s", user.get("email"), user.get("groups", []))

    next_url = request.session.pop("next_url", None) or FALLBACK_LANDING
    return JSONResponse(
        {
            "success": True,
            "redirect_url": next_url,
            "user": {"email": user.get("email"), "groups": user.get("groups", [])},
        }
    )


@router.get("/logout", name="auth_logout")
async def logout(request: Request):
    """Clear the session, then hand off to the provider's logout if it has one."""
    user_email: str | None = None
    if AUTH_ENABLED:
        user = await get_current_user_optional(request)
        if user:
            user_email = user.get("email")

    request.session.clear()
    if user_email:
        logger.info("User logged out: %s", user_email)

    if AUTH_ENABLED:
        auth = _get_auth()
        if auth:
            # Google returns "" here — no RP-initiated logout, so clearing
            # the session above is the whole story for that provider.
            logout_url = auth.get_logout_url()
            if logout_url:
                return RedirectResponse(logout_url, status_code=status.HTTP_302_FOUND)

    return RedirectResponse(FALLBACK_LANDING, status_code=status.HTTP_302_FOUND)


@router.get("/auth/debug", name="auth_debug")
async def auth_debug(
    request: Request,
    user: dict[str, Any] = Depends(require_login),
) -> JSONResponse:
    """Show the current user's verified token claims. Login-gated."""
    resolved_provider = "none"
    groups_source = "env"
    if AUTH_ENABLED:
        from src.auth.factory import groups_source as _groups_source
        from src.auth.factory import resolve_auth_provider_name

        resolved_provider = resolve_auth_provider_name()
        groups_source = _groups_source()

    raw_claims: dict[str, Any] | None = None
    id_token = request.session.get("id_token")
    if id_token and AUTH_ENABLED:
        auth = _get_auth()
        if auth:
            raw_claims = auth.verify_token(id_token)

    return JSONResponse(
        {
            "user": user,
            "resolved_provider": resolved_provider,
            "groups_source": groups_source,
            "raw_claims": raw_claims,
            "groups_from_user": user.get("groups", []),
            "cognito_groups_from_claims": (
                raw_claims.get("cognito:groups", []) if raw_claims else None
            ),
        }
    )
