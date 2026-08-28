"""Cross-dialect timestamp helpers + Beijing/UTC conversion.

Extracted from ``web_app.py`` because these helpers have no Flask
dependency and roughly a dozen FastAPI handlers were doing
``from web_app import _to_iso, _to_datetime, convert_utc_to_beijing,
BEIJING_TZ`` just to get them. Living in ``web_app.py`` meant every
FastAPI handler pulled Flask, blueprints, and ~200 KB of the app into
their import graph for four pure functions.

``web_app.py`` re-imports these names for the Flask side, so the module
remains the single source of truth for both entries during the
co-existence window. When ``web_app.py`` finally goes, callers already
point here.

Why these live together:

- ``_to_iso`` / ``_to_datetime`` — the sqlite-vs-psycopg2 driver split
  hands back ``str`` on SQLite and ``datetime`` on Postgres for the
  same ``text()``-column shape, so raw SQL callers need a normaliser.
- ``BEIJING_TZ`` / ``UTC`` — the two ``pytz`` timezones the rest of the
  file operates on. Constants, not helpers, but the file needs them
  and no other module owns them.
- ``convert_utc_to_beijing`` / ``convert_beijing_to_utc`` — display
  and query-window helpers used by the flight-schedule and
  aircraft-history handlers. Preserve the Flask-era loose behaviour:
  swallow parse errors, log a warning, hand back a best-effort value
  the frontend can render or the SQL layer can filter on.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytz

logger = logging.getLogger("web.time_helpers")

UTC = pytz.UTC
BEIJING_TZ = pytz.timezone("Asia/Shanghai")


def _to_iso(value: datetime | str | None) -> str | None:
    """Render a timestamp column from a raw-SQL row as an ISO-8601 string.

    A ``text()`` query carries no type information, so the driver
    decides what a timestamp column becomes: psycopg2 returns a
    ``datetime``, but SQLite hands back the stored string.
    ``value.isoformat()`` therefore raises ``AttributeError`` on
    SQLite for SQL that works fine against Aurora.

    Args:
        value: A ``datetime``, a timestamp string, or None.

    Returns:
        The ISO-8601 form, or None when ``value`` is None or empty.
    """
    if not value:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _to_datetime(value: datetime | str | None) -> datetime | None:
    """Read a timestamp column from a raw-SQL row as a ``datetime``.

    Mirror of :func:`_to_iso` for callers that do arithmetic on the
    value rather than rendering it. Subtracting a ``str`` from a
    ``datetime`` is a ``TypeError``, so the same driver difference
    would break those callers too.

    Args:
        value: A ``datetime``, a timestamp string, or None.

    Returns:
        The value as a ``datetime``, or None when it is None, empty,
        or an unparseable string.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning("Unparseable timestamp from the database: %r", value)
            return None
    return None


def convert_utc_to_beijing(utc_datetime_str: datetime | str | None) -> str | None:
    """Format a UTC timestamp as a Beijing-local ``YYYY-MM-DD HH:MM:SS`` string.

    Best-effort — accepts trailing ``Z``, ``+HH:MM`` offsets, or the
    literal ``UTC`` suffix. Parse failures fall back to ``str(value)``
    so the frontend still has something to render.
    """
    if not utc_datetime_str:
        return None

    try:
        if isinstance(utc_datetime_str, str):
            if utc_datetime_str.endswith("Z"):
                utc_dt = datetime.fromisoformat(utc_datetime_str.replace("Z", "+00:00"))
            elif "+" in utc_datetime_str or utc_datetime_str.endswith("UTC"):
                utc_dt = datetime.fromisoformat(utc_datetime_str.replace("UTC", "").strip())
            else:
                utc_dt = datetime.fromisoformat(utc_datetime_str)
        else:
            utc_dt = utc_datetime_str

        if utc_dt.tzinfo is None:
            utc_dt = UTC.localize(utc_dt)

        beijing_dt = utc_dt.astimezone(BEIJING_TZ)
        return beijing_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.warning("Time conversion error for %r: %s", utc_datetime_str, e)
        return str(utc_datetime_str)


def convert_beijing_to_utc(beijing_datetime_str: str | None) -> datetime | None:
    """Parse a Beijing-local timestamp string, return a naive UTC datetime.

    DB columns store naive UTC, so the returned value is naive on
    purpose — feeding it back to SQL doesn't need ``tzinfo``. The
    last-ditch ``fromisoformat`` fallback preserves the Flask-era
    behaviour of returning a naive datetime for strings that happen
    to already be ISO.
    """
    if not beijing_datetime_str:
        return None

    try:
        beijing_dt = datetime.fromisoformat(beijing_datetime_str)
        if beijing_dt.tzinfo is None:
            beijing_dt = BEIJING_TZ.localize(beijing_dt)
        utc_dt = beijing_dt.astimezone(UTC)
        return utc_dt.replace(tzinfo=None)  # DB stores naive datetimes.
    except Exception as e:
        logger.warning("Time conversion error: %s", e)
        try:
            return datetime.fromisoformat(beijing_datetime_str)
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# SQL constant used by several handlers touching aircraft_static_info.
#
# "This aircraft has a livery worth showing", as a SQL predicate on
# ``aircraft_static_info asi``.
#
# Three queries used to write ``asi.has_special_livery = TRUE``, but no
# such column exists in any environment: it is absent from
# ``AircraftStaticInfo``, from ``_ensure_analysis_columns()`` in the
# analysis service, and from every migration script. Those queries
# raised "no such column" on both dialects. ``livery_type`` is the
# field the analysis service actually populates (free text such as
# "special livery" or "government VIP"), so its presence is the
# available expression of the same idea.
# ---------------------------------------------------------------------------

HAS_LIVERY_SQL = "(asi.livery_type IS NOT NULL AND asi.livery_type != '')"
