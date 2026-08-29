"""Auth-on/auth-off resolution for the FastAPI web app.

Two booleans + one predicate:

- :func:`is_auth_skipped` — checks the ``--skip-auth`` CLI flag and
  the ``SKIP_AUTH`` env var. Load-bearing across the auth surface;
  every module that gates behaviour on "is auth enabled" reads this.
- :data:`SKIP_AUTH` — module-level snapshot of that predicate at
  import time. Historically load-bearing for the Flask-side decorator
  install; kept as an alias for compatibility with any callers that
  read the constant instead of calling the function.
- :data:`AUTH_ENABLED` — ``not SKIP_AUTH``. The FastAPI ``/login`` and
  ``/logout`` handlers use this to short-circuit into a 403 when the
  auth provider isn't configured.

Historically this module also decided whether to import the real
Flask-side decorators (login_required / admin_required / …) or install
no-op replacements. That branch is gone — the FastAPI half uses
``src/auth/dependencies.py`` for the equivalent surface, and the
Flask half no longer runs. If some downstream code still needs a
noop decorator, get it from there.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("web.auth_shim")


def is_auth_skipped() -> bool:
    """True when ``--skip-auth`` is on the CLI or ``SKIP_AUTH=true`` in the env."""
    return "--skip-auth" in sys.argv or os.environ.get("SKIP_AUTH", "").lower() == "true"


def _normalise_skip_auth_flags() -> None:
    """Reflect the CLI flag into env vars so downstream code sees it.

    Also strips the flag from argv so any downstream CLI parsers don't
    choke — legacy from the Flask era but harmless.
    """
    if "--skip-auth" in sys.argv:
        os.environ["STAGE"] = "local"
        os.environ["SKIP_AUTH"] = "true"
        sys.argv.remove("--skip-auth")


_normalise_skip_auth_flags()

SKIP_AUTH: bool = is_auth_skipped()
AUTH_ENABLED: bool = not SKIP_AUTH

if SKIP_AUTH:
    logger.info("Auth disabled via --skip-auth flag or SKIP_AUTH env var")
