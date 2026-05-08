"""Flask web application package.

The Flask app lives under `src/web/` so routes, middleware, and context
processors can be separated cleanly. `web_app.py` at the repo root is
still the entry point — it imports from here and keeps backward
compatibility with existing route handlers that have not yet been
migrated into blueprints.

Migration guide: see docs/web-blueprints.md.
"""

from src.web.auth_shim import AUTH_ENABLED, is_auth_skipped
from src.web.middleware import TTLCache

__all__ = ["AUTH_ENABLED", "TTLCache", "is_auth_skipped"]
