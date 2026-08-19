#!/usr/bin/env python3
"""
Distributed Web Scraper Entry Point

Drives the submodule Worker against flight-matrix's Postgres/SQLite queue.

Usage:
    # Run worker against the configured queue (distributed mode)
    python -m src.scraper_main --config config/config.yaml

    # Restrict to specific scraper types
    python -m src.scraper_main --config config/config.yaml --scrapers jetphotos

    # Show queue status
    python -m src.scraper_main --config config/config.yaml --status

    # Populate queue from aircraft_static_info
    python -m src.scraper_main --config config/config.yaml --populate

    # Run one task right now without hitting the queue
    python -m src.scraper_main --scrapers jetphotos --task N703PA --local

Environment:
    DISPLAY: X display for the browser (default :55)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure project root is in path when running directly
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("scraper_main")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config(config_path: str) -> dict[str, Any]:
    from src.utils.yaml_config import YAMLConfig

    return YAMLConfig(config_path).config


def get_database_url(config: dict[str, Any]) -> str:
    from src.utils.yaml_config import YAMLConfig

    yaml_config = YAMLConfig.__new__(YAMLConfig)
    yaml_config.config = config
    yaml_config.config_path = None
    return yaml_config.get_database_config().get("url", "")


# ---------------------------------------------------------------------------
# Queue ops (synchronous, reuse the flight-matrix TaskQueue directly)
# ---------------------------------------------------------------------------


def show_status(config: dict[str, Any]) -> None:
    from src.scraper.task_queue import TaskQueue

    queue = TaskQueue(get_database_url(config))
    try:
        stats = queue.get_queue_stats()
    except Exception as e:
        logger.error(f"Failed to get queue stats: {e}")
        return

    print("\n" + "=" * 60)
    print("SCRAPER QUEUE STATUS")
    print("=" * 60)
    print("\nTask Counts:")
    print(f"  Pending:    {stats.get('total_pending', 0)}")
    print(f"  Processing: {stats.get('total_processing', 0)}")
    print(f"  Completed:  {stats.get('total_completed', 0)}")
    print(f"  No Data:    {stats.get('total_no_data', 0)}")
    print(f"  Failed:     {stats.get('total_failed', 0)}")
    print(f"\nActive Workers: {stats.get('active_workers', 0)}")
    pending_by_type = stats.get("pending_by_type", {})
    if pending_by_type:
        print("\nPending by Type:")
        for task_type, count in pending_by_type.items():
            print(f"  {task_type}: {count}")
    print("=" * 60 + "\n")


def populate_queue(config: dict[str, Any], limit: int = 100) -> None:
    from sqlalchemy import create_engine, text

    from src.scraper.task_queue import TaskQueue

    database_url = get_database_url(config)
    queue = TaskQueue(database_url)
    queue.ensure_tables_exist()

    engine = create_engine(database_url)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT registration
                FROM aircraft_static_info
                WHERE registration IS NOT NULL
                AND registration != ''
                AND (images_downloaded IS NULL OR images_downloaded = false)
                ORDER BY last_updated DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        registrations = [row[0] for row in result]

    if not registrations:
        logger.info("No registrations found that need images")
        return
    logger.info(f"Found {len(registrations)} registrations needing images")
    tasks = [
        {"task_type": "jetphotos", "task_key": reg, "payload": {}, "priority": 0}
        for reg in registrations
    ]
    added = queue.add_tasks_bulk(tasks)
    logger.info(f"Added {added} tasks to queue")


def populate_fr24_queue(
    config: dict[str, Any],
    task_types: list[str] | None = None,
) -> None:
    from src.scraper.task_queue import TaskQueue

    queue = TaskQueue(get_database_url(config))
    queue.ensure_tables_exist()

    scraper_config = config.get("scraper", {}).get("scrapers", {})
    if task_types is None:
        if scraper_config.get("fr24_airport", {}).get("enabled", False):
            task_types = ["fr24_airport"]
        else:
            task_types = ["fr24_arrivals", "fr24_departures"]

    total_added = 0
    for task_type in task_types:
        type_config = scraper_config.get(task_type, {})
        if not type_config.get("enabled", False):
            logger.info(f"Skipping {task_type}: not enabled in config")
            continue
        airports = type_config.get("airports", [])
        if not airports:
            logger.info(f"Skipping {task_type}: no airports configured")
            continue

        payload: dict[str, Any] = {}
        if "max_load_more_clicks" in type_config:
            payload["max_clicks"] = type_config["max_load_more_clicks"]
        tasks = [
            {
                "task_type": task_type,
                "task_key": airport.upper().strip(),
                "payload": payload,
                "priority": type_config.get("priority", 0),
            }
            for airport in airports
            if airport and airport.strip()
        ]
        if tasks:
            added = queue.add_tasks_bulk(tasks)
            logger.info(f"Added {added} {task_type} tasks for airports: {airports}")
            total_added += added

    if total_added == 0:
        logger.warning("No FR24 tasks added. Check config.scraper.scrapers.fr24_* airports list")
    else:
        logger.info(f"Total FR24 tasks added: {total_added}")


# ---------------------------------------------------------------------------
# Scraper wiring
# ---------------------------------------------------------------------------


def _build_scraper_configs(
    config: dict[str, Any],
    database_url: str,
    local_mode: bool,
    no_db: bool,
    max_notes: int | None,
    max_comments: int | None,
    max_replies: int | None,
) -> dict[str, tuple[type, dict[str, Any]]]:
    """Produce (scraper_class, merged_config) for every scraper type we know."""
    from resilient_scraper.scrapers.aviation.adsbx_map import ADSBxMapScraper
    from resilient_scraper.scrapers.aviation.airport_data import AirportDataScraper
    from resilient_scraper.scrapers.aviation.fr24_aircraft import FR24AircraftScraper
    from resilient_scraper.scrapers.aviation.fr24_airport import (
        FR24AirportArrivalsScraper,
        FR24AirportDeparturesScraper,
        FR24AirportScraper,
    )
    from resilient_scraper.scrapers.aviation.fr24_map import FR24MapScraper
    from resilient_scraper.scrapers.aviation.jetphotos import JetPhotosScraper

    scraper_config = config.get("scraper", {}).get("scrapers", {})
    image_config = config.get("image_download", {})
    s3_config = image_config.get("s3", {})
    email_config = config.get("email", {}).get("smtp", {})

    configs: dict[str, tuple[type, dict[str, Any]]] = {}

    # Aviation scrapers
    jp_cfg = scraper_config.get("jetphotos", {})
    configs["jetphotos"] = (
        JetPhotosScraper,
        {
            "max_images_per_aircraft": image_config.get("max_images_per_aircraft", 3),
            "images_dir": image_config.get("images_dir", "data/jetphotos_images"),
            "collect_all_metadata": image_config.get("collect_all_metadata", True),
            "download_all_images": image_config.get("download_all_images", True),
            "max_pages": image_config.get("max_pages", 50),
            "s3_upload": s3_config.get("enabled", False),
            "s3_bucket": s3_config.get("bucket", ""),
            "s3_prefix": s3_config.get("prefix", "data/jetphotos_images"),
            "delete_local_after_upload": s3_config.get("delete_local_after_upload", False),
            **jp_cfg,
        },
    )

    for type_name in ("fr24_arrivals", "fr24_departures", "fr24_airport"):
        type_cfg = scraper_config.get(type_name, {})
        cls = {
            "fr24_arrivals": FR24AirportArrivalsScraper,
            "fr24_departures": FR24AirportDeparturesScraper,
            "fr24_airport": FR24AirportScraper,
        }[type_name]
        configs[type_name] = (
            cls,
            {
                "max_load_more_clicks": type_cfg.get("max_load_more_clicks", 10),
                "load_more_delay": type_cfg.get("load_more_delay", 2.0),
                **type_cfg,
            },
        )

    map_cfg = scraper_config.get("fr24_map", {})
    configs["fr24_map"] = (
        FR24MapScraper,
        {
            "wait_for_load": map_cfg.get("wait_for_load", 15),
            "save_debug_html": map_cfg.get("save_debug_html", False),
            **map_cfg,
        },
    )

    adsbx_cfg = scraper_config.get("adsbx_map", {})
    # "target" routes sink output:
    #   "military"  (default) → adsbx_military_positions
    #   "positions"           → adsbx_positions (full fleet, own retention)
    #   "snapshots"           → aircraft_snapshots (the table the RapidAPI track
    #                           service writes)
    # The latter two imply military_only=False so the sink sees the whole fleet;
    # otherwise the scraper drops ~97% of rows before they reach the DB.
    adsbx_target = str(adsbx_cfg.get("target", "military")).lower()
    default_military_only = adsbx_target not in ("snapshots", "positions")
    configs["adsbx_map"] = (
        ADSBxMapScraper,
        {
            "wait_for_load": adsbx_cfg.get("wait_for_load", 15),
            "collect_duration": adsbx_cfg.get("collect_duration", 60),
            "military_only": adsbx_cfg.get("military_only", default_military_only),
            "save_debug_html": adsbx_cfg.get("save_debug_html", False),
            "target": adsbx_target,
            # `military_only` and `target` are resolved above and must survive
            # the spread of the raw YAML, which would otherwise put back the
            # unnormalised casing and the un-defaulted filter.
            **{k: v for k, v in adsbx_cfg.items() if k not in ("military_only", "target")},
        },
    )

    ac_cfg = scraper_config.get("fr24_aircraft", {})
    configs["fr24_aircraft"] = (
        FR24AircraftScraper,
        {
            "max_load_earlier_clicks": ac_cfg.get("max_load_earlier_clicks", 0),
            "load_more_delay": ac_cfg.get("load_more_delay", 2.0),
            **ac_cfg,
        },
    )

    ad_cfg = scraper_config.get("airport_data", {})
    configs["airport_data"] = (
        AirportDataScraper,
        {
            "screenshots_dir": ad_cfg.get("screenshots_dir", "data/airport_data_screenshots"),
            "s3_upload": s3_config.get("enabled", False),
            "s3_bucket": s3_config.get("bucket", ""),
            "s3_prefix": ad_cfg.get("s3_prefix", "data/airport_data_raw"),
            "max_pages_per_manufacturer": ad_cfg.get("max_pages_per_manufacturer", 500),
            "skip_existing": ad_cfg.get("skip_existing", True),
            **ad_cfg,
        },
    )

    # Social / aviation-photos scrapers (optional submodule extras)
    def _xhs_base(section: str, default_extras: dict[str, Any]) -> dict[str, Any]:
        cfg = scraper_config.get(section, {})
        return {
            "database_url": "" if no_db else database_url,
            "screenshots_dir": cfg.get("screenshots_dir", "data/xiaohongshu_screenshots"),
            "local_mode": local_mode,
            "login_alert_email": cfg.get("login_alert_email", ""),
            "smtp_server": email_config.get("server", "smtp.qq.com"),
            "smtp_port": email_config.get("port", 465),
            "smtp_sender": email_config.get("sender", ""),
            "smtp_password": email_config.get("password", ""),
            **default_extras,
            **cfg,
        }

    try:
        from resilient_scraper.scrapers.xiaohongshu import (
            XiaohongshuFollowingScraper,
            XiaohongshuScraper,
            XiaohongshuSearchAuthorScraper,
        )

        xhs_cfg = scraper_config.get("xiaohongshu", {})
        configs["xiaohongshu"] = (
            XiaohongshuScraper,
            {
                **_xhs_base("xiaohongshu", {}),
                "max_notes": max_notes if max_notes is not None else xhs_cfg.get("max_notes", 50),
                "max_comments_per_note": (
                    max_comments
                    if max_comments is not None
                    else xhs_cfg.get("max_comments_per_note", 100)
                ),
                "max_replies_per_comment": (
                    max_replies
                    if max_replies is not None
                    else xhs_cfg.get("max_replies_per_comment", 50)
                ),
                "images_dir": xhs_cfg.get("images_dir", "data/xiaohongshu_images"),
                "s3_upload": s3_config.get("enabled", False),
                "s3_bucket": s3_config.get("bucket", ""),
                "s3_prefix": xhs_cfg.get("s3_prefix", "data/xiaohongshu_images"),
                "delete_local_after_upload": s3_config.get("delete_local_after_upload", False),
            },
        )
        configs["xiaohongshu_following"] = (
            XiaohongshuFollowingScraper,
            _xhs_base(
                "xiaohongshu_following",
                {
                    "max_following": scraper_config.get("xiaohongshu_following", {}).get(
                        "max_following", 1000
                    ),
                    "wait_for_login": True,
                    "login_timeout": 300,
                },
            ),
        )
        configs["xiaohongshu_search_author"] = (
            XiaohongshuSearchAuthorScraper,
            _xhs_base(
                "xiaohongshu_search_author",
                {
                    "max_results": scraper_config.get("xiaohongshu_search_author", {}).get(
                        "max_results", 20
                    ),
                    "wait_for_login": True,
                    "login_timeout": 300,
                },
            ),
        )
    except ModuleNotFoundError:
        logger.debug("Xiaohongshu scrapers unavailable; skipping registration")

    try:
        from resilient_scraper.scrapers.planespotters import PlanespottersScraper

        ps_cfg = scraper_config.get("planespotters", {})
        configs["planespotters"] = (
            PlanespottersScraper,
            {
                "database_url": database_url,
                "screenshots_dir": ps_cfg.get("screenshots_dir", "data/planespotters_screenshots"),
                "s3_upload": s3_config.get("enabled", False),
                "s3_bucket": s3_config.get("bucket", ""),
                "s3_prefix": ps_cfg.get("s3_prefix", "data/planespotters_raw"),
                "max_pages_per_family": ps_cfg.get("max_pages_per_family", 50),
                "skip_existing": ps_cfg.get("skip_existing", True),
                "cookies_file": ps_cfg.get("cookies_file", "www.planespotters.net_cookies.txt"),
                **ps_cfg,
            },
        )
    except ModuleNotFoundError:
        logger.debug("Planespotters scraper unavailable; skipping")

    return configs


def _build_storage(config: dict[str, Any] | None) -> Any:
    """Build object storage for the active deployment target.

    Args:
        config: Loaded YAML configuration, or ``None`` when the caller has none.

    Returns:
        An :class:`~src.storage.base.ObjectStorage`, or ``None`` if no provider
        could be configured. ``None`` is a supported outcome: it disables the
        features that need storage (thumbnails) rather than failing the scrape.
    """
    if config is None:
        return None

    from src.core.exceptions import StorageError
    from src.storage import StorageFactory
    from src.utils.yaml_config import YAMLConfig

    # Wrap the already-loaded dict rather than re-reading the file: `__init__`
    # reloads .env and re-resolves the include tree. Only `config` is needed —
    # `StorageFactory` reads it through `get()`, which is also what interpolates
    # `${S3_BUCKET_NAME}` and friends.
    yaml_config = YAMLConfig.__new__(YAMLConfig)
    yaml_config.config = config
    try:
        return StorageFactory.create(yaml_config)
    except StorageError as e:
        logger.warning(f"Object storage unavailable; thumbnails disabled: {e}")
        return None


def _build_sinks_and_augment_configs(
    configs: dict[str, tuple[type, dict[str, Any]]],
    database_url: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Instantiate the sink for each aviation scraper and wire callbacks into its config.

    Args:
        configs: Scraper ``task_type → (class, config)`` map, augmented in place
            with the callbacks each sink provides.
        database_url: SQLAlchemy URL; an empty value builds no sinks at all.
        config: Loaded YAML configuration, for sinks that need more than the
            database — currently only JetPhotos, which writes thumbnails to
            object storage.

    Returns a mapping ``task_type → sink`` so callers can bind the sink's
    on_success/on_failure onto the scraper after instantiation.
    """
    from src.scraper.sinks.adsbx_map_sink import ADSBxMapSink
    from src.scraper.sinks.airport_data_sink import AirportDataSink
    from src.scraper.sinks.fr24_aircraft_sink import FR24AircraftSink
    from src.scraper.sinks.fr24_airport_sink import FR24AirportSink
    from src.scraper.sinks.fr24_map_sink import FR24MapSink
    from src.scraper.sinks.jetphotos_sink import JetPhotosSink
    from src.scraper.task_queue import TaskQueue

    if not database_url:
        return {}

    sinks: dict[str, Any] = {}

    if "fr24_map" in configs:
        sinks["fr24_map"] = FR24MapSink(database_url)
    if "adsbx_map" in configs:
        # Target chooses the downstream table:
        #   "military"  → adsbx_military_positions (default; original behavior)
        #   "positions" → adsbx_positions, the full fleet. Same columns as the
        #                 military table, but no `raw_data` copy of the source
        #                 row and no aircraft_static_info bootstrap, because at
        #                 full-fleet volume both of those are what hurt.
        #   "snapshots" → aircraft_snapshots (same table as the RapidAPI track
        #                 service, so the two producers are interchangeable)
        from src.scraper.sinks.adsbx_map_sink import POSITIONS_TABLE

        _adsbx_cfg = configs["adsbx_map"][1]
        _target = str(_adsbx_cfg.get("target", "military")).lower()
        if _target == "snapshots":
            from src.scraper.sinks.adsbx_snapshots_sink import ADSBxSnapshotsSink

            sinks["adsbx_map"] = ADSBxSnapshotsSink(database_url)
        elif _target == "positions":
            sinks["adsbx_map"] = ADSBxMapSink(database_url, table=POSITIONS_TABLE)
        else:
            sinks["adsbx_map"] = ADSBxMapSink(database_url)
    if "fr24_aircraft" in configs:
        sinks["fr24_aircraft"] = FR24AircraftSink(database_url)
    for t in ("fr24_airport", "fr24_arrivals", "fr24_departures"):
        if t in configs:
            hint = {"fr24_arrivals": "arrival", "fr24_departures": "departure"}.get(t, "")
            sinks[t] = FR24AirportSink(database_url, flight_type_hint=hint)

    if "airport_data" in configs:
        task_queue = TaskQueue(database_url)
        sink = AirportDataSink(database_url, task_queue=task_queue)
        cls, cfg = configs["airport_data"]
        cfg.setdefault("persist_aircraft_callback", sink.persist_aircraft)
        cfg.setdefault("add_task_callback", sink.add_tasks)
        sinks["airport_data"] = sink

    if "jetphotos" in configs:
        cls, cfg = configs["jetphotos"]
        # Everything the JetPhotos scraper stores -- the images, the saved page
        # HTML, and the thumbnails derived from them -- goes through
        # ObjectStorage, so it lands on whichever provider this target uses
        # rather than only on S3.
        storage = _build_storage(config)
        jetphotos_sink = JetPhotosSink(
            database_url,
            storage=storage,
            images_dir=str(cfg.get("images_dir", "")),
        )
        cfg.setdefault("persist_images_callback", jetphotos_sink.persist_images)
        if storage is not None:
            # Left unset when no provider could be built, so the scraper keeps
            # its own boto3 path rather than routing uploads into a sink that
            # has nowhere to put them.
            cfg.setdefault("upload_callback", jetphotos_sink.store_object)
        sinks["jetphotos"] = jetphotos_sink

    return sinks


def _build_registry(
    active_types: list[str],
    configs: dict[str, tuple[type, dict[str, Any]]],
) -> Any:
    """Build a submodule ScraperRegistry populated with the requested types."""
    from resilient_scraper.service.registry import ScraperRegistry

    registry = ScraperRegistry()
    for task_type in active_types:
        entry = configs.get(task_type)
        if entry is None:
            logger.warning(f"Unknown scraper type: {task_type}")
            continue
        scraper_cls, scraper_cfg = entry
        registry.register(scraper_cls, config=scraper_cfg)
    return registry


def _build_local_source(
    task_type: str,
    config: dict[str, Any],
    database_url: str,
    from_queue: bool,
) -> Any:
    """Construct a LocalTaskSource for local-mode scraping.

    In local mode each scraper type has its own strategy for deciding which
    targets to work on: polling aircraft_static_info, a YAML list, scraper_tasks,
    etc. This function centralises that wiring.
    """
    if from_queue:
        from src.scraper.sources.queue_source import QueueTaskSource

        return QueueTaskSource(task_type=task_type, database_url=database_url, limit=10)

    if task_type == "jetphotos":
        from src.scraper.sources.jetphotos_source import JetPhotosTaskSource

        return JetPhotosTaskSource(database_url=database_url, config=config)

    if task_type in ("fr24_arrivals", "fr24_departures", "fr24_airport"):
        from src.scraper.sources.fr24_airport_source import FR24AirportTaskSource

        return FR24AirportTaskSource(task_type=task_type, config=config, database_url=database_url)

    if task_type == "fr24_map":
        from src.scraper.sources.fr24_map_source import FR24MapTaskSource

        return FR24MapTaskSource(config=config, database_url=database_url)

    if task_type == "adsbx_map":
        from src.scraper.sources.adsbx_map_source import ADSBxMapTaskSource

        return ADSBxMapTaskSource(config=config, database_url=database_url)

    if task_type == "xiaohongshu":
        # Xiaohongshu source uses Postgres-only SQL (make_interval, jsonb_*);
        # on SQLite every poll raises and returns []. Skip the registration
        # entirely so the log stays quiet and the round-robin doesn't burn
        # slots on a source that can't produce tasks.
        if database_url.startswith("sqlite"):
            logger.info(
                "Skipping xiaohongshu source on SQLite backend (requires Postgres-only SQL)"
            )
            return None

        from src.scraper.sources.xiaohongshu_source import XiaohongshuAuthorSource

        return XiaohongshuAuthorSource(database_url=database_url, config=config)

    logger.warning(f"No local task source available for {task_type}")
    return None


def _default_active_types(config: dict[str, Any]) -> list[str]:
    scraper_config = config.get("scraper", {}).get("scrapers", {})
    active: list[str] = []
    if scraper_config.get("jetphotos", {}).get("enabled", True):
        active.append("jetphotos")
    if scraper_config.get("fr24_airport", {}).get("enabled", False):
        active.append("fr24_airport")
    elif scraper_config.get("fr24_arrivals", {}).get("enabled", False):
        active.append("fr24_arrivals")
    if scraper_config.get("fr24_departures", {}).get("enabled", False):
        active.append("fr24_departures")
    for t in (
        "xiaohongshu",
        "xiaohongshu_following",
        "xiaohongshu_search_author",
        "fr24_map",
        "adsbx_map",
        "airport_data",
        "planespotters",
        "fr24_aircraft",
    ):
        if scraper_config.get(t, {}).get("enabled", False):
            active.append(t)
    return active


def _build_service_settings(
    config: dict[str, Any],
    database_url: str,
    worker_id: str | None,
    pool_size: int | None,
) -> Any:
    """Build a submodule ServiceSettings from the flight-matrix YAML config."""
    from resilient_scraper.service.config import (
        BrowserSettings,
        DatabaseSettings,
        S3Settings,
        ServiceSettings,
        WorkerSettings,
    )

    scraper_cfg = config.get("scraper", {})
    browser_cfg = scraper_cfg.get("browser_pool", {})
    image_cfg = config.get("image_download", {}).get("s3", {})

    db_settings = DatabaseSettings(url=database_url or "", pool_size=10)
    worker_settings = WorkerSettings(
        id=worker_id or "",
        poll_interval=scraper_cfg.get("poll_interval", 5.0),
        heartbeat_interval=scraper_cfg.get("heartbeat_interval", 30.0),
        task_timeout=scraper_cfg.get("task_timeout", 300.0),
        stale_task_minutes=scraper_cfg.get("stale_task_minutes", 5),
    )
    browser_settings = BrowserSettings(
        pool=True,
        size=pool_size if pool_size is not None else browser_cfg.get("size", 1),
        max_tasks_per_browser=browser_cfg.get("max_tasks_per_browser", 50),
        headless=browser_cfg.get("headless", False),
    )
    # Drop unresolved ${VAR} placeholders — common in local dev where the
    # real S3 bucket env var isn't set. Otherwise the submodule Worker would
    # force-enable S3 with a literal "${S3_BUCKET_NAME}" and every upload
    # would fail boto3 parameter validation.
    raw_bucket = image_cfg.get("bucket", "") or ""
    resolved_bucket = "" if ("${" in raw_bucket or not raw_bucket.strip()) else raw_bucket
    s3_settings = S3Settings(
        bucket=resolved_bucket,
        prefix=image_cfg.get("prefix", ""),
        delete_local_after_upload=image_cfg.get("delete_local_after_upload", False),
    )
    return ServiceSettings(
        db=db_settings,
        worker=worker_settings,
        browser=browser_settings,
        s3=s3_settings,
        log_level=config.get("logging", {}).get("level", "INFO"),
    )


async def run_worker(
    config: dict[str, Any],
    scrapers: list[str] | None = None,
    worker_id: str | None = None,
    local_mode: bool = False,
    task_key: str | None = None,
    pool_size: int | None = None,
    no_db: bool = False,
    max_notes: int | None = None,
    max_comments: int | None = None,
    max_replies: int | None = None,
    start_page: int = 1,
    from_queue: bool = False,
) -> None:
    """Assemble queue + registry + sinks and hand off to the submodule Worker."""
    from resilient_scraper.service.worker import Worker

    from src.scraper.async_task_queue import AsyncTaskQueue
    from src.scraper.cli_task_queue import CLITaskQueue
    from src.scraper.sinks.base import bind_sink
    from src.scraper.task_queue import TaskQueue as FlightTaskQueue

    database_url = get_database_url(config)

    # Build scraper (class, config) map
    scraper_configs = _build_scraper_configs(
        config,
        database_url=database_url if not no_db else "",
        local_mode=local_mode,
        no_db=no_db,
        max_notes=max_notes,
        max_comments=max_comments,
        max_replies=max_replies,
    )

    # Seed per-task CLI overrides into scraper configs
    if start_page > 1:
        for _, (_, cfg) in scraper_configs.items():
            cfg.setdefault("start_page", start_page)

    # Sinks: write to flight-matrix tables from on_success and provide
    # persist_*_callback / add_task_callback hooks to scrapers that need them.
    sinks = (
        _build_sinks_and_augment_configs(scraper_configs, database_url, config) if not no_db else {}
    )

    # Which scraper types should this process serve?
    active_types = scrapers if scrapers else _default_active_types(config)
    registry = _build_registry(active_types, scraper_configs)
    if not registry.task_types:
        logger.error("No scrapers active; nothing to run")
        return

    # Build the queue
    queue: Any
    cli_queue: CLITaskQueue | None = None
    if local_mode and task_key:
        # One-shot CLI mode: a fake queue that serves exactly one task.
        if len(active_types) != 1:
            logger.error("--task requires exactly one scraper type via --scrapers")
            return
        scraper_type = active_types[0]
        # Map scrapers expect coordinates in the payload, not the task_key.
        # Accept task_key in the shape "lat,lon[,zoom]" and split it apart.
        cli_payload: dict[str, Any] = {}
        if scraper_type in ("fr24_map", "adsbx_map") and "," in task_key:
            parts = [p.strip() for p in task_key.split(",")]
            try:
                cli_payload["lat"] = float(parts[0])
                cli_payload["lon"] = float(parts[1])
                if len(parts) >= 3:
                    cli_payload["zoom"] = int(parts[2])
            except (ValueError, IndexError):
                logger.error(
                    "%s --task must be 'lat,lon[,zoom]' (got %r)",
                    scraper_type,
                    task_key,
                )
                return
            if scraper_type == "adsbx_map":
                cli_payload.setdefault("dbFlags", 1)
        cli_queue = CLITaskQueue(scraper_type, task_key, payload=cli_payload)
        queue = cli_queue
        logger.info(f"CLI one-shot mode: {scraper_type}:{task_key} payload={cli_payload}")
    elif local_mode:
        # Local mode without --task: each scraper polls its own domain table
        # (e.g. aircraft_static_info for JetPhotos). No scraper_tasks involved.
        from src.scraper.local_task_queue import LocalTaskQueue

        local_queue = LocalTaskQueue()
        for task_type in active_types:
            source = _build_local_source(task_type, config, database_url, from_queue)
            if source is not None:
                local_queue.register_source(source)
        if not local_queue.task_types:
            logger.error(
                "Local mode requested but no LocalTaskSource could be built "
                f"for types: {active_types}"
            )
            return
        queue = local_queue
        logger.info(f"Local mode active with sources: {local_queue.task_types}")
    else:
        inner_queue = FlightTaskQueue(database_url)
        inner_queue.ensure_tables_exist()
        queue = AsyncTaskQueue(inner_queue)

    # Build settings
    settings = _build_service_settings(
        config,
        database_url=database_url,
        worker_id=worker_id,
        pool_size=pool_size,
    )

    # Submodule Worker ignores the sinks directly, but our scraper classes
    # call on_success (chained via bind_sink) after they're created by the
    # registry. We attach a factory hook by subclassing the registry.create
    # so sinks get bound right when the Worker instantiates each scraper.
    if sinks:
        _wrap_registry_with_sinks(registry, sinks, bind_sink)

    worker = Worker(settings=settings, registry=registry, queue=queue)

    # In CLI mode, stop the worker once the task has been processed.
    async def _run_with_cli_stop() -> None:
        worker_task = asyncio.create_task(worker.run())
        if cli_queue is not None:
            await cli_queue.wait_for_completion()
            # The Worker polls and will observe no more tasks; signal shutdown.
            worker._shutdown.set()
        await worker_task

    await _run_with_cli_stop()


def _wrap_registry_with_sinks(
    registry: Any,
    sinks: dict[str, Any],
    bind_sink: Any,
) -> None:
    """Monkey-patch registry.create so created scrapers get their sink bound.

    submodule's ScraperRegistry doesn't know about sinks; rather than changing
    its signature we intercept ``create`` to run ``bind_sink(scraper, sink)``
    right after construction.
    """
    original_create = registry.create

    def create_with_sink(task_type: str) -> Any:
        scraper = original_create(task_type)
        if scraper is not None:
            sink = sinks.get(task_type)
            if sink is not None:
                bind_sink(scraper, sink)
        return scraper

    registry.create = create_with_sink


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flight Matrix scraper worker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--config", "-c", default="config/config.yaml")
    parser.add_argument("--scrapers", "-s", nargs="+")
    parser.add_argument("--worker-id", "-w")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--populate", action="store_true")
    parser.add_argument("--populate-limit", type=int, default=100)
    parser.add_argument("--populate-fr24", action="store_true")
    parser.add_argument(
        "--fr24-type",
        choices=["flights", "arrivals", "departures", "both"],
        default="flights",
    )
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--task", "-t")
    parser.add_argument("--from-queue", action="store_true")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--max-notes", type=int, default=None)
    parser.add_argument("--max-comments", type=int, default=None)
    parser.add_argument("--max-replies", type=int, default=None)

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {args.config}")
        sys.exit(1)
    config = load_config(args.config)

    if args.migrate:
        from src.scraper.task_queue import TaskQueue

        queue = TaskQueue(get_database_url(config))
        queue.ensure_tables_exist()
        logger.info("Migration complete")
        return

    if args.status:
        show_status(config)
        return

    if args.populate:
        populate_queue(config, limit=args.populate_limit)
        return

    if args.populate_fr24:
        task_types_map = {
            "flights": ["fr24_airport"],
            "arrivals": ["fr24_arrivals"],
            "departures": ["fr24_departures"],
            "both": None,
        }
        populate_fr24_queue(config, task_types=task_types_map[args.fr24_type])
        return

    mode_str = "local/CLI" if (args.local and args.task) else "queue"
    logger.info(f"Starting scraper worker in {mode_str} mode...")
    asyncio.run(
        run_worker(
            config=config,
            scrapers=args.scrapers,
            worker_id=args.worker_id,
            local_mode=args.local,
            task_key=args.task,
            pool_size=args.workers,
            no_db=args.no_db,
            max_notes=args.max_notes,
            max_comments=args.max_comments,
            max_replies=args.max_replies,
            start_page=args.start_page,
            from_queue=args.from_queue,
        )
    )


if __name__ == "__main__":
    main()
