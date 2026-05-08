"""
Flask Authentication Decorators

Provides decorators for protecting Flask routes with Cognito authentication.
Supports role-based and group-based access control.
"""

import logging
import os
from collections.abc import Callable
from functools import wraps

from flask import (
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from src.auth.cognito_auth import get_cognito_auth, get_user_from_token

logger = logging.getLogger(__name__)

# Admin role/group identifiers
ADMIN_GROUPS = ["admins", "admin", "administrator", "superuser"]

# Flight schedules access groups
FLIGHT_SCHEDULES_GROUPS = ["flight-schedules-viewers", "admins"]


def _should_skip_auth() -> bool:
    """Check if authentication should be skipped (local development only).

    Returns:
        True if both STAGE=local and SKIP_AUTH=true are set
    """
    stage = os.environ.get("STAGE", "").lower()
    skip_auth = os.environ.get("SKIP_AUTH", "false").lower() == "true"
    return stage == "local" and skip_auth


def _get_mock_user() -> dict:
    """Get mock user for local development.

    Returns:
        Dict containing mock user info with admin privileges
    """
    groups_str = os.environ.get("LOCAL_DEV_GROUPS", "admins,flight-schedules-viewers")
    groups = [g.strip() for g in groups_str.split(",") if g.strip()]

    return {
        "sub": "local-dev-user",
        "email": os.environ.get("LOCAL_DEV_EMAIL", "dev@local.test"),
        "groups": groups,
        "role": "admin",
    }


def get_current_user() -> dict | None:
    """Get the current authenticated user from session.

    Returns:
        User info dict if authenticated, None otherwise
    """
    # Check if already loaded in request context
    if hasattr(g, "current_user") and g.current_user is not None:
        return g.current_user

    # Local development: return mock user if auth is skipped
    if _should_skip_auth():
        mock_user = _get_mock_user()
        g.current_user = mock_user
        logger.debug(f"Local dev mode: using mock user {mock_user['email']}")
        return mock_user

    # Try to get user from session
    id_token = session.get("id_token")
    if not id_token:
        return None

    # Verify token and get user info
    user = get_user_from_token(id_token)
    if user:
        g.current_user = user
        return user

    # Token invalid or expired, try refresh (with timeout protection)
    refresh_token = session.get("refresh_token")
    if refresh_token:
        try:
            auth = get_cognito_auth()
            if auth:
                logger.debug("Attempting token refresh")
                tokens = auth.refresh_tokens(refresh_token)
                if tokens:
                    session["id_token"] = tokens.get("id_token")
                    session["access_token"] = tokens.get("access_token")
                    # Refresh token is not always returned
                    if tokens.get("refresh_token"):
                        session["refresh_token"] = tokens.get("refresh_token")

                    user = get_user_from_token(tokens.get("id_token"))
                    if user:
                        g.current_user = user
                        logger.info(f"Token refreshed for user: {user.get('email')}")
                        return user
                else:
                    logger.warning("Token refresh returned no tokens")
        except Exception as e:
            logger.error(f"Token refresh failed with exception: {e}")

    # Clear invalid session
    logger.debug("Clearing invalid session")
    session.pop("id_token", None)
    session.pop("access_token", None)
    session.pop("refresh_token", None)
    return None


def login_required(f: Callable) -> Callable:
    """Decorator to require authentication for a route.

    Usage:
        @app.route('/protected')
        @login_required
        def protected_route():
            return "Secret content"

    Args:
        f: The view function to decorate

    Returns:
        Decorated function that checks authentication
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()

        if user is None:
            # Store the original URL for redirect after login
            session["next_url"] = request.url

            # Check if Cognito is configured
            auth = get_cognito_auth()
            if auth:
                # Redirect to Cognito login
                return redirect(url_for("login"))
            else:
                # Cognito not configured, show error
                logger.warning("Authentication required but Cognito not configured")
                return render_template(
                    "403.html",
                    message="Authentication system not configured",
                ), 403

        # User is authenticated, proceed with the request
        return f(*args, **kwargs)

    return decorated_function


def group_required(allowed_groups: list[str]) -> Callable:
    """Decorator factory to require user membership in specific Cognito groups.

    Usage:
        @app.route('/premium')
        @login_required
        @group_required(['premium-users', 'admins'])
        def premium_route():
            return "Premium content"

    Args:
        allowed_groups: List of group names that are allowed access

    Returns:
        Decorator function that checks group membership
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()

            if user is None:
                # Store the original URL for redirect after login
                session["next_url"] = request.url

                auth = get_cognito_auth()
                if auth:
                    return redirect(url_for("login"))
                else:
                    logger.warning("Group access required but Cognito not configured")
                    return render_template(
                        "403.html",
                        message="Authentication system not configured",
                    ), 403

            # Get user's groups (case-insensitive comparison)
            user_groups = [grp.lower() for grp in user.get("groups", [])]
            allowed_lower = [grp.lower() for grp in allowed_groups]

            logger.debug(
                "group_check user=%s groups=%s allowed=%s",
                user.get("email"),
                user_groups,
                allowed_lower,
            )

            # Check if user belongs to any allowed group
            has_access = any(grp in allowed_lower for grp in user_groups)

            if not has_access:
                logger.warning(
                    "access_denied user=%s groups=%s required=%s",
                    user.get("email"),
                    user_groups,
                    allowed_groups,
                )
                return render_template(
                    "403.html",
                    message="You do not have permission to access this page",
                ), 403

            return f(*args, **kwargs)

        return decorated_function

    return decorator


def flight_schedules_required(f: Callable) -> Callable:
    """Decorator to require flight-schedules-viewers or admins group.

    Usage:
        @app.route('/flight-schedules')
        @login_required
        @flight_schedules_required
        def flight_schedules():
            return render_template('flight_schedules.html')

    Args:
        f: The view function to decorate

    Returns:
        Decorated function that checks group membership
    """
    return group_required(FLIGHT_SCHEDULES_GROUPS)(f)


def admin_required(f: Callable) -> Callable:
    """Decorator to require admin group for a route.

    Usage:
        @app.route('/admin')
        @admin_required
        def admin_route():
            return "Admin content"

    Args:
        f: The view function to decorate

    Returns:
        Decorated function that checks admin group
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()

        if user is None:
            # Store the original URL for redirect after login
            session["next_url"] = request.url

            auth = get_cognito_auth()
            if auth:
                return redirect(url_for("login"))
            else:
                logger.warning("Admin access required but Cognito not configured")
                return render_template(
                    "403.html",
                    message="Authentication system not configured",
                ), 403

        # Check if user has admin role or is in admin group
        user_role = user.get("role", "").lower()
        user_groups = [grp.lower() for grp in user.get("groups", [])]

        is_admin = user_role in ADMIN_GROUPS or any(grp in ADMIN_GROUPS for grp in user_groups)

        if not is_admin:
            logger.warning(f"Non-admin user {user.get('email')} attempted to access admin route")
            return render_template(
                "403.html",
                message="You do not have permission to access this page",
            ), 403

        return f(*args, **kwargs)

    return decorated_function


def optional_login(f: Callable) -> Callable:
    """Decorator that loads user info if available, but doesn't require auth.

    Usage:
        @app.route('/public')
        @optional_login
        def public_route():
            if g.current_user:
                return f"Hello, {g.current_user['email']}"
            return "Hello, guest"

    Args:
        f: The view function to decorate

    Returns:
        Decorated function that optionally loads user info
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Try to load user, but don't require it
        get_current_user()
        return f(*args, **kwargs)

    return decorated_function
