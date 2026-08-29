#!/usr/bin/env python3
"""Compatibility shim over the extracted web helpers.

**Historically this was the Flask entrypoint.** Every ``@app.route``
handler lived here (5000+ lines). Those routes were migrated to
FastAPI under ``src/web/routes/*_fastapi.py`` in one PR, and the
helpers they still delegate to were extracted into small dedicated
modules in a second:

- Runtime state (``db_manager`` / ``config`` / ``api_cache`` /
  ``init_app``) → :mod:`src.web.runtime`
- Image URL helpers → :mod:`src.web.image_helpers`
- Aircraft full-text search + fallback → :mod:`src.web.search_index`
- Multi-user + scraper-session service factories →
  :mod:`src.web.service_factory`
- Pure helpers (``_table_exists``, ``get_aircraft_type_name``,
  ``extract_livery_indicator``, ``SPECIAL_ATTENTION_LEVELS``) →
  :mod:`src.web.helpers`
- Timestamp / timezone helpers (``_to_iso`` / ``BEIJING_TZ`` / …) →
  :mod:`src.web.time_helpers`

This file exists only to keep every existing ``from web_app import
<name>`` call working during the co-existence window. When those
imports get updated to point at the new modules directly, this file
can be deleted outright.

Because :attr:`runtime.db_manager` / :attr:`runtime.config` are
module attributes populated at startup, importing them here gives you
the *value at import time* — ``None`` on cold start, the real object
after ``init_app()`` ran. Callers that were reading ``web_app.db_manager``
mid-request would get stale ``None``; the ``__getattr__`` shim below
routes attribute access back to :mod:`src.web.runtime` so
``web_app.db_manager`` always resolves to the current value.
"""

from __future__ import annotations

import sys
from typing import Any

# For callers that reach for ``web_app.MAX_WINDOW``.
from src.search.aircraft_index import MAX_WINDOW

# ---- Re-exports ------------------------------------------------------------
# Pure helpers and constants first.
from src.web.helpers import (
    SPECIAL_ATTENTION_LEVELS,
    extract_livery_indicator,
    get_aircraft_type_name,
)
from src.web.helpers import (
    table_exists as _table_exists,
)
from src.web.image_helpers import (
    batch_get_images_from_static_info,
    get_image_url,
    get_images_from_static_info,
    transform_image_paths,
)
from src.web.runtime import init_app
from src.web.search_index import (
    aircraft_search_index,
    with_aircraft_index,
)
from src.web.service_factory import (
    get_multi_user_services,
    get_scraper_db_session,
)
from src.web.time_helpers import (
    BEIJING_TZ,
    HAS_LIVERY_SQL,
    UTC,
    _to_datetime,
    _to_iso,
    convert_beijing_to_utc,
    convert_utc_to_beijing,
)


def __getattr__(name: str) -> Any:
    """Route ``web_app.db_manager`` / ``web_app.config`` / ``web_app.api_cache``
    through to the ``src.web.runtime`` module.

    Load-bearing: those three are module attributes that *change* after
    :func:`init_app` runs. A plain ``from src.web.runtime import db_manager``
    at the top of this module would bind ``None`` at import time and
    never see the update. Module ``__getattr__`` fires on attribute
    lookup, so every ``web_app.db_manager`` read fetches the current
    :mod:`src.web.runtime` value.
    """
    if name in ("db_manager", "config", "api_cache"):
        from src.web import runtime

        return getattr(runtime, name)
    raise AttributeError(f"module 'web_app' has no attribute {name!r}")


# Legacy CLI: `python web_app.py` no longer serves anything (Flask is
# gone). Point people at the ASGI entry.
if __name__ == "__main__":
    sys.stderr.write(
        "web_app.py no longer runs a server. Use "
        "`uv run uvicorn asgi:app --reload` or "
        "`uv run python -m uvicorn app:app`.\n"
    )
    sys.exit(1)
