"""Service singletons for the multi-user + scraper admin endpoints.

Two entry points:

- :func:`get_multi_user_services` — constructs
  :class:`UserService` / :class:`SubscriptionService` /
  :class:`FilterService` once on first call and reuses. Also runs
  ``db_manager.ensure_multi_user_tables_exist()`` on that first call,
  which is where the ``users`` / ``subscriptions`` / ``user_filters``
  / ``user_cooldowns`` / ``user_usage`` tables get created on a
  fresh DB.
- :func:`get_scraper_db_session` — returns a session against the
  shared ``db_manager`` (so scraper queries hit the same connection
  pool as the web app). Ensures ``init_app`` has run if a request
  arrives before cold-start finished.

The singleton state is module-global and lazily populated on first
call.  :func:`reset_multi_user_services` is the test-only reset hook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.web import runtime

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.services.filter_service import FilterService
    from src.services.subscription_service import SubscriptionService
    from src.services.user_service import UserService

_user_service: UserService | None = None
_subscription_service: SubscriptionService | None = None
_filter_service: FilterService | None = None


def get_multi_user_services() -> tuple[UserService, SubscriptionService, FilterService]:
    """Return ``(user_service, subscription_service, filter_service)``.

    Constructs the three services on first call from ``runtime.db_manager``
    and ``runtime.config``, and calls
    ``db_manager.ensure_multi_user_tables_exist()`` so a fresh SQLite
    DB doesn't 500 the very first request into an admin page.
    """
    global _user_service, _subscription_service, _filter_service

    if _user_service is None:
        from src.services.filter_service import FilterService
        from src.services.subscription_service import SubscriptionService
        from src.services.user_service import UserService

        # Same lazy fallback as get_scraper_db_session — cover the
        # race where a request arrives before the FastAPI lifespan
        # populated the runtime, or between-test fixture ordering
        # that wipes ``src.web.*`` from ``sys.modules``.
        if runtime.db_manager is None:
            runtime.init_app()
        assert runtime.db_manager is not None
        runtime.db_manager.ensure_multi_user_tables_exist()

        _user_service = UserService(runtime.db_manager)
        _subscription_service = SubscriptionService(runtime.db_manager, runtime.config)
        _filter_service = FilterService(runtime.db_manager)

    # The `assert not None` above narrows for mypy; the assignment
    # inside the `if` block wrote all three.
    assert _subscription_service is not None
    assert _filter_service is not None
    return _user_service, _subscription_service, _filter_service


def reset_multi_user_services() -> None:
    """Drop the cached service instances. Test-only hook."""
    global _user_service, _subscription_service, _filter_service
    _user_service = None
    _subscription_service = None
    _filter_service = None


def get_scraper_db_session() -> Session:
    """Return a DB session against the shared ``runtime.db_manager``.

    Runs :func:`runtime.init_app` lazily if a caller races the FastAPI
    lifespan — the scraper monitoring endpoints occasionally hit this
    before startup finishes on a slow cold start.
    """
    if runtime.db_manager is None:
        runtime.init_app()
    assert runtime.db_manager is not None
    return runtime.db_manager.get_session()
