#!/usr/bin/env python3
"""
Distributed Web Scraper Entry Point

Runs a scraper worker with registered scrapers.

Usage:
    # Run worker with default config
    python -m src.scraper_main --config config/config.yaml

    # Run with specific scrapers
    python -m src.scraper_main --config config/config.yaml --scrapers jetphotos

    # Show queue status
    python -m src.scraper_main --config config/config.yaml --status

    # Add tasks from database
    python -m src.scraper_main --config config/config.yaml --populate

    # Run with custom worker ID
    python -m src.scraper_main --config config/config.yaml --worker-id worker-us-west-1

Environment:
    DISPLAY: X display for browser (default :55)

Example systemd service:
    [Unit]
    Description=Flight Matrix Scraper Worker
    After=network.target

    [Service]
    Type=simple
    User=ubuntu
    Environment=DISPLAY=:55
    WorkingDirectory=/home/ubuntu/Project/flight-matrix
    ExecStart=/usr/bin/python -m src.scraper_main --config config/config.yaml
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
"""

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

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("scraper_main")


def load_config(config_path: str) -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config.yaml file.

    Returns:
        Configuration dictionary.
    """
    from src.utils.yaml_config import YAMLConfig

    yaml_config = YAMLConfig(config_path)
    return yaml_config.config


def get_database_url(config: dict[str, Any]) -> str:
    """Extract database URL from config.

    Args:
        config: Configuration dictionary.

    Returns:
        Database URL string.
    """
    from src.utils.yaml_config import YAMLConfig

    yaml_config = YAMLConfig.__new__(YAMLConfig)
    yaml_config.config = config
    yaml_config.config_path = None
    db_config = yaml_config.get_database_config()
    return db_config.get("url", "")


def show_status(config: dict[str, Any]) -> None:
    """Display queue and worker status.

    Args:
        config: Configuration dictionary.
    """
    from src.scraper.task_queue import TaskQueue

    database_url = get_database_url(config)
    queue = TaskQueue(database_url)

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
    """Populate queue with tasks from aircraft_static_info.

    Args:
        config: Configuration dictionary.
        limit: Maximum tasks to add.
    """
    from sqlalchemy import create_engine, text

    from src.scraper.task_queue import TaskQueue

    database_url = get_database_url(config)
    queue = TaskQueue(database_url)

    # Ensure tables exist
    queue.ensure_tables_exist()

    # Get registrations without images
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

    # Add tasks in bulk
    tasks = [
        {
            "task_type": "jetphotos",
            "task_key": reg,
            "payload": {},
            "priority": 0,
        }
        for reg in registrations
    ]

    added = queue.add_tasks_bulk(tasks)
    logger.info(f"Added {added} tasks to queue")


def populate_fr24_queue(
    config: dict[str, Any],
    task_types: list[str] | None = None,
) -> None:
    """Populate queue with FR24 flight tasks from airport list in config.

    Reads airport list from config.scraper.scrapers.fr24_airport.airports
    or config.scraper.scrapers.fr24_arrivals/fr24_departures.airports.

    Args:
        config: Configuration dictionary.
        task_types: List of task types to populate (fr24_airport, fr24_arrivals, fr24_departures).
                   If None, populates fr24_airport if configured, otherwise arrivals/departures.
    """
    from src.scraper.task_queue import TaskQueue

    database_url = get_database_url(config)
    queue = TaskQueue(database_url)

    # Ensure tables exist
    queue.ensure_tables_exist()

    scraper_config = config.get("scraper", {}).get("scrapers", {})

    # Determine which task types to process
    if task_types is None:
        # Prefer fr24_airport (combined), fall back to separate arrivals/departures
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

        # Build payload from config
        payload: dict[str, Any] = {}
        if "max_load_more_clicks" in type_config:
            payload["max_clicks"] = type_config["max_load_more_clicks"]

        # Add tasks for each airport
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
        logger.warning(
            "No FR24 tasks added. Check config.scraper.scrapers.fr24_arrivals/fr24_departures"
        )
    else:
        logger.info(f"Total FR24 tasks added: {total_added}")


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
    """Run the scraper worker.

    Args:
        config: Configuration dictionary.
        scrapers: List of scraper types to enable.
        worker_id: Optional worker identifier.
        local_mode: If True, run in local mode (poll aircraft_static_info directly).
        task_key: Optional task key to process directly (for --task argument).
        pool_size: Optional browser pool size (overrides config).
        start_page: Start page for pagination (planespotters).
        no_db: If True, disable database writes.
        max_notes: Optional override for max notes per account (xiaohongshu).
        max_comments: Optional override for max comments per note (xiaohongshu).
        max_replies: Optional override for max replies per comment (xiaohongshu).
        from_queue: If True in local mode, pull tasks from database queue.
    """
    from resilient_scraper.scrapers.planespotters import PlanespottersScraper
    from resilient_scraper.scrapers.xiaohongshu import (
        XiaohongshuFollowingScraper,
        XiaohongshuScraper,
        XiaohongshuSearchAuthorScraper,
    )

    from src.scraper.scrapers.fr24_aircraft import FR24AircraftScraper
    from src.scraper.scrapers.fr24_airport import (
        FR24AirportArrivalsScraper,
        FR24AirportDeparturesScraper,
        FR24AirportScraper,
    )
    from src.scraper.scrapers.fr24_map import FR24MapScraper
    from src.scraper.scrapers.jetphotos import JetPhotosScraper
    from src.scraper.worker import ScraperWorker

    # Get database URL
    database_url = get_database_url(config)

    # Determine mode
    mode = "local" if local_mode else "distributed"

    # Build CLI payload for task options
    cli_payload: dict[str, Any] = {}
    if start_page > 1:
        cli_payload["start_page"] = start_page

    # Create worker
    worker = ScraperWorker(
        config=config,
        database_url=database_url,
        worker_id=worker_id,
        mode=mode,
        cli_task_key=task_key,
        cli_payload=cli_payload,
        pool_size=pool_size,
        from_queue=from_queue,
    )

    # Register scrapers based on config
    scraper_config = config.get("scraper", {}).get("scrapers", {})

    # Default to all enabled scrapers if none specified
    if scrapers is None:
        scrapers = []
        if scraper_config.get("jetphotos", {}).get("enabled", True):
            scrapers.append("jetphotos")
        # Prefer fr24_airport (combined) over separate arrivals/departures
        if scraper_config.get("fr24_airport", {}).get("enabled", False):
            scrapers.append("fr24_airport")
        elif scraper_config.get("fr24_arrivals", {}).get("enabled", False):
            scrapers.append("fr24_arrivals")
        if scraper_config.get("fr24_departures", {}).get("enabled", False):
            scrapers.append("fr24_departures")
        if scraper_config.get("xiaohongshu", {}).get("enabled", False):
            scrapers.append("xiaohongshu")
        if scraper_config.get("xiaohongshu_following", {}).get("enabled", False):
            scrapers.append("xiaohongshu_following")
        if scraper_config.get("xiaohongshu_search_author", {}).get("enabled", False):
            scrapers.append("xiaohongshu_search_author")
        if scraper_config.get("fr24_map", {}).get("enabled", False):
            scrapers.append("fr24_map")
        if scraper_config.get("airport_data", {}).get("enabled", False):
            scrapers.append("airport_data")
        if scraper_config.get("planespotters", {}).get("enabled", False):
            scrapers.append("planespotters")
        if scraper_config.get("fr24_aircraft", {}).get("enabled", False):
            scrapers.append("fr24_aircraft")

    # Register requested scrapers
    for scraper_type in scrapers:
        if scraper_type == "jetphotos":
            # Merge image_download config with scraper config
            jetphotos_config = scraper_config.get("jetphotos", {})
            image_config = config.get("image_download", {})
            s3_config = image_config.get("s3", {})

            merged_config = {
                "max_images_per_aircraft": image_config.get("max_images_per_aircraft", 3),
                "images_dir": image_config.get("images_dir", "data/jetphotos_images"),
                # Pagination and download settings
                "collect_all_metadata": image_config.get("collect_all_metadata", True),
                "download_all_images": image_config.get("download_all_images", True),
                "max_pages": image_config.get("max_pages", 50),
                # S3 settings
                "s3_upload": s3_config.get("enabled", False),
                "s3_bucket": s3_config.get("bucket", ""),
                "s3_prefix": s3_config.get("prefix", "data/jetphotos_images"),
                "delete_local_after_upload": s3_config.get("delete_local_after_upload", False),
                # Pass database URL for syncing to aircraft_static_info
                "database_url": database_url,
                "sync_to_static_info": True,
                **jetphotos_config,
            }
            worker.register_scraper(JetPhotosScraper, merged_config)

        elif scraper_type in ("fr24_arrivals", "fr24_departures", "fr24_airport"):
            # FR24 flights scraper config
            fr24_config = scraper_config.get(scraper_type, {})
            merged_config = {
                "database_url": database_url,
                "sync_to_database": fr24_config.get("sync_to_database", True),
                "max_load_more_clicks": fr24_config.get("max_load_more_clicks", 10),
                "load_more_delay": fr24_config.get("load_more_delay", 2.0),
                **fr24_config,
            }
            if scraper_type == "fr24_arrivals":
                worker.register_scraper(FR24AirportArrivalsScraper, merged_config)
            elif scraper_type == "fr24_departures":
                worker.register_scraper(FR24AirportDeparturesScraper, merged_config)
            else:  # fr24_airport
                worker.register_scraper(FR24AirportScraper, merged_config)

        elif scraper_type == "fr24_map":
            # FR24 map scraper config
            fr24_map_config = scraper_config.get("fr24_map", {})
            merged_config = {
                "database_url": database_url,
                "sync_to_database": fr24_map_config.get("sync_to_database", True),
                "wait_for_load": fr24_map_config.get("wait_for_load", 15),
                "save_debug_html": fr24_map_config.get("save_debug_html", False),
                **fr24_map_config,
            }
            worker.register_scraper(FR24MapScraper, merged_config)

        elif scraper_type == "fr24_aircraft":
            # FR24 aircraft flight history scraper config
            fr24_aircraft_config = scraper_config.get("fr24_aircraft", {})
            merged_config = {
                "database_url": database_url,
                "sync_to_database": fr24_aircraft_config.get("sync_to_database", True),
                "max_load_earlier_clicks": fr24_aircraft_config.get("max_load_earlier_clicks", 0),
                "load_more_delay": fr24_aircraft_config.get("load_more_delay", 2.0),
                **fr24_aircraft_config,
            }
            worker.register_scraper(FR24AircraftScraper, merged_config)

        elif scraper_type == "xiaohongshu":
            # Xiaohongshu scraper config
            xhs_config = scraper_config.get("xiaohongshu", {})
            email_config = config.get("email", {}).get("smtp", {})
            s3_config = config.get("image_download", {}).get("s3", {})

            # Use CLI override for max_notes if provided, otherwise use config
            effective_max_notes = (
                max_notes if max_notes is not None else xhs_config.get("max_notes", 50)
            )
            # Use CLI override for max_comments if provided, otherwise use config
            effective_max_comments = (
                max_comments
                if max_comments is not None
                else xhs_config.get("max_comments_per_note", 100)
            )
            # Use CLI override for max_replies if provided, otherwise use config
            effective_max_replies = (
                max_replies
                if max_replies is not None
                else xhs_config.get("max_replies_per_comment", 50)
            )

            merged_config = {
                "database_url": "" if no_db else database_url,
                "max_notes": effective_max_notes,
                "max_comments_per_note": effective_max_comments,
                "max_replies_per_comment": effective_max_replies,
                "images_dir": xhs_config.get("images_dir", "data/xiaohongshu_images"),
                "screenshots_dir": xhs_config.get(
                    "screenshots_dir", "data/xiaohongshu_screenshots"
                ),
                # S3 settings
                "s3_upload": s3_config.get("enabled", False),
                "s3_bucket": s3_config.get("bucket", ""),
                "s3_prefix": xhs_config.get("s3_prefix", "data/xiaohongshu_images"),
                "delete_local_after_upload": s3_config.get("delete_local_after_upload", False),
                # Email alert settings (disabled in local mode)
                "local_mode": local_mode,
                "login_alert_email": xhs_config.get("login_alert_email", ""),
                "smtp_server": email_config.get("server", "smtp.qq.com"),
                "smtp_port": email_config.get("port", 465),
                "smtp_sender": email_config.get("sender", ""),
                "smtp_password": email_config.get("password", ""),
                **xhs_config,
            }
            worker.register_scraper(XiaohongshuScraper, merged_config)

        elif scraper_type == "xiaohongshu_following":
            # Xiaohongshu following scraper config
            xhs_following_config = scraper_config.get("xiaohongshu_following", {})
            email_config = config.get("email", {}).get("smtp", {})

            merged_config = {
                "database_url": "" if no_db else database_url,
                "max_following": xhs_following_config.get("max_following", 1000),
                "screenshots_dir": xhs_following_config.get(
                    "screenshots_dir", "data/xiaohongshu_screenshots"
                ),
                # Email alert settings (disabled in local mode)
                "local_mode": local_mode,
                "login_alert_email": xhs_following_config.get("login_alert_email", ""),
                "smtp_server": email_config.get("server", "smtp.qq.com"),
                "smtp_port": email_config.get("port", 465),
                "smtp_sender": email_config.get("sender", ""),
                "smtp_password": email_config.get("password", ""),
                "wait_for_login": xhs_following_config.get("wait_for_login", True),
                "login_timeout": xhs_following_config.get("login_timeout", 300),
                **xhs_following_config,
            }
            worker.register_scraper(XiaohongshuFollowingScraper, merged_config)

        elif scraper_type == "xiaohongshu_search_author":
            # Xiaohongshu search author scraper config
            xhs_search_config = scraper_config.get("xiaohongshu_search_author", {})
            email_config = config.get("email", {}).get("smtp", {})

            merged_config = {
                "database_url": "" if no_db else database_url,
                "max_results": xhs_search_config.get("max_results", 20),
                "screenshots_dir": xhs_search_config.get(
                    "screenshots_dir", "data/xiaohongshu_screenshots"
                ),
                # Email alert settings (disabled in local mode)
                "local_mode": local_mode,
                "login_alert_email": xhs_search_config.get("login_alert_email", ""),
                "smtp_server": email_config.get("server", "smtp.qq.com"),
                "smtp_port": email_config.get("port", 465),
                "smtp_sender": email_config.get("sender", ""),
                "smtp_password": email_config.get("password", ""),
                "wait_for_login": xhs_search_config.get("wait_for_login", True),
                "login_timeout": xhs_search_config.get("login_timeout", 300),
                **xhs_search_config,
            }
            worker.register_scraper(XiaohongshuSearchAuthorScraper, merged_config)

        elif scraper_type == "planespotters":
            # Planespotters scraper config
            ps_config = scraper_config.get("planespotters", {})
            s3_config = config.get("image_download", {}).get("s3", {})
            merged_config = {
                "database_url": database_url,
                "screenshots_dir": ps_config.get(
                    "screenshots_dir", "data/planespotters_screenshots"
                ),
                "s3_upload": s3_config.get("enabled", False),
                "s3_bucket": s3_config.get("bucket", ""),
                "s3_prefix": ps_config.get("s3_prefix", "data/planespotters_raw"),
                "max_pages_per_family": ps_config.get("max_pages_per_family", 50),
                "skip_existing": ps_config.get("skip_existing", True),
                "cookies_file": ps_config.get("cookies_file", "www.planespotters.net_cookies.txt"),
                "use_existing_browser": ps_config.get("use_existing_browser", False),
                "chrome_debug_port": ps_config.get("chrome_debug_port", 9222),
                "wait_for_login": ps_config.get("wait_for_login", True),
                "login_timeout": ps_config.get("login_timeout", 300),
                "login_check_interval": ps_config.get("login_check_interval", 5),
                **ps_config,
            }
            worker.register_scraper(PlanespottersScraper, merged_config)

        elif scraper_type == "airport_data":
            # Airport-data.com scraper config
            from src.scraper.scrapers.airport_data import AirportDataScraper

            ad_config = scraper_config.get("airport_data", {})
            s3_config = config.get("image_download", {}).get("s3", {})
            merged_config = {
                "database_url": database_url,
                "screenshots_dir": ad_config.get(
                    "screenshots_dir", "data/airport_data_screenshots"
                ),
                "s3_upload": s3_config.get("enabled", False),
                "s3_bucket": s3_config.get("bucket", ""),
                "s3_prefix": ad_config.get("s3_prefix", "data/airport_data_raw"),
                "max_pages_per_manufacturer": ad_config.get("max_pages_per_manufacturer", 500),
                "skip_existing": ad_config.get("skip_existing", True),
                **ad_config,
            }
            worker.register_scraper(AirportDataScraper, merged_config)

        else:
            logger.warning(f"Unknown scraper type: {scraper_type}")

    # Run worker
    await worker.run()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Distributed Web Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--config",
        "-c",
        default="config/config.yaml",
        help="Path to config.yaml file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--scrapers",
        "-s",
        nargs="+",
        help="Scraper types to enable (default: all enabled in config)",
    )
    parser.add_argument(
        "--worker-id",
        "-w",
        help="Custom worker identifier",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show queue status and exit",
    )
    parser.add_argument(
        "--populate",
        action="store_true",
        help="Populate queue from aircraft_static_info and exit",
    )
    parser.add_argument(
        "--populate-limit",
        type=int,
        default=100,
        help="Maximum tasks to add when populating (default: 100)",
    )
    parser.add_argument(
        "--populate-fr24",
        action="store_true",
        help="Populate queue with FR24 flight tasks from airport list in config",
    )
    parser.add_argument(
        "--fr24-type",
        choices=["flights", "arrivals", "departures", "both"],
        default="flights",
        help="FR24 task type to populate (default: flights for combined arrivals+departures)",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Run database migration and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run in local mode (no PostgreSQL task queue required, polls aircraft_static_info directly)",
    )
    parser.add_argument(
        "--task",
        "-t",
        help="Task key to process directly (e.g., account ID for xiaohongshu, registration for jetphotos)",
    )
    parser.add_argument(
        "--from-queue",
        action="store_true",
        help="Pull tasks from database queue (requires --local and --scrapers)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Start page for pagination (planespotters scraper). Use to resume from a specific page.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of browser workers (overrides config.scraper.browser_pool.size)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Disable database writes (data will not be saved to PostgreSQL)",
    )
    parser.add_argument(
        "--max-notes",
        type=int,
        default=None,
        help="Override max notes per account for xiaohongshu scraper (for testing)",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=None,
        help="Override max comments per note for xiaohongshu scraper (for testing)",
    )
    parser.add_argument(
        "--max-replies",
        type=int,
        default=None,
        help="Override max replies per comment for xiaohongshu scraper (for testing)",
    )

    args = parser.parse_args()

    # Set log level
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {args.config}")
        sys.exit(1)

    config = load_config(args.config)

    # Handle subcommands
    if args.migrate:
        from src.scraper.task_queue import TaskQueue

        database_url = get_database_url(config)
        queue = TaskQueue(database_url)
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
        # Determine task types based on --fr24-type
        if args.fr24_type == "flights":
            task_types = ["fr24_airport"]
        elif args.fr24_type == "arrivals":
            task_types = ["fr24_arrivals"]
        elif args.fr24_type == "departures":
            task_types = ["fr24_departures"]
        else:
            task_types = None  # Auto-detect based on config
        populate_fr24_queue(config, task_types=task_types)
        return

    # Run worker
    mode_str = "local" if args.local else "distributed"
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
