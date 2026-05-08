#!/usr/bin/env python3
"""
Aircraft Report Generation Service Entry Point

Runs as an independent process that filters aircraft from database
and generates reports based on custom SQL filters.

Usage:
    # Run continuously
    python -m src.report_main --config config/config.yaml

    # Process one batch and exit
    python -m src.report_main --config config/config.yaml --once

    # Show status and exit
    python -m src.report_main --config config/config.yaml --status

    # Cleanup old cooldown records
    python -m src.report_main --config config/config.yaml --cleanup-cooldowns
"""

import argparse
import asyncio
import logging
import os
import sys

# Add project root to path for absolute imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.report_service import ReportService
from src.utils.logging_config import setup_logging

logger = logging.getLogger("report_main")


def print_status(service: ReportService):
    """Print service status to console."""
    status = service.get_service_status()

    print("\n=== Report Service Status ===")
    print(f"Running: {status['is_running']}")
    print(f"Poll interval: {status['poll_interval']}s")
    print(f"Batch size: {status['batch_size']}")
    print(f"Cooldown hours: {status['cooldown_hours']}")
    print(f"Min move distance: {status['min_move_distance_km']} km")
    print(f"Processed this session: {status['processed_this_session']}")
    print(f"Failed this session: {status['failed_this_session']}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Aircraft Report Generation Service",
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
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument(
        "--cleanup-cooldowns", action="store_true", help="Clean up old cooldown records and exit"
    )
    parser.add_argument(
        "--cleanup-hours",
        type=float,
        default=24.0,
        help="Hours to keep cooldown records (default: 24)",
    )
    args = parser.parse_args()

    setup_logging(
        config_file=args.config,
        log_file="report_service.log",
        log_file_key="system.report_log_file",
    )

    if not os.path.exists(args.config):
        logger.error(f"Configuration file not found: {args.config}")
        print(f"Error: Configuration file not found: {args.config}")
        sys.exit(1)

    logger.info(f"Starting Report Service with config: {args.config}")

    try:
        asyncio.run(run_service(args))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        print("\nShutdown complete")
    except Exception as e:
        logger.error(f"Error in report service: {e}")
        print(f"Error: {e}")
        sys.exit(1)


async def run_service(args):
    """Run the report service based on command line arguments."""
    service = ReportService(args.config)

    if args.status:
        print_status(service)
        return

    if args.cleanup_cooldowns:
        logger.info(f"Cleaning up old cooldowns (keeping {args.cleanup_hours} hours)")
        service.cleanup_cooldowns(args.cleanup_hours)
        print(f"Cooldown cleanup completed (kept last {args.cleanup_hours} hours)")
        return

    if args.once:
        logger.info("Processing one batch only")
        await service.run_once()
        print_status(service)
        return

    # Run continuously
    logger.info("Running continuously - press Ctrl+C to stop")
    print("Report service running. Press Ctrl+C to stop.")
    await service.run_forever()


if __name__ == "__main__":
    main()
