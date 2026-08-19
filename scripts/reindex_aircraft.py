#!/usr/bin/env python3
"""
Build and maintain the OpenSearch index over `aircraft_static_info`.

The index only accelerates the admin aircraft list's text search; PostgreSQL
remains the source of truth, so this script can be re-run at any time and is
the recovery lever whenever the index and the table disagree.

Three modes:

  --incremental   Index rows changed since the index's own watermark. Cheap
                  enough to run from a timer every couple of minutes; falls
                  back to a full pass when the index is empty.
  --full          Index every row into the existing index.
  --recreate      Delete the index and rebuild it. Needed only when the
                  analysers or a field *type* in `src/search/aircraft_index.py`
                  change, since neither can be altered in place. The index is
                  unsearchable while this runs.

Usage:
    python scripts/reindex_aircraft.py --incremental
    python scripts/reindex_aircraft.py --full
    python scripts/reindex_aircraft.py --registration B-1234 --registration N703PA
    python scripts/reindex_aircraft.py --recreate --yes
    python scripts/reindex_aircraft.py --stats
"""

import argparse
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.exceptions import SearchError
from src.data.db_manager import DatabaseManager, mask_database_url
from src.search.aircraft_index import AircraftSearchIndex
from src.search.aircraft_sync import incremental_start, sync_aircraft_index
from src.search.opensearch_client import OpenSearchSettings, build_client
from src.utils.yaml_config import YAMLConfig

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("reindex_aircraft")


def _build(
    args: argparse.Namespace,
) -> tuple[DatabaseManager, AircraftSearchIndex, OpenSearchSettings]:
    """Build the database manager, the index handle and the settings behind them.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The database manager, the index it will be synced into, and the
        resolved OpenSearch settings.

    Raises:
        SystemExit: If OpenSearch is not configured, or is configured but its
            client cannot be built. The unconfigured case exits **0**: the
            systemd timer runs this on every host, including ones that have no
            cluster, and a deployment without search is a choice rather than a
            fault.
    """
    config = YAMLConfig(args.config)
    settings = OpenSearchSettings.from_config(config)

    if not settings.enabled:
        logger.warning(
            "OPENSEARCH_URL is not set; nothing to index. "
            "Set it in /etc/flight-matrix/env to enable aircraft search."
        )
        raise SystemExit(0)

    try:
        client = build_client(settings)
    except SearchError as e:
        raise SystemExit(f"{e}\nSet OPENSEARCH_URL to point at the cluster.") from e

    database_url = os.environ.get("DATABASE_URL", config.get_database_config()["url"])
    logger.info("Database: %s", mask_database_url(database_url))
    logger.info("OpenSearch: %s (index=%s)", settings.url, settings.index)

    index = AircraftSearchIndex(client, index=settings.index, max_results=settings.max_results)
    return DatabaseManager(database_url), index, settings


def main() -> None:
    """Parse arguments and run the requested reindex mode."""
    parser = argparse.ArgumentParser(description="Reindex aircraft into OpenSearch")
    parser.add_argument("--config", default="config/config.yaml", help="Config file path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--incremental", action="store_true", help="Index rows changed since the watermark"
    )
    mode.add_argument("--full", action="store_true", help="Index every row")
    mode.add_argument("--recreate", action="store_true", help="Drop the index and rebuild it")
    mode.add_argument("--stats", action="store_true", help="Report index size and watermark")
    parser.add_argument(
        "--registration",
        action="append",
        default=[],
        metavar="REG",
        help="Index only this registration (repeatable)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Documents per bulk request (default: from config)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt for --recreate"
    )

    args = parser.parse_args()
    if not any([args.incremental, args.full, args.recreate, args.stats, args.registration]):
        parser.print_help()
        return

    db_manager, index, settings = _build(args)
    batch_size = args.batch_size or settings.batch_size

    try:
        if args.stats:
            watermark = index.max_last_updated()
            print(f"\nIndex:     {index.index}")
            print(f"Documents: {index.document_count()}")
            print(f"Watermark: {watermark.isoformat() if watermark else '(empty)'}\n")
            return

        if args.recreate:
            if not args.yes:
                answer = input(
                    f"Delete and rebuild index '{index.index}'? "
                    "It will be unsearchable until the rebuild finishes [y/N] "
                )
                if answer.strip().lower() not in ("y", "yes"):
                    print("Aborted.")
                    return
            logger.warning("Deleting index %s", index.index)
            index.client.indices.delete(index=index.index, ignore_unavailable=True)

        created = index.ensure_index()
        logger.info("Index %s %s", index.index, "created" if created else "already present")

        since = None
        if args.incremental:
            since = incremental_start(index)
            if since is None:
                logger.info("Index is empty; falling back to a full pass")

        stats = sync_aircraft_index(
            db_manager.engine,
            index,
            since=since,
            registrations=args.registration or None,
            batch_size=batch_size,
        )

        print(f"\nIndexed {stats.indexed} aircraft in {stats.batches} batches")
        print(f"Index now holds {index.document_count()} documents\n")
    except SearchError as e:
        raise SystemExit(f"Reindex failed: {e}") from e
    finally:
        db_manager.close()


if __name__ == "__main__":
    main()
