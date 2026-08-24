"""FastAPI application factory — the successor to :mod:`web_app`.

Migration status (feat/fastapi-migration): this module owns the FastAPI
runtime. Routes are being moved off ``web_app.py`` (Flask) one blueprint at
a time. During the migration both entries are runnable in parallel — the
FastAPI process handles the routes it has taken over, the Flask process
handles everything else. Once every route is migrated ``web_app.py`` and
the Flask/asgiref/flask-cors dependencies come out in one commit.

The order the migration follows lives in the plan file
(``/Users/panda/.claude/plans/ios-app-lucky-sonnet.md``, stage 0). Pilot
is ``src.web.routes.ingest_fastapi`` — one POST endpoint that reuses the
existing Pydantic models from the Flask blueprint.

Startup lives in the ASGI ``lifespan`` handler rather than the module top
level: uvicorn / Mangum both prefer that, and it removes the two-copies
trap ``web_app.init_app()`` had to work around (``python web_app.py``
running the module as ``__main__`` while blueprints ``import web_app``).
The initialised objects hang off ``app.state``; endpoint code reads them
through ``request.app.state.<name>`` or a small dependency helper.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.data.db_manager import DatabaseManager
from src.utils.yaml_config import YAMLConfig
from src.web.routes.ingest_fastapi import router as ingest_router

logger = logging.getLogger("app")


def _mask_database_url(url: str) -> str:
    """Elide the password from a SQLAlchemy URL for log output.

    Copy of ``web_app.mask_database_url`` narrowed to the shape actually
    logged here. Kept local so this module doesn't import from ``web_app``
    (which owns the Flask stack).
    """
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Populate ``app.state`` with the config + DatabaseManager on startup.

    Mirrors ``web_app.init_app()`` — same env vars, same YAMLConfig entry
    point, same DatabaseManager constructor — so the FastAPI and Flask
    entries observe the same runtime state. Neither closes the DB engine
    on shutdown today; SQLAlchemy handles pool teardown at process exit
    and the Flask side never did anything more.
    """
    try:
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
        config = YAMLConfig(config_path)
        db_config = config.get_database_config()
        db_url = os.environ.get("DATABASE_URL", db_config["url"])
        logger.info("Database URL: %s", _mask_database_url(db_url))

        db_manager = DatabaseManager(db_url)

        app.state.config = config
        app.state.db_manager = db_manager
        logger.info("FastAPI application initialised successfully")
        yield
    except Exception:
        logger.exception("Failed to initialise FastAPI application")
        raise


def create_app() -> FastAPI:
    """Build the FastAPI app. Split out so tests can spin a fresh copy."""
    app = FastAPI(
        title="flight-matrix",
        description="Real-time aircraft tracking and analysis system",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Session cookie — same secret as Flask (``FLASK_SECRET_KEY``) so a
    # rollback back to the Flask entry doesn't invalidate every session.
    # Session serialisation formats differ between Flask and Starlette, so
    # the migration note says "users will get logged out once" anyway; the
    # shared secret is defence in depth, not compatibility.
    session_secret = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="flight_matrix_session",
        https_only=os.environ.get("STAGE", "local") != "local",
        same_site="lax",
        max_age=7 * 24 * 60 * 60,  # 7 days, matches Flask PERMANENT_SESSION_LIFETIME
    )

    # CORS — the Flask side had `CORS(app)` unrestricted. Keep permissive
    # for the pilot; stage 1 (auth layer) narrows it to a real allowlist
    # once we know the deployed origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # can't be True with allow_origins=["*"]
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Jinja2 + static assets. Both live at their existing paths so the
    # migrated HTML routes will find them without any rearrangement.
    app.state.templates = Jinja2Templates(directory="web_templates")
    app.mount("/static", StaticFiles(directory="web_static"), name="static")

    # Routers ---
    app.include_router(ingest_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        """Cheap liveness probe. Reports whether ``lifespan`` ran clean."""
        db_ready = getattr(app.state, "db_manager", None) is not None
        config_ready = getattr(app.state, "config", None) is not None
        return {"ok": db_ready and config_ready, "db": db_ready, "config": config_ready}

    @app.exception_handler(Exception)
    async def unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
        """Log the traceback, hand the client a body that says nothing.

        Matches ``src.web.errors.api_error``'s stance: "the detail belongs
        in the log, where it is paired with a traceback; the client gets a
        fixed string." Applied globally so no handler has to remember it.
        """
        logger.exception("Unhandled exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )

    return app


app = create_app()
