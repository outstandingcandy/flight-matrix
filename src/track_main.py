#!/usr/bin/env python3
"""
Aircraft Tracking Service Entry Point

Runs as an independent process that recalls aircraft data from API
and stores snapshots to database.

Usage:
    # Run continuously
    python -m src.track_main --config config/config.yaml

    # Run one cycle and exit
    python -m src.track_main --config config/config.yaml --once

    # Show status and exit
    python -m src.track_main --config config/config.yaml --status
"""

import argparse
import asyncio
import logging
import os
import sys

# Add project root to path for absolute imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.track_service import TrackService
from src.utils.logging_config import setup_logging

logger = logging.getLogger("track_main")


def print_status(service: TrackService):
    """Print service status to console."""
    status = service.get_service_status()

    print("\n=== Track Service Status ===")
    print(f"Running: {status['is_running']}")
    print(f"Update interval: {status['update_interval']}s")
    print(f"Cycle count: {status['cycle_count']}")
    print(f"Total recalled: {status['total_recalled']}")
    print(f"Total stored: {status['total_stored']}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Aircraft Tracking Service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to YAML config file (default: config/config.yaml)",
    )
    parser.add_argument("--status", action="store_true", help="Show service status and exit")
    parser.add_argument("--once", action="store_true", help="Run one tracking cycle and exit")
    args = parser.parse_args()

    setup_logging(
        config_file=args.config, log_file="track_service.log", log_file_key="system.track_log_file"
    )

    if not os.path.exists(args.config):
        logger.error(f"Configuration file not found: {args.config}")
        print(f"Error: Configuration file not found: {args.config}")
        sys.exit(1)

    logger.info(f"Starting Track Service with config: {args.config}")

    try:
        asyncio.run(run_service(args))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        print("\nShutdown complete")
    except Exception as e:
        logger.error(f"Error in track service: {e}")
        print(f"Error: {e}")
        sys.exit(1)


async def run_service(args):
    """Run the track service based on command line arguments."""
    service = TrackService(args.config)

    if args.status:
        print_status(service)
        return

    if args.once:
        logger.info("Running one tracking cycle")
        await service.run_once()
        print_status(service)
        return

    # Run continuously
    logger.info("Running continuously - press Ctrl+C to stop")
    print("Track service running. Press Ctrl+C to stop.")
    await service.run_forever()


if __name__ == "__main__":
    main()
