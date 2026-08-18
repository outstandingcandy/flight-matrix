"""Authentication blueprint: login, callback, logout, session, debug.

Five routes total, serving two different OAuth2 flows depending on which
provider `src/auth/factory.py` resolves for the active deployment target:

- **Cognito (aws)** — the code-for-token exchange happens *in the browser*.
  `/auth/callback` renders `auth_callback.html`, which exchanges the code and
  POSTs the tokens back to `/auth/set-session`. This exists because Lambda runs
  inside a VPC (required to reach Aurora) with no NAT, so it cannot call the
  Cognito token endpoint itself. Documented as a known asymmetry in
  docs/deployment.md; changing it needs a NAT Gateway.
- **Google (gcp)** — the exchange happens server-side inside `/auth/callback`.
  No template, no secret in the page source.

`/auth/set-session` and `auth_callback.html` therefore stay: they are the
Cognito path, not dead code.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from src.web.auth_shim import AUTH_ENABLED

if TYPE_CHECKING:
    from src.auth.base import OIDCProvider

logger = logging.getLogger("web.auth")
bp = Blueprint("auth", __name__)


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


@bp.route("/login")
def login():
    """Redirect to the identity provider's hosted login page."""
    if not AUTH_ENABLED:
        return render_template("403.html", message="Authentication is not enabled"), 403

    auth = _get_auth()
    if not auth:
        return render_template("403.html", message="Authentication is not configured"), 403

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session.modified = True
    logger.debug("login state generated=%s...", state[:20])

    return redirect(auth.get_login_url(state=state))


@bp.route("/auth/callback")
def auth_callback():
    """OAuth2 callback. Cognito exchanges client-side; Google server-side."""
    if not AUTH_ENABLED:
        return render_template("403.html", message="Authentication is not enabled"), 403

    auth = _get_auth()
    if not auth:
        return render_template("403.html", message="Authentication is not configured"), 403

    if _is_cognito(auth):
        expected_state = session.get("oauth_state", "")
        return render_template(
            "auth_callback.html",
            cognito_domain=auth.domain,  # type: ignore[attr-defined]
            cognito_client_id=auth.client_id,
            cognito_client_secret=auth.client_secret or "",
            cognito_callback_url=auth.callback_url,
            expected_state=expected_state,
        )

    return _server_side_callback(auth)


def _server_side_callback(auth: OIDCProvider):
    """Complete the OAuth2 code flow entirely on the server.

    Used by every provider except Cognito. Nothing secret reaches the browser.

    Args:
        auth: The resolved identity provider.

    Returns:
        A redirect to the post-login destination, or a 403 page.
    """
    error = request.args.get("error")
    if error:
        logger.warning("Provider returned an error at callback: %s", error)
        return render_template("403.html", message=f"Sign-in failed: {error}"), 403

    code = request.args.get("code", "")
    if not code:
        return render_template("403.html", message="Sign-in failed: no authorization code"), 403

    expected_state = session.pop("oauth_state", "")
    if not expected_state or request.args.get("state", "") != expected_state:
        logger.warning("OAuth state mismatch at callback")
        return render_template("403.html", message="Sign-in failed: invalid state"), 403

    tokens = auth.exchange_code_for_tokens(code)
    if not tokens or not tokens.get("id_token"):
        logger.error("Token exchange returned no id_token")
        return render_template("403.html", message="Sign-in failed: token exchange failed"), 403

    id_token = tokens["id_token"]
    user = auth.get_user_from_token(id_token)
    if not user:
        # Either the token is invalid or the account is outside allowed_domains.
        logger.warning("Rejected sign-in: token invalid or account not permitted")
        return render_template("403.html", message="This account is not permitted to sign in"), 403

    session["id_token"] = id_token
    session["access_token"] = tokens.get("access_token")
    if tokens.get("refresh_token"):
        session["refresh_token"] = tokens["refresh_token"]
    session.permanent = True

    logger.info("auth success user=%s groups=%s", user.get("email"), user.get("groups", []))

    next_url = session.pop("next_url", None) or url_for("flight_schedules_page")
    return redirect(next_url)


@bp.route("/auth/set-session", methods=["POST"])
def auth_set_session():
    """Set server session from the Cognito client-side token-exchange result."""
    if not AUTH_ENABLED:
        return jsonify({"error": "Authentication is not enabled"}), 403

    auth = _get_auth()
    if not auth:
        return jsonify({"error": "Authentication is not configured"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "No token data provided"}), 400

    id_token = data.get("id_token")
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not id_token:
        return jsonify({"error": "No id_token provided"}), 400

    user = auth.get_user_from_token(id_token)
    if not user:
        logger.error("Failed to verify id_token")
        return jsonify({"error": "Invalid token"}), 401

    session.pop("oauth_state", None)
    session["id_token"] = id_token
    session["access_token"] = access_token
    session["refresh_token"] = refresh_token
    session.permanent = True

    logger.info("auth success user=%s groups=%s", user.get("email"), user.get("groups", []))

    next_url = session.pop("next_url", None) or url_for("flight_schedules_page")
    return jsonify(
        {
            "success": True,
            "redirect_url": next_url,
            "user": {"email": user.get("email"), "groups": user.get("groups", [])},
        }
    )


@bp.route("/logout")
def logout():
    """Clear the session, then hand off to the provider's logout if it has one."""
    from src.web.auth_shim import get_current_user

    user_email = None
    if AUTH_ENABLED:
        user = get_current_user()
        if user:
            user_email = user.get("email")

    session.clear()
    if user_email:
        logger.info(f"User logged out: {user_email}")

    if AUTH_ENABLED:
        auth = _get_auth()
        if auth:
            # Google has no RP-initiated logout and returns an empty URL, so
            # clearing the session above is the whole of signing out there.
            logout_url = auth.get_logout_url()
            if logout_url:
                return redirect(logout_url)

    return redirect(url_for("flight_schedules_page"))


@bp.route("/auth/debug")
def auth_debug():
    """Show the current user's verified token claims. Login-gated."""
    # Import decorator here because it's conditional on AUTH_ENABLED and
    # registering at module import time would break skip-auth mode.
    from src.web.auth_shim import get_current_user, login_required

    @login_required
    def _handler():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Not authenticated"}), 401

        resolved_provider = "none"
        groups_source = "env"
        if AUTH_ENABLED:
            from src.auth.factory import groups_source as _groups_source
            from src.auth.factory import resolve_auth_provider_name

            resolved_provider = resolve_auth_provider_name()
            groups_source = _groups_source()

        raw_claims = None
        id_token = session.get("id_token")
        if id_token and AUTH_ENABLED:
            auth = _get_auth()
            if auth:
                raw_claims = auth.verify_token(id_token)

        return jsonify(
            {
                "user": user,
                "resolved_provider": resolved_provider,
                "groups_source": groups_source,
                "raw_claims": raw_claims,
                "groups_from_user": user.get("groups", []),
                "cognito_groups_from_claims": raw_claims.get("cognito:groups", [])
                if raw_claims
                else None,
            }
        )

    return _handler()
