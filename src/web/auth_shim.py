"""Centralises the auth-on/auth-off logic used by the web app.

The rules:

- If `--skip-auth` was on the CLI or `SKIP_AUTH=true` in the environment,
  auth is disabled — we hand out no-op decorators and a mock user.
- Otherwise we try to import the real Cognito decorators. If that import
  fails (e.g. Cognito not configured yet), we still fall back to the
  no-op decorators.

This module consolidates the three copies of that logic that lived in
`web_app.py` before the Phase 5.3 split.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("web.auth_shim")


def is_auth_skipped() -> bool:
    """True when --skip-auth or SKIP_AUTH=true is set."""
    return "--skip-auth" in sys.argv or os.environ.get("SKIP_AUTH", "").lower() == "true"


def _normalise_skip_auth_flags() -> None:
    """Reflect the CLI flag into env vars so downstream code sees it.

    Also strips the flag from argv so Flask's CLI parser doesn't choke.
    """
    if "--skip-auth" in sys.argv:
        os.environ["STAGE"] = "local"
        os.environ["SKIP_AUTH"] = "true"
        sys.argv.remove("--skip-auth")


def _noop(f: Callable[..., Any]) -> Callable[..., Any]:
    return f


def _noop_group_required(_groups: Any) -> Callable[..., Any]:
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        return f

    return decorator


def _install_noops(module_globals: dict[str, Any]) -> None:
    module_globals["login_required"] = _noop
    module_globals["admin_required"] = _noop
    module_globals["flight_schedules_required"] = _noop
    module_globals["optional_login"] = _noop
    module_globals["group_required"] = _noop_group_required


_normalise_skip_auth_flags()
SKIP_AUTH = is_auth_skipped()

# Populate the decorator names into this module's globals so callers can
# `from src.web.auth_shim import login_required, admin_required, …` regardless
# of which branch fired.
_globals: dict[str, Any] = globals()

if SKIP_AUTH:
    logger.info("Auth disabled via --skip-auth flag or SKIP_AUTH env var")
    AUTH_ENABLED = False
    _install_noops(_globals)
    # get_current_user still works in skip-auth mode — decorators.py detects it.
    from src.auth.decorators import get_current_user
else:
    try:
        from src.auth.cognito_auth import get_cognito_auth, get_user_from_token
        from src.auth.decorators import (
            admin_required,
            flight_schedules_required,
            get_current_user,
            group_required,
            login_required,
            optional_login,
        )

        AUTH_ENABLED = True
    except ImportError as e:
        logger.warning(f"Auth module not available: {e}")
        AUTH_ENABLED = False
        _install_noops(_globals)

        def get_current_user() -> dict[str, Any] | None:  # type: ignore[no-redef]
            return None
