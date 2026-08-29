"""Pure helpers extracted from ``web_app.py``.

Second pass of the web_app.py teardown (the first was
:mod:`src.web.time_helpers`). Everything here is a **pure function or
a plain constant** — no ``db_manager`` module-global read, no Flask
``app`` reference, no ``config`` singleton. That's the exit criterion
for landing in this module: it must be safe to import from any layer
without dragging the runtime state along.

Anything that reaches for ``db_manager`` / ``config`` / OpenSearch
still lives in ``web_app.py`` — extracting those requires a
runtime-state module which is a bigger design step than a cleanup
pass warrants.

``web_app.py`` re-imports these names so the Flask half's global
namespace still resolves them; when ``web_app.py`` is eventually
deleted, callers already point here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ``attention_level`` values that make an aircraft "special" — the admin
# list's ``category=special`` filter, its header count and the per-row
# flag all mean this same set. Both the Chinese and English spellings
# occur in the data.
SPECIAL_ATTENTION_LEVELS: tuple[str, ...] = ("高", "极高", "high", "very high")


# ---------------------------------------------------------------------------
# DB introspection
# ---------------------------------------------------------------------------


def table_exists(session: Session, table_name: str) -> bool:
    """Dialect-agnostic "does this table exist?" check.

    Originally the code used
    ``SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = ...)``
    which is Postgres-only. SQLAlchemy's Inspector works across SQLite,
    Postgres, MySQL, etc.

    Named without the ``_`` prefix here — the module is the new home,
    and a leading underscore was the "private to web_app" marker.
    ``web_app.py`` re-exports as ``_table_exists`` for source-level
    backward compat.
    """
    from sqlalchemy import inspect

    try:
        return inspect(session.get_bind()).has_table(table_name)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Aircraft type & livery helpers
# ---------------------------------------------------------------------------

# ICAO type-code → Chinese display-name map. Kept short (only the
# frequently-seen types) — anything not in the map falls back to the
# raw code. Additions welcome; keep alphabetical-by-value inside groups
# for readability.
_AIRCRAFT_TYPE_NAMES: dict[str, str] = {
    "H60": "黑鹰直升机",
    "C17": "环球霸王运输机",
    "TWR": "塔台",
    "C30J": "大力神运输机",
    "TEX2": "教练机",
    "C130": "大力神运输机",
    "EC45": "欧直直升机",
    "H47": "支奴干直升机",
    "C295": "运输机",
    "A139": "阿古斯塔直升机",
    "B350": "商务机",
    "A400": "运输机",
    "K35R": "加油机",
    "A332": "空客A330",
    "BE20": "商务机",
    "B737": "波音737",
    "EC35": "欧直直升机",
    "CN35": "其他",
    "C172": "塞斯纳172",
    "B762": "波音767",
    "B738": "波音737-800",
    "B739": "波音737-900",
    "B77W": "波音777-300ER",
    "B788": "波音787-8",
    "B789": "波音787-9",
    "B78X": "波音787-10",
    "A320": "空客A320",
    "A321": "空客A321",
    "A319": "空客A319",
    "A20N": "空客A320neo",
    "A21N": "空客A321neo",
    "A350": "空客A350",
    "A359": "空客A350-900",
    "A35K": "空客A350-1000",
    "A333": "空客A330-300",
    "A339": "空客A330-900neo",
    "A388": "空客A380-800",
    "E190": "Embraer E190",
    "E195": "Embraer E195",
    "CRJ9": "CRJ-900",
    "CRJ7": "CRJ-700",
    "C919": "C919",
    "ARJ2": "ARJ21",
    "MA60": "MA60",
}


def get_aircraft_type_name(code: str) -> str:
    """Return the display name for an ICAO type code.

    Falls back to the raw code when unknown, so the caller never has
    to check for None.
    """
    return _AIRCRAFT_TYPE_NAMES.get(code, code)


_LIVERY_INDICATOR_PATTERN = re.compile(
    r"\(([^)]*(?:Livery|Alliance|Special|Retro)[^)]*)\)", re.IGNORECASE
)
_LIVERY_SUFFIX_PATTERN = re.compile(r"\s*Livery\s*", re.IGNORECASE)


def extract_livery_indicator(airline_name: str) -> str | None:
    """Extract the livery indicator embedded in an airline name.

    Handles patterns like ``"China Eastern (SkyTeam Livery)"`` or
    ``"Delta (Retro)"`` — matches the first parenthesised group whose
    text contains Livery / Alliance / Special / Retro (case-insensitive),
    then trims the trailing ``Livery`` suffix if present.

    Returns ``None`` when the input is falsy or no indicator matches.
    """
    if not airline_name:
        return None
    match = _LIVERY_INDICATOR_PATTERN.search(airline_name)
    if not match:
        return None
    livery = _LIVERY_SUFFIX_PATTERN.sub("", match.group(1)).strip()
    return livery or None
