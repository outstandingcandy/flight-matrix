#!/usr/bin/env python3
"""Runtime state + shared helpers for the FastAPI web app.

**Historically this was the Flask entrypoint.** Every ``@app.route``
decorated handler lived here (5000+ lines). Those routes have all been
migrated to FastAPI under ``src/web/routes/*_fastapi.py``, and this
module has been stripped down to what the migrated handlers still
delegate to: the ``db_manager`` / ``config`` module globals populated
by :func:`init_app`, the image / index / multi-user-service helpers
they call, and a re-export layer for the constants + timestamp
helpers that already moved to ``src/web/helpers.py`` +
``src/web/time_helpers.py``.

Flask itself is gone — no ``app`` object, no blueprint registration,
no CORS / ProxyFix / session middleware. Those all live on the
FastAPI side in ``app.py`` now.

``init_app()`` is still what the FastAPI lifespan calls to prime this
module's globals; that contract is unchanged.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

# Ensure the project root is on sys.path before importing anything under `src.`.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.core.exceptions import SearchError
from src.search.aircraft_index import MAX_WINDOW, AircraftPage, AircraftSearchIndex
from src.search.opensearch_client import OpenSearchSettings, get_client
from src.storage import resolve_media_base_url
from src.utils.database import DatabaseManager, mask_database_url
from src.utils.yaml_config import YAMLConfig
from src.web.middleware import TTLCache

# API cache for hot data (TTL ~1 hour).
api_cache = TTLCache()

# Result of one aircraft-index query; see `with_aircraft_index`.
_T = TypeVar("_T")

from src.web.helpers import SPECIAL_ATTENTION_LEVELS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_app")


# Trust proxy headers from API Gateway / ALB for correct URL generation.


# Session configuration for authentication.

# Register blueprints.

# 全局变量
db_manager = None
config = None


# Context processor - inject the static asset base URL and current user


# Auth routes live in src/web/routes/auth.py (registered as `auth_bp` above).


# Timezone constants + shared timestamp helpers moved to src/web/time_helpers.py
# so FastAPI handlers can import them without dragging Flask + the whole
# web_app module into their import graph. Re-exported here to keep every
# ``from web_app import _to_iso, ...`` in the Flask half working during the
# co-existence window.
# _table_exists lives in src/web/helpers.py; kept re-exported for the
# Flask half's existing `from web_app import _table_exists` callers.
from src.web.helpers import table_exists as _table_exists
from src.web.time_helpers import (
    BEIJING_TZ,
    HAS_LIVERY_SQL,
    UTC,
    _to_datetime,
    _to_iso,
)


def aircraft_search_index() -> AircraftSearchIndex | None:
    """Return the aircraft full-text index, if one is configured and reachable.

    Returns:
        The index handle, or None when OpenSearch is unconfigured or its client
        could not be built. Callers fall back to SQL.
    """
    settings = OpenSearchSettings.from_config(config) if config else OpenSearchSettings()
    client = get_client(settings)
    if client is None:
        return None
    return AircraftSearchIndex(client, index=settings.index, max_results=settings.max_results)


def with_aircraft_index(operation: Callable[[AircraftSearchIndex], _T], what: str) -> _T | None:
    """Run one query against the aircraft index, or report that SQL should answer.

    Every index-backed endpoint goes through here so that the fallback rule lives
    in one place: an admin page is never allowed to break because search is down.

    Args:
        operation: Receives the index and returns whatever the caller needs.
        what: Short description of the query, for the fallback log line.

    Returns:
        The operation's result, or None when OpenSearch is unconfigured,
        unreachable or rejected the query — in which case the caller must use its
        own SQL path.
    """
    index = aircraft_search_index()
    if index is None:
        return None

    try:
        return operation(index)
    except SearchError as e:
        logger.warning(f"{what} fell back to SQL: {e}")
        return None


def get_image_url(relative_path: str | None) -> str | None:
    """Convert relative image path to full URL based on environment.

    Args:
        relative_path: Relative path stored in database (e.g., "data/jetphotos_images/B-1234_001.jpg")

    Returns:
        Full URL for the image (always uses CloudFront since images are stored on S3)
    """
    if not relative_path:
        return None

    # Already a full URL, return as-is
    if relative_path.startswith("https://") or relative_path.startswith("http://"):
        return relative_path

    # Ensure path starts with 'data/' for consistency
    if not relative_path.startswith("data/"):
        relative_path = f"data/{relative_path}"

    # Scraped images live in object storage and are served from the media base
    # URL (CloudFront on aws, the public GCS bucket on gcp).
    base_url = resolve_media_base_url()
    if not base_url:
        # No CDN configured; fall back to a relative path so local dev works.
        return f"/{relative_path}"
    return f"{base_url}/{relative_path}"


def transform_image_paths(data: dict) -> dict:
    """Transform image paths in a data dictionary to full URLs.

    Args:
        data: Dictionary that may contain image_path_1, image_path_2, image_path_3

    Returns:
        Same dictionary with image paths transformed to full URLs
    """
    for key in ["image_path_1", "image_path_2", "image_path_3"]:
        if data.get(key):
            data[key] = get_image_url(data[key])
    return data


def get_images_from_static_info(registration: str) -> dict[str, str | None]:
    """Get image paths from aircraft_images table.

    Args:
        registration: Aircraft registration number

    Returns:
        Dictionary with image_path_1, image_path_2, image_path_3 keys
        (for backward compatibility with existing code)
    """
    if not registration or not db_manager:
        return {"image_path_1": None, "image_path_2": None, "image_path_3": None}

    try:
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            # Query from aircraft_images table ordered by display_order
            result = session.execute(
                text("""
                SELECT image_path
                FROM aircraft_images
                WHERE registration = :reg
                AND image_path IS NOT NULL
                AND image_path != ''
                ORDER BY display_order ASC
                LIMIT 3
            """),
                {"reg": registration},
            ).fetchall()

            if result:
                paths = [row[0] for row in result]
                return {
                    "image_path_1": paths[0] if len(paths) > 0 else None,
                    "image_path_2": paths[1] if len(paths) > 1 else None,
                    "image_path_3": paths[2] if len(paths) > 2 else None,
                }
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Error getting images from aircraft_images: {e}")

    return {"image_path_1": None, "image_path_2": None, "image_path_3": None}


def batch_get_images_from_static_info(registrations: list[str]) -> dict[str, dict[str, str | None]]:
    """Batch get image paths from aircraft_images table.

    Args:
        registrations: List of aircraft registration numbers

    Returns:
        Dictionary mapping registration to image paths dict
        (for backward compatibility, returns image_path_1, image_path_2, image_path_3 keys)
    """
    if not registrations or not db_manager:
        return {}

    try:
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            # Filter out None and empty
            valid_regs = [r for r in registrations if r]
            if not valid_regs:
                return {}

            # Build query
            placeholders = ", ".join([f":reg{i}" for i in range(len(valid_regs))])
            params = {f"reg{i}": reg for i, reg in enumerate(valid_regs)}

            # Query from aircraft_images table ordered by display_order
            # Use ROW_NUMBER to get top 3 images per registration
            result = session.execute(
                text(f"""
                WITH ranked_images AS (
                    SELECT
                        registration,
                        image_path,
                        ROW_NUMBER() OVER (PARTITION BY registration ORDER BY display_order ASC) as rn
                    FROM aircraft_images
                    WHERE registration IN ({placeholders})
                    AND image_path IS NOT NULL
                    AND image_path != ''
                )
                SELECT registration, image_path, rn
                FROM ranked_images
                WHERE rn <= 3
                ORDER BY registration, rn
            """),
                params,
            ).fetchall()

            # Build result dictionary
            images_dict: dict[str, dict[str, str | None]] = {}
            for row in result:
                reg = row[0]
                image_path = row[1]
                rn = row[2]

                if reg not in images_dict:
                    images_dict[reg] = {
                        "image_path_1": None,
                        "image_path_2": None,
                        "image_path_3": None,
                    }

                images_dict[reg][f"image_path_{rn}"] = image_path

            return images_dict
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Error batch getting images from aircraft_images: {e}")
        return {}


# Beijing/UTC conversion helpers live in src/web/time_helpers.py — see the
# note next to the _to_iso import above. Kept re-exported here so the
# Flask handlers that still `from web_app import convert_utc_to_beijing`
# keep resolving.
from src.web.time_helpers import (
    convert_beijing_to_utc,
    convert_utc_to_beijing,
)


def init_app():
    """Initialise the app — Lambda-compatible, called once at cold start."""
    global db_manager, config

    try:
        # 加载配置 - support Lambda environment
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
        config = YAMLConfig(config_path)
        db_config = config.get_database_config()

        # 使用环境变量覆盖数据库URL (Lambda部署时使用)
        db_url = os.environ.get("DATABASE_URL", db_config["url"])
        logger.info(f"Database URL: {mask_database_url(db_url)}")

        # 初始化数据库 (支持PostgreSQL和SQLite)
        db_manager = DatabaseManager(db_url)

        # FastAPI's lifespan calls this exactly once at startup and reads
        # ``db_manager`` / ``config`` off this module directly. No more
        # Flask ``app.config`` mirroring — those keys had no reader on
        # the FastAPI side.

        logger.info("Web application initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize web application: {e}")
        raise


# get_aircraft_type_name lives in src/web/helpers.py.
from src.web.helpers import get_aircraft_type_name

# ==================== 新增: 机场看板和搜索追踪功能 ====================


MAX_TYPE_PHOTO_TYPES = 40
MAX_TYPE_PHOTOS_PER_TYPE = 12
DEFAULT_TYPE_PHOTOS_PER_TYPE = 8
TYPE_PHOTOS_CACHE_TTL = 1800


# ==================== Aircraft Info API ====================


# ==================== Multi-User Management API ====================

# Global services for multi-user mode
user_service = None
subscription_service = None
filter_service = None


def get_multi_user_services():
    """Initialize multi-user services if not already done."""
    global user_service, subscription_service, filter_service

    if user_service is None:
        from src.services.filter_service import FilterService
        from src.services.subscription_service import SubscriptionService
        from src.services.user_service import UserService

        # Ensure tables exist
        db_manager.ensure_multi_user_tables_exist()

        user_service = UserService(db_manager)
        subscription_service = SubscriptionService(db_manager, config)
        filter_service = FilterService(db_manager)

    return user_service, subscription_service, filter_service


# Admin Pages


# Admin API - User Management


# Admin API - Aircraft Management


# Admin API - Report Management


# ============== Scraped Data Admin APIs ==============


# User API - Profile and Usage


# User API - Filter Management


# ==================== Flight Schedules API ====================

import re

# extract_livery_indicator lives in src/web/helpers.py.
from src.web.helpers import extract_livery_indicator

# ============================================================
# Scraper Status Admin Page and APIs
# ============================================================


def get_scraper_db_session():
    """Get a database session for scraper queries.

    Reuses the shared `db_manager` engine — it's critical for SQLite where
    in-memory databases are per-connection, but also the right thing for
    Postgres (reuses the connection pool instead of spinning up a second one).
    """
    if db_manager is None:
        # Lazy init for code paths that call this before init_app()
        # (e.g. a request arriving before cold-start finished).
        init_app()
    return db_manager.get_session()


# Legacy CLI: `python web_app.py` no longer serves anything (Flask is
# gone). Point people at ``asgi:app`` under uvicorn instead.
if __name__ == "__main__":
    sys.stderr.write(
        "web_app.py no longer runs a server. Use "
        "`uv run uvicorn asgi:app --reload` or `uv run python -m uvicorn app:app`.\n"
    )
    sys.exit(1)
