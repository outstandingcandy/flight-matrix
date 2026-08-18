"""
Flight Matrix Authentication Module

Provides OIDC authentication for the web application, with the backend chosen
by the active deployment target (see `src/core/deploy_target.py`):

- `aws`   -> `CognitoAuth`, groups from the `cognito:groups` token claim
- `gcp`   -> `GoogleAuth`, groups from `auth.google.groups` in config/auth.yaml
- `local` -> no provider; auth is bypassed via SKIP_AUTH=true + STAGE=local

Call `get_auth_provider()` rather than a concrete class so that route and
decorator code stays target-agnostic.

Groups:
- admins: Full access to all protected resources
- flight-schedules-viewers: Access to /flight-schedules page
"""

from src.auth.base import OIDCProvider
from src.auth.cognito_auth import CognitoAuth, get_cognito_auth
from src.auth.decorators import (
    admin_required,
    flight_schedules_required,
    get_current_user,
    group_required,
    login_required,
    optional_login,
)
from src.auth.factory import (
    exchange_code_for_tokens,
    get_auth_provider,
    get_user_from_token,
    groups_source,
    reset_auth_provider,
    resolve_auth_provider_name,
    verify_token,
)
from src.auth.google_auth import GoogleAuth

__all__ = [
    "CognitoAuth",
    "GoogleAuth",
    "OIDCProvider",
    "admin_required",
    "exchange_code_for_tokens",
    "flight_schedules_required",
    "get_auth_provider",
    "get_cognito_auth",
    "get_current_user",
    "get_user_from_token",
    "group_required",
    "groups_source",
    "login_required",
    "optional_login",
    "reset_auth_provider",
    "resolve_auth_provider_name",
    "verify_token",
]
