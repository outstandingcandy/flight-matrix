"""
Flight Matrix Authentication Module

Provides Cognito-based authentication for the web application.
Supports group-based access control via Cognito Groups.

Groups:
- admins: Full access to all protected resources
- flight-schedules-viewers: Access to /flight-schedules page
"""

from src.auth.cognito_auth import (
    CognitoAuth,
    exchange_code_for_tokens,
    get_cognito_auth,
    get_user_from_token,
    verify_token,
)
from src.auth.decorators import (
    admin_required,
    flight_schedules_required,
    get_current_user,
    group_required,
    login_required,
    optional_login,
)

__all__ = [
    # Cognito auth
    "CognitoAuth",
    "admin_required",
    "exchange_code_for_tokens",
    "flight_schedules_required",
    "get_cognito_auth",
    "get_current_user",
    "get_user_from_token",
    "group_required",
    # Decorators
    "login_required",
    "optional_login",
    "verify_token",
]
