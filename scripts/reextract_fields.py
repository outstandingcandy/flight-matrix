#!/usr/bin/env python3
"""
Re-extract fields from saved HTML files in object storage.

The storage backend follows `DEPLOY_TARGET` (S3 on aws, GCS on gcp, the local
filesystem otherwise); `--bucket` overrides the configured bucket.

This script enables batch re-extraction of metadata fields from HTML files
that were previously saved during scraping. Useful when:
- Extraction logic has been updated
- New fields have been added to extractors
- Data needs to be re-processed after bug fixes

Usage:
    # List available extractors and their versions
    python scripts/reextract_fields.py --list-extractors

    # Dry run - preview what would be extracted without updating DB
    python scripts/reextract_fields.py --source jetphotos --limit 10 --dry-run

    # Re-extract and update database
    python scripts/reextract_fields.py --source jetphotos --limit 1000 --update-db

    # Re-extract from a specific prefix
    python scripts/reextract_fields.py --source airport_data --prefix data/airport_data_raw/aircraft/ --limit 100

    # Re-extract a single file
    python scripts/reextract_fields.py --source jetphotos --file data/jetphotos_images/html/12345678.html
"""

import argparse
import json
import logging
import os
import sys
from typing import Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from src.scraper.reextractor import ReExtractor
from src.storage import StorageFactory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("reextract_fields")


# Default bucket for HTML files; set S3_BUCKET_NAME (aws) or
# GCS_ASSETS_BUCKET (gcp) in env to override.
DEFAULT_BUCKET = os.environ.get("S3_BUCKET_NAME", "") or os.environ.get("GCS_ASSETS_BUCKET", "")

# Default prefixes for each source
DEFAULT_PREFIXES = {
    "jetphotos": "data/jetphotos_images/html/",
    "airport_data": "data/airport_data_raw/aircraft/",
}


def update_jetphotos_metadata(
    engine: Any,
    jetphotos_id: str,
    fields: dict[str, Any],
) -> bool:
    """Update JetPhotos metadata in database.

    Args:
        engine: SQLAlchemy engine.
        jetphotos_id: JetPhotos photo ID.
        fields: Extracted fields to update.

    Returns:
        True if update succeeded, False otherwise.
    """
    try:
        with engine.connect() as conn:
            # Update aircraft_images table with extracted metadata
            conn.execute(
                text("""
                    UPDATE aircraft_images
                    SET
                        jp_photographer = COALESCE(:photographer, jp_photographer),
                        jp_photo_date = COALESCE(:photo_date, jp_photo_date),
                        jp_upload_date = COALESCE(:upload_date, jp_upload_date),
                        jp_airport_icao = COALESCE(:airport_icao, jp_airport_icao),
                        jp_airport_name = COALESCE(:airport_name, jp_airport_name),
                        jp_camera = COALESCE(:camera, jp_camera),
                        jp_views = COALESCE(:views, jp_views),
                        jp_likes = COALESCE(:likes, jp_likes),
                        jp_badges = COALESCE(:badges, jp_badges),
                        jp_notes = COALESCE(:notes, jp_notes)
                    WHERE jetphotos_id = :jetphotos_id
                """),
                {
                    "jetphotos_id": jetphotos_id,
                    "photographer": fields.get("photographer"),
                    "photo_date": fields.get("photo_date"),
                    "upload_date": fields.get("upload_date"),
                    "airport_icao": fields.get("airport_icao"),
                    "airport_name": fields.get("airport_name"),
                    "camera": fields.get("camera"),
                    "views": fields.get("views"),
                    "likes": fields.get("likes"),
                    "badges": fields.get("badges"),
                    "notes": fields.get("notes"),
                },
            )
            conn.commit()
            return True
    except SQLAlchemyError as e:
        logger.error(f"Failed to update JetPhotos metadata for {jetphotos_id}: {e}")
        return False


def update_airport_data(
    engine: Any,
    registration: str,
    fields: dict[str, Any],
) -> bool:
    """Update airport-data fields in database.

    Args:
        engine: SQLAlchemy engine.
        registration: Aircraft registration.
        fields: Extracted fields to update.

    Returns:
        True if update succeeded, False otherwise.
    """
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    UPDATE aircraft_static_info
                    SET
                        manufacturer = COALESCE(:manufacturer, manufacturer),
                        model = COALESCE(:model, model),
                        serial_number = COALESCE(:serial_number, serial_number),
                        year_built = COALESCE(:year_built, year_built),
                        hex_code = COALESCE(:mode_s_code, hex_code),
                        owner = COALESCE(:owner, owner),
                        ad_status = COALESCE(:status, ad_status),
                        ad_owner = COALESCE(:owner, ad_owner),
                        ad_engines = COALESCE(:engines, ad_engines),
                        ad_seats = COALESCE(:seats, ad_seats),
                        ad_location = COALESCE(:location, ad_location),
                        ad_delivery_date = COALESCE(:delivery_date, ad_delivery_date),
                        ad_updated_at = CURRENT_TIMESTAMP,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE registration = :registration
                """),
                {
                    "registration": registration,
                    "manufacturer": fields.get("manufacturer"),
                    "model": fields.get("model"),
                    "serial_number": fields.get("serial_number"),
                    "year_built": fields.get("year_built"),
                    "mode_s_code": fields.get("mode_s_code"),
                    "owner": fields.get("owner"),
                    "status": fields.get("status"),
                    "engines": fields.get("engines"),
                    "seats": fields.get("seats"),
                    "location": fields.get("location"),
                    "delivery_date": fields.get("delivery_date"),
                },
            )
            conn.commit()
            return True
    except SQLAlchemyError as e:
        logger.error(f"Failed to update airport_data for {registration}: {e}")
        return False


def extract_context_from_path(source: str, html_path: str) -> dict[str, Any]:
    """Extract context information from HTML path.

    Args:
        source: Data source type.
        html_path: S3 path to HTML file.

    Returns:
        Context dictionary with extracted information.
    """
    context: dict[str, Any] = {}

    if source == "jetphotos":
        # Extract photo ID from filename: .../12345678.html
        filename = os.path.basename(html_path)
        if filename.endswith(".html"):
            photo_id = filename[:-5]  # Remove .html
            context["source_url"] = f"https://www.jetphotos.com/photo/{photo_id}"
    elif source == "airport_data":
        # Extract registration from filename
        # Format: {registration}_{date}.html (e.g., N12345_20260228.html)
        # Or: aircraft_{registration}_{date}.html
        filename = os.path.basename(html_path)
        if filename.endswith(".html"):
            name_part = filename[:-5]  # Remove .html
            parts = name_part.split("_")
            if len(parts) >= 2:
                # Check if first part is "aircraft" prefix
                if parts[0] == "aircraft":
                    context["registration"] = parts[1]
                else:
                    # First part is registration, rest is date
                    context["registration"] = parts[0]
            else:
                context["registration"] = name_part

    return context


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Re-extract fields from saved HTML files in S3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--source",
        choices=["jetphotos", "airport_data"],
        help="Data source type",
    )
    parser.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help=f"Object storage bucket name (default: {DEFAULT_BUCKET})",
    )
    parser.add_argument(
        "--prefix",
        help="Key prefix to search for HTML files (default: source-specific)",
    )
    parser.add_argument(
        "--file",
        help="Single HTML file to re-extract (S3 key)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of files to process (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview extraction results without updating database",
    )
    parser.add_argument(
        "--update-db",
        action="store_true",
        help="Update database with extracted fields",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Database URL (default: from DATABASE_URL env var)",
    )
    parser.add_argument(
        "--list-extractors",
        action="store_true",
        help="List available extractors and their versions",
    )
    parser.add_argument(
        "--output-json",
        help="Output results to JSON file",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Initialize reextractor on the storage backend for the active target
    reextractor = ReExtractor(storage=StorageFactory.create_from_dict({"bucket": args.bucket}))

    # List extractors mode
    if args.list_extractors:
        print("Available extractors:")
        for source, info in reextractor.get_version_info().items():
            print(f"  {source}: {info['extractor']} v{info['version']}")
        return 0

    # Validate source argument
    if not args.source and not args.list_extractors:
        parser.error("--source is required unless using --list-extractors")

    # Validate update-db requirements
    if args.update_db and not args.database_url:
        parser.error("--database-url is required when using --update-db")

    # Initialize database engine if needed
    db_engine = None
    if args.update_db:
        try:
            db_engine = create_engine(args.database_url, echo=False, pool_pre_ping=True)
            logger.info("Database connection initialized")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            return 1

    # Determine HTML files to process
    if args.file:
        html_paths = [args.file]
    else:
        prefix = args.prefix or DEFAULT_PREFIXES.get(args.source, "")
        logger.info(f"Listing HTML files from {args.bucket or 'local storage'}/{prefix}")
        html_paths = reextractor.list_html_files(prefix, max_files=args.limit)
        logger.info(f"Found {len(html_paths)} HTML files")

    if not html_paths:
        logger.warning("No HTML files found")
        return 0

    # Build contexts from file paths
    contexts = [extract_context_from_path(args.source, path) for path in html_paths]

    # Process files
    results = []
    success_count = 0
    error_count = 0
    update_count = 0

    for result in reextractor.batch_reextract(args.source, html_paths, contexts):
        html_path = result["html_path"]
        fields = result["fields"]
        errors = result["errors"]

        if result["success"]:
            success_count += 1

            # Extract context from path for database updates
            context = extract_context_from_path(args.source, html_path)

            if args.dry_run:
                logger.info(f"[DRY RUN] {html_path}: {json.dumps(fields, default=str)}")
            elif args.update_db and db_engine:
                # Update database
                if args.source == "jetphotos":
                    photo_id = (
                        fields.get("jetphotos_id") or context.get("source_url", "").split("/")[-1]
                    )
                    if photo_id:
                        if update_jetphotos_metadata(db_engine, photo_id, fields):
                            update_count += 1
                            logger.debug(f"Updated JetPhotos {photo_id}")
                elif args.source == "airport_data":
                    registration = fields.get("registration") or context.get("registration")
                    if registration:
                        if update_airport_data(db_engine, registration, fields):
                            update_count += 1
                            logger.debug(f"Updated airport_data {registration}")
            else:
                logger.info(f"{html_path}: {len(fields)} fields extracted")
        else:
            error_count += 1
            for error in errors:
                logger.warning(f"{html_path}: {error}")

        results.append(result)

    # Summary
    logger.info(
        f"Completed: {success_count} success, {error_count} errors"
        + (f", {update_count} DB updates" if args.update_db else "")
    )

    # Output JSON if requested
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Results written to {args.output_json}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
