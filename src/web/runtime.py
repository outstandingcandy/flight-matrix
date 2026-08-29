"""Process-level runtime state for the FastAPI web app.

Holds the three module globals that used to live in ``web_app.py``:
``db_manager`` / ``config`` / ``api_cache``. They start as ``None`` /
empty and get populated by :func:`init_app`, which the FastAPI
lifespan handler in ``app.py`` calls once at startup (and Lambda cold
start via ``lambda_handler.py``).

Every helper that used to reach for ``web_app.db_manager`` /
``web_app.config`` now imports them from here instead. ``web_app.py``
keeps re-exporting the names so nothing on the outside had to move at
the same time.

Structure mirrors :mod:`src.auth.factory` — module-level singletons
initialised lazily on first request, torn down between tests via
:func:`reset_runtime`.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from src.utils.database import DatabaseManager, mask_database_url
from src.utils.yaml_config import YAMLConfig
from src.web.middleware import TTLCache

if TYPE_CHECKING:
    pass

logger = logging.getLogger("web.runtime")

# Process-level state. ``None`` until :func:`init_app` runs; any
# helper that reads them must handle that lazily or call
# :func:`init_app` first.
db_manager: DatabaseManager | None = None
config: YAMLConfig | None = None

# API cache for hot data (TTL ~1 hour). Constructed at module load
# because it's cheap and its state is per-key; concurrent test suites
# clearing it between runs would need :func:`reset_runtime`.
api_cache = TTLCache()


def init_app() -> None:
    """Populate ``db_manager`` and ``config`` from the current env.

    Idempotent enough — a second call replaces both singletons with
    fresh instances. That's what tests do between fixture invocations.

    Reads ``CONFIG_PATH`` (default ``config/config.yaml``) and
    ``DATABASE_URL`` (overrides the URL the YAML config resolved).
    """
    global db_manager, config

    try:
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
        config = YAMLConfig(config_path)
        db_config = config.get_database_config()

        db_url = os.environ.get("DATABASE_URL", db_config["url"])
        logger.info("Database URL: %s", mask_database_url(db_url))

        db_manager = DatabaseManager(db_url)
        logger.info("Web application initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize web application: %s", e)
        raise


def reset_runtime() -> None:
    """Drop the cached ``db_manager`` and ``config``. Test-only hook.

    Doesn't currently reset ``api_cache`` — its contents are keyed
    per-request and rotate on TTL, so a stale entry in a fresh test
    context is a non-issue.
    """
    global db_manager, config
    db_manager = None
    config = None
