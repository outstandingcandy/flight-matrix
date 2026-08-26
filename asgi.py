"""ASGI entry point for uvicorn / Mangum.

Parallel to ``wsgi.py``. ``wsgi.py`` still exists and still exports the
Flask ``app`` — that's the roll-back target if anything on the FastAPI
side goes wrong in production. Once the FastAPI cut-over settles and
Flask is retired, ``wsgi.py`` (and its ``asgiref.wsgi.WsgiToAsgi``
usage) come out.

Startup for FastAPI happens inside the app's ASGI ``lifespan`` handler
(see ``app.create_app``), so unlike ``wsgi.py`` this module does NOT
call any ``init_app()``: importing ``app.app`` is enough. The lifespan
handler runs as soon as the ASGI server starts serving traffic.

Use:
    uvicorn asgi:app --host 0.0.0.0 --port 8000
    gunicorn asgi:app -k uvicorn.workers.UvicornWorker --workers 2
"""

from __future__ import annotations

from app import app

__all__ = ["app"]
