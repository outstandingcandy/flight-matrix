"""AWS Lambda handler for flight-matrix.

Wraps the FastAPI ASGI app with Mangum. Previously this file wrapped a
Flask WSGI app via ``asgiref.wsgi.WsgiToAsgi``; the FastAPI cut-over
removes that indirection — Mangum speaks ASGI natively.

The FastAPI startup lives inside the app's ASGI ``lifespan`` handler
(``app.create_app``), so this file doesn't need to call ``init_app()``
by hand. Mangum's ``lifespan='auto'`` runs the FastAPI startup once
per Lambda cold start.
"""

from __future__ import annotations

import logging
import os
import sys

# Add project root to Python path — matches the prior Flask handler.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lambda_handler")

from app import app  # noqa: E402 — sys.path setup must precede import
from mangum import Mangum  # noqa: E402

# lifespan='auto' runs the FastAPI startup on cold start; on Lambda the
# shutdown side never fires (container just disappears), which is what
# FastAPI's lifespan already tolerates.
handler = Mangum(app, lifespan="auto")

logger.info("Lambda handler initialized (FastAPI + Mangum ASGI)")


def lambda_handler(event, context):  # type: ignore[no-untyped-def]
    """AWS Lambda entry point.

    Args:
        event: API Gateway event payload.
        context: Lambda context object.

    Returns:
        API Gateway response.
    """
    try:
        request_context = event.get("requestContext", {})
        http_context = request_context.get("http", {})
        method = http_context.get("method", "UNKNOWN")
        path = http_context.get("path", "/")
        logger.info("Incoming request: %s %s", method, path)

        response = handler(event, context)

        logger.info("Response status: %s", response.get("statusCode", "UNKNOWN"))
        return response
    except Exception:
        logger.exception("Lambda handler error")
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": '{"error": "Internal server error"}',
        }
