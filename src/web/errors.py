"""Client-safe error responses for JSON API handlers.

Every `except Exception` in the API used to answer with
``{"success": False, "error": str(exc)}`` and a 500. That hands the client
whatever the exception happened to say, which for the database errors these
handlers mostly raise means table names, column names and the full failing SQL —
`sqlite3.OperationalError` and `psycopg2.errors.*` both embed the statement.
The detail belongs in the log, where it is paired with a traceback; the client
gets a fixed string.

Usage in a handler:

    from src.web.errors import api_error

    try:
        ...
    except Exception as exc:
        return api_error(exc, "Error listing aircraft")

The response shape is unchanged, so no frontend code needs to change.
"""

from __future__ import annotations

import logging

from flask import Response, jsonify

logger = logging.getLogger(__name__)

__all__ = ["GENERIC_ERROR_MESSAGE", "api_error"]

# Deliberately says nothing about what failed. Anything more specific is a
# judgement call per call site, and the whole point here is to stop making that
# judgement wrongly 67 times.
GENERIC_ERROR_MESSAGE = "Internal server error"


def api_error(exc: BaseException, context: str, status: int = 500) -> tuple[Response, int]:
    """Log `exc` with a traceback and build a response that leaks nothing.

    Args:
        exc: The caught exception. Logged with its traceback; never serialised
            into the response.
        context: What the handler was doing, phrased as the log message —
            e.g. ``"Error listing aircraft"``. Written verbatim to the log, so
            existing log searches keep working.
        status: HTTP status to return. Defaults to 500; pass something else only
            when the failure genuinely is the client's fault.

    Returns:
        A ``(response, status)`` pair for a handler to return directly. The body
        is ``{"success": False, "error": GENERIC_ERROR_MESSAGE}``.
    """
    logger.error(context, exc_info=exc)
    return jsonify({"success": False, "error": GENERIC_ERROR_MESSAGE}), status
