"""Authentication blueprint: login, callback, logout, session, debug.

Five routes total. The Cognito OAuth2 flow is client-side: `/auth/callback`
returns an HTML page that exchanges the authorization code for tokens in
the browser, then POSTs the tokens to `/auth/set-session` for server-side
verification and session setup.
"""

from __future__ import annotations

import logging
import secrets

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from src.web.auth_shim import AUTH_ENABLED

logger = logging.getLogger("web.auth")
bp = Blueprint("auth", __name__)


def _get_cognito_auth():
    """Lazy getter — Cognito config is optional."""
    if not AUTH_ENABLED:
        return None
    from src.auth.cognito_auth import get_cognito_auth

    return get_cognito_auth()


@bp.route("/login")
def login():
    """Redirect to Cognito Hosted UI for login."""
    if not AUTH_ENABLED:
        return render_template("403.html", message="Authentication is not enabled"), 403

    auth = _get_cognito_auth()
    if not auth:
        return render_template("403.html", message="Authentication is not configured"), 403

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session.modified = True
    logger.debug("login state generated=%s...", state[:20])

    return redirect(auth.get_login_url(state=state))


@bp.route("/auth/callback")
def auth_callback():
    """OAuth2 callback — returns the page that exchanges code for tokens client-side.

    Token exchange happens in the browser to avoid Lambda needing external
    network access. After the exchange, the browser POSTs the tokens to
    `/auth/set-session` below.
    """
    if not AUTH_ENABLED:
        return render_template("403.html", message="Authentication is not enabled"), 403

    auth = _get_cognito_auth()
    if not auth:
        return render_template("403.html", message="Authentication is not configured"), 403

    expected_state = session.get("oauth_state", "")
    return render_template(
        "auth_callback.html",
        cognito_domain=auth.domain,
        cognito_client_id=auth.client_id,
        cognito_client_secret=auth.client_secret or "",
        cognito_callback_url=auth.callback_url,
        expected_state=expected_state,
    )


@bp.route("/auth/set-session", methods=["POST"])
def auth_set_session():
    """Set server session from client-side token-exchange result."""
    if not AUTH_ENABLED:
        return jsonify({"error": "Authentication is not enabled"}), 403

    auth = _get_cognito_auth()
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

    from src.auth.cognito_auth import get_user_from_token

    user = get_user_from_token(id_token)
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
    """Clear session and redirect to Cognito logout (or the public landing)."""
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
        auth = _get_cognito_auth()
        if auth:
            return redirect(auth.get_logout_url())

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

        raw_claims = None
        id_token = session.get("id_token")
        if id_token and AUTH_ENABLED:
            auth = _get_cognito_auth()
            if auth:
                raw_claims = auth.verify_token(id_token)

        return jsonify(
            {
                "user": user,
                "raw_claims": raw_claims,
                "groups_from_user": user.get("groups", []),
                "cognito_groups_from_claims": raw_claims.get("cognito:groups", [])
                if raw_claims
                else None,
            }
        )

    return _handler()
