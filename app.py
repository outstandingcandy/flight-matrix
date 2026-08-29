"""FastAPI application factory.

Owns the FastAPI runtime for the whole web app — the Flask entry
(``web_app.py`` + ``wsgi.py``) is gone. Runtime state lives in
:mod:`src.web.runtime`; the ASGI ``lifespan`` handler here calls
``runtime.init_app()`` at startup so ``db_manager`` and ``config``
are ready before the first request.

Routes are registered from small per-domain modules under
``src/web/routes/*_fastapi.py``; each one is a thin
:class:`APIRouter` that reads shared state from :mod:`src.web.runtime`
and image / index / service helpers from the sibling modules under
:mod:`src.web`.
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

from src.web.routes.admin_aircraft_fastapi import router as admin_aircraft_router
from src.web.routes.admin_ops_fastapi import import_router as admin_import_router
from src.web.routes.admin_ops_fastapi import scraper_router as admin_scraper_router
from src.web.routes.admin_reports_scraped_fastapi import router as admin_reports_scraped_router
from src.web.routes.admin_users_fastapi import router as admin_users_router
from src.web.routes.aircraft_fastapi import router as aircraft_router
from src.web.routes.airports_fastapi import router as airports_router
from src.web.routes.auth_fastapi import router as auth_router
from src.web.routes.flight_schedules_fastapi import router as flight_schedules_router
from src.web.routes.ingest_fastapi import router as ingest_router
from src.web.routes.native_auth_fastapi import router as native_auth_router
from src.web.routes.pages_fastapi import router as pages_router
from src.web.routes.search_fastapi import router as search_router
from src.web.routes.user_fastapi import router as user_router

logger = logging.getLogger("app")


def _mask_database_url(url: str) -> str:
    """Elide the password from a SQLAlchemy URL for log output.

    Local copy narrowed to the shape actually logged here. The
    canonical version lives in ``src.utils.database.mask_database_url``.
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

    Runs :func:`src.web.runtime.init_app` — the single writer of the
    process-wide ``db_manager`` / ``config`` singletons every helper
    reads from — and mirrors them onto ``app.state`` for handlers that
    reach through ``request.app.state.db_manager``.

    Nothing closes the DB engine on shutdown; SQLAlchemy handles pool
    teardown at process exit.
    """
    try:
        # ``runtime.init_app()`` populates the process-wide ``db_manager``
        # / ``config`` singletons that every helper module reads. Import
        # inside the lifespan (not at module top) to avoid a circular
        # dependency and to keep FastAPI runnable if the runtime module
        # ever gains an import-time side effect that fails.
        from src.web import runtime

        runtime.init_app()

        # Mirror onto app.state for handlers that reach through
        # ``request.app.state.db_manager`` — same instance either way.
        app.state.config = runtime.config
        app.state.db_manager = runtime.db_manager
        logger.info(
            "FastAPI application initialised (Database URL: %s)",
            _mask_database_url(getattr(runtime.db_manager, "database_url", "unknown")),
        )
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
    app.include_router(auth_router)
    app.include_router(aircraft_router)
    app.include_router(airports_router)
    app.include_router(search_router)
    app.include_router(flight_schedules_router)
    app.include_router(user_router)
    app.include_router(admin_users_router)
    app.include_router(admin_aircraft_router)
    app.include_router(admin_reports_scraped_router)
    app.include_router(admin_scraper_router)
    app.include_router(admin_import_router)
    app.include_router(pages_router)
    app.include_router(ingest_router)
    app.include_router(native_auth_router)

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

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Unwrap ``detail`` when handlers pass a dict, matching Flask's
        flat error shape.

        Handlers written for stage 0 raise ``HTTPException(status_code=401,
        detail={"success": False, "error": "…"})`` to keep the response
        body the Flask endpoint returned. FastAPI's default handler would
        wrap that under ``{"detail": {…}}`` — this override peels it back
        off so ``response.json() == {"success": False, "error": "…"}``,
        which is what every Flask-era test in ``tests/web/`` asserts and
        what the migrated frontend / mobile clients will parse.

        A non-dict ``detail`` (a plain string) keeps FastAPI's default
        shape ``{"detail": "…"}`` — those are FastAPI-native paths
        (Pydantic 422 validation errors, for instance), not migrated
        handlers, and their clients are already used to that shape.
        """
        # `exc.headers` matters for redirects — `require_login` raises
        # HTTPException(302, headers={"Location": "/login"}). Forwarding
        # the headers keeps that Location in the JSONResponse.
        if isinstance(exc.detail, dict):
            return JSONResponse(
                status_code=exc.status_code, content=exc.detail, headers=exc.headers
            )
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
        )

    return app


app = create_app()
