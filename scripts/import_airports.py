#!/usr/bin/env python3
"""
Airport Data Import Script

Imports airport data from OurAirports open dataset into the local database.
Data source: https://ourairports.com/data/

Usage:
    python scripts/import_airports.py [--db-url DATABASE_URL] [--csv-path CSV_PATH]

Examples:
    # Download and import (default)
    python scripts/import_airports.py

    # Use local CSV file
    python scripts/import_airports.py --csv-path /path/to/airports.csv

    # Specify database URL
    python scripts/import_airports.py --db-url sqlite:///aircraft_data.db
"""

import argparse
import csv
import logging
import os
import sys
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional

import requests

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.data.models import Base, Airport

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# OurAirports data URL
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# Mapping of OurAirports type to our type
AIRPORT_TYPE_MAP = {
    "large_airport": "large_airport",
    "medium_airport": "medium_airport",
    "small_airport": "small_airport",
    "heliport": "heliport",
    "seaplane_base": "seaplane_base",
    "closed": "closed",
    "balloonport": "balloonport",
}


def download_airport_data() -> str:
    """Download airport data from OurAirports.

    Returns:
        CSV content as string
    """
    logger.info(f"Downloading airport data from {OURAIRPORTS_URL}")

    try:
        response = requests.get(OURAIRPORTS_URL, timeout=60)
        response.raise_for_status()
        logger.info(f"Downloaded {len(response.content)} bytes")
        return response.text
    except requests.RequestException as e:
        logger.error(f"Failed to download airport data: {e}")
        raise


def parse_airport_csv(csv_content: str) -> List[Dict]:
    """Parse OurAirports CSV content.

    Args:
        csv_content: CSV content as string

    Returns:
        List of airport dictionaries
    """
    airports = []
    reader = csv.DictReader(StringIO(csv_content))

    for row in reader:
        try:
            # Skip airports without ICAO code
            icao_code = (row.get("ident") or "").strip()
            if not icao_code or len(icao_code) != 4:
                continue

            # Skip closed airports
            airport_type = row.get("type", "").strip()
            if airport_type == "closed":
                continue

            # Parse coordinates
            try:
                latitude = float(row.get("latitude_deg", 0))
                longitude = float(row.get("longitude_deg", 0))
            except (ValueError, TypeError):
                continue

            # Skip invalid coordinates
            if latitude == 0 and longitude == 0:
                continue

            # Parse elevation
            try:
                elevation_ft = int(float(row.get("elevation_ft", 0) or 0))
            except (ValueError, TypeError):
                elevation_ft = None

            # Get IATA code (from iata_code field)
            iata_code = (row.get("iata_code") or "").strip()
            if not iata_code or len(iata_code) != 3:
                iata_code = None

            # Parse name and municipality
            name = (row.get("name") or "").strip()
            city = (row.get("municipality") or "").strip()
            country = (row.get("iso_country") or "").strip()

            # Skip airports without name
            if not name:
                continue

            airport = {
                "icao_code": icao_code.upper(),
                "iata_code": iata_code.upper() if iata_code else None,
                "name": name,
                "name_en": name,  # OurAirports uses English names
                "city": city or None,
                "country": get_country_name(country),
                "country_code": country.upper() if country else None,
                "latitude": latitude,
                "longitude": longitude,
                "elevation_ft": elevation_ft,
                "timezone": None,  # Not available in OurAirports CSV
                "airport_type": AIRPORT_TYPE_MAP.get(airport_type, airport_type),
            }

            airports.append(airport)

        except Exception as e:
            logger.warning(f"Error parsing row: {e}")
            continue

    logger.info(f"Parsed {len(airports)} airports from CSV")
    return airports


def get_country_name(iso_code: str) -> str:
    """Get country name from ISO code.

    Args:
        iso_code: 2-letter ISO country code

    Returns:
        Country name
    """
    # Common country mappings (add more as needed)
    COUNTRY_NAMES = {
        "CN": "China",
        "US": "United States",
        "JP": "Japan",
        "KR": "South Korea",
        "TW": "Taiwan",
        "HK": "Hong Kong",
        "SG": "Singapore",
        "TH": "Thailand",
        "MY": "Malaysia",
        "ID": "Indonesia",
        "PH": "Philippines",
        "VN": "Vietnam",
        "AU": "Australia",
        "NZ": "New Zealand",
        "GB": "United Kingdom",
        "DE": "Germany",
        "FR": "France",
        "IT": "Italy",
        "ES": "Spain",
        "NL": "Netherlands",
        "BE": "Belgium",
        "CH": "Switzerland",
        "AT": "Austria",
        "PT": "Portugal",
        "SE": "Sweden",
        "NO": "Norway",
        "DK": "Denmark",
        "FI": "Finland",
        "PL": "Poland",
        "CZ": "Czech Republic",
        "RU": "Russia",
        "UA": "Ukraine",
        "TR": "Turkey",
        "AE": "United Arab Emirates",
        "SA": "Saudi Arabia",
        "QA": "Qatar",
        "IL": "Israel",
        "IN": "India",
        "PK": "Pakistan",
        "BD": "Bangladesh",
        "LK": "Sri Lanka",
        "CA": "Canada",
        "MX": "Mexico",
        "BR": "Brazil",
        "AR": "Argentina",
        "CL": "Chile",
        "CO": "Colombia",
        "PE": "Peru",
        "ZA": "South Africa",
        "EG": "Egypt",
        "NG": "Nigeria",
        "KE": "Kenya",
        "ET": "Ethiopia",
        "MO": "Macau",
    }

    return COUNTRY_NAMES.get(iso_code.upper(), iso_code) if iso_code else None


def import_airports_to_db(airports: List[Dict], database_url: str):
    """Import airports into the database.

    Args:
        airports: List of airport dictionaries
        database_url: SQLAlchemy database URL
    """
    logger.info(f"Connecting to database: {database_url}")

    # Create engine and session
    engine = create_engine(database_url, echo=False)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Create tables if they don't exist
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()

    try:
        # Check existing airports count
        existing_count = session.query(Airport).count()
        logger.info(f"Existing airports in database: {existing_count}")

        # Import airports
        imported = 0
        updated = 0
        skipped = 0

        for airport_data in airports:
            try:
                # Check if airport already exists
                existing = (
                    session.query(Airport)
                    .filter(Airport.icao_code == airport_data["icao_code"])
                    .first()
                )

                if existing:
                    # Update existing record
                    for key, value in airport_data.items():
                        if value is not None:
                            setattr(existing, key, value)
                    updated += 1
                else:
                    # Create new record
                    airport = Airport(**airport_data)
                    session.add(airport)
                    imported += 1

                # Commit in batches
                if (imported + updated) % 1000 == 0:
                    session.commit()
                    logger.info(f"Progress: imported={imported}, updated={updated}")

            except Exception as e:
                logger.warning(f"Error importing airport {airport_data.get('icao_code')}: {e}")
                skipped += 1
                continue

        # Final commit
        session.commit()

        # Get final count
        final_count = session.query(Airport).count()

        logger.info(f"Import completed:")
        logger.info(f"  - New airports imported: {imported}")
        logger.info(f"  - Existing airports updated: {updated}")
        logger.info(f"  - Skipped due to errors: {skipped}")
        logger.info(f"  - Total airports in database: {final_count}")

    except Exception as e:
        session.rollback()
        logger.error(f"Error during import: {e}")
        raise
    finally:
        session.close()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Import airport data from OurAirports into the database"
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL (default: read from config or use sqlite:///aircraft_data.db)",
    )
    parser.add_argument(
        "--csv-path",
        default=None,
        help="Path to local CSV file (default: download from OurAirports)",
    )
    parser.add_argument(
        "--filter-type",
        choices=["large_airport", "medium_airport", "small_airport", "all"],
        default="all",
        help="Filter airports by type (default: all)",
    )

    args = parser.parse_args()

    # Determine database URL
    database_url = args.db_url
    if not database_url:
        # Try to read from config
        try:
            config_path = project_root / "config.yaml"
            if config_path.exists():
                import yaml

                with open(config_path, "r") as f:
                    config = yaml.safe_load(f)
                database_url = config.get("database", {}).get("url")
        except Exception:
            pass

        # Default to SQLite
        if not database_url:
            database_url = f"sqlite:///{project_root / 'aircraft_data.db'}"

    # Get CSV content
    if args.csv_path:
        logger.info(f"Reading airport data from {args.csv_path}")
        with open(args.csv_path, "r", encoding="utf-8") as f:
            csv_content = f.read()
    else:
        csv_content = download_airport_data()

    # Parse airports
    airports = parse_airport_csv(csv_content)

    # Filter by type if specified
    if args.filter_type != "all":
        airports = [a for a in airports if a["airport_type"] == args.filter_type]
        logger.info(f"Filtered to {len(airports)} {args.filter_type} airports")

    # Import to database
    import_airports_to_db(airports, database_url)


if __name__ == "__main__":
    main()
