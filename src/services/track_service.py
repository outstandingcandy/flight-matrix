"""
Aircraft Tracking Service

Handles aircraft data recall from API and storage to database.
This service runs independently and only focuses on data collection.

Data flow:
    API (ADS-B Exchange) → Database (aircraft_snapshots table)
"""

import asyncio
import logging
from datetime import datetime

import requests

from src.utils.database import DatabaseManager
from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("track_service")


class TrackService:
    """Aircraft Tracking Service - recalls data from API and stores to database."""

    def __init__(self, config_file: str = "config.yaml"):
        """Initialize the tracking service.

        Args:
            config_file: Path to YAML configuration file
        """
        self.config_file = config_file
        self.yaml_config = YAMLConfig(config_file)
        self.db = self._init_database()
        self._api_config, self._api_headers = self._init_api_config()

        # Runtime state
        self.is_running = False
        self._cycle_count = 0
        self._total_recalled = 0
        self._total_stored = 0
        self._start_time = None

        logger.info("Track Service initialized")

    def _init_database(self) -> DatabaseManager:
        """Initialize database connection."""
        db_config = self.yaml_config.get_database_config()
        return DatabaseManager(db_config["url"])

    def _init_api_config(self) -> tuple:
        """Initialize API configuration and headers."""
        api_config = self.yaml_config.get_api_config()
        recall_config = self.yaml_config.get_recall_config()
        strategy = recall_config.get("strategy", {})

        config = {
            "api_key": api_config["adsb_api_key"],
            "api_url": api_config.get(
                "adsb_api_url", "https://adsbexchange-com1.p.rapidapi.com/v2"
            ),
            "timeout": api_config.get("timeout", 30),
            "update_interval": strategy.get("update_interval", 300),
        }

        headers = {
            "X-RapidAPI-Key": config["api_key"],
            "X-RapidAPI-Host": "adsbexchange-com1.p.rapidapi.com",
        }

        return config, headers

    # -------------------------------------------------------------------------
    # API Methods
    # -------------------------------------------------------------------------

    def _make_api_request(self, endpoint: str, params: dict | None = None) -> dict | None:
        """Make API request to ADS-B Exchange."""
        try:
            url = f"{self._api_config['api_url']}/{endpoint}"
            response = requests.get(
                url, headers=self._api_headers, params=params, timeout=self._api_config["timeout"]
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"API request failed: {e}")
            return None

    def get_flight_by_registration(self, registration: str) -> dict | None:
        """Get aircraft by registration number."""
        endpoint = f"registration/{registration}/"
        data = self._make_api_request(endpoint, None)
        if data and data.get("ac"):
            return data["ac"][0] if len(data["ac"]) > 0 else None
        return None

    def get_military_flights(self) -> list[dict]:
        """Get all military aircraft."""
        data = self._make_api_request("mil")
        if data and data.get("ac"):
            return data["ac"]
        return []

    def get_aircraft_by_location(self, lat: float, lon: float, dist: int = 25) -> list[dict]:
        """Get aircraft within a radius of specified coordinates."""
        lat = max(-90, min(90, lat))
        lon = max(-180, min(180, lon))
        dist = max(1, min(2000, dist))

        endpoint = f"lat/{lat}/lon/{lon}/dist/{dist}/"
        data = self._make_api_request(endpoint)
        if data and data.get("ac"):
            return data["ac"]
        return []

    # -------------------------------------------------------------------------
    # Tracking Cycle
    # -------------------------------------------------------------------------

    async def run_tracking_cycle(self):
        """Execute a complete tracking cycle: recall → store."""
        try:
            cycle_start = datetime.now()
            logger.info(f"Starting tracking cycle at {cycle_start}")

            # Phase 1: Recall aircraft data
            recalled_aircraft = await self._recall_aircraft_data()
            recalled_count = len(recalled_aircraft)
            logger.info(f"Recalled {recalled_count} aircraft")
            self._total_recalled += recalled_count

            # Phase 2: Batch store to database
            stored_count = 0
            if recalled_aircraft:
                stored_count = self.db.batch_insert_aircraft(recalled_aircraft)
                logger.info(f"Stored {stored_count} aircraft snapshots to database")
                self._total_stored += stored_count

            self._cycle_count += 1
            cycle_duration = (datetime.now() - cycle_start).total_seconds()
            logger.info(f"Tracking cycle #{self._cycle_count} completed in {cycle_duration:.1f}s")

        except Exception as e:
            logger.error(f"Error in tracking cycle: {e}")

    # -------------------------------------------------------------------------
    # Recall Phase
    # -------------------------------------------------------------------------

    async def _recall_aircraft_data(self) -> list[dict]:
        """Recall phase - fetch aircraft data from various sources."""
        recall_config = self.yaml_config.get_recall_config()
        sources = recall_config.get("sources", {})

        try:
            all_aircraft = []
            all_aircraft.extend(self._recall_specific_registrations(sources))
            all_aircraft.extend(self._recall_military_aircraft(sources))
            all_aircraft.extend(self._recall_aircraft_by_location(sources))

            unique_aircraft = self._deduplicate_aircraft(all_aircraft)
            logger.info(
                f"Recalled {len(unique_aircraft)} unique aircraft from {len(all_aircraft)} total"
            )
            return unique_aircraft

        except Exception as e:
            logger.error(f"Error in recall phase: {e}")
            return []

    def _recall_specific_registrations(self, sources: dict) -> list[dict]:
        """Recall aircraft by specific registration numbers."""
        aircraft = []
        registrations = sources.get("specific_registrations", [])
        for registration in registrations:
            ac = self.get_flight_by_registration(registration)
            if ac:
                aircraft.append(ac)
                logger.debug(f"Recalled aircraft by registration: {registration}")
        return aircraft

    def _recall_military_aircraft(self, sources: dict) -> list[dict]:
        """Recall global military aircraft."""
        if not sources.get("military_global"):
            return []
        military = self.get_military_flights()
        logger.debug(f"Recalled {len(military)} military aircraft globally")
        return military

    def _recall_aircraft_by_location(self, sources: dict) -> list[dict]:
        """Recall aircraft around specified coordinates."""
        location_tracking = sources.get("location_tracking", [])
        if not location_tracking:
            return []

        all_aircraft = []
        for location in location_tracking:
            lat = location.get("lat")
            lon = location.get("lon")
            radius_nm = location.get("radius_nm", 25)
            name = location.get("name", f"({lat}, {lon})")

            if lat is None or lon is None:
                logger.warning(f"Invalid location config (missing lat/lon): {location}")
                continue

            aircraft = self.get_aircraft_by_location(lat, lon, radius_nm)
            logger.debug(f"Recalled {len(aircraft)} aircraft around {name} (radius: {radius_nm}nm)")
            all_aircraft.extend(aircraft)

        return all_aircraft

    def _deduplicate_aircraft(self, aircraft_list: list[dict]) -> list[dict]:
        """Remove duplicate aircraft based on hex code."""
        seen_hex = set()
        unique = []
        for aircraft in aircraft_list:
            hex_code = aircraft.get("hex", "")
            if hex_code and hex_code not in seen_hex:
                seen_hex.add(hex_code)
                unique.append(aircraft)
        return unique

    # -------------------------------------------------------------------------
    # Lifecycle Methods
    # -------------------------------------------------------------------------

    async def run_forever(self):
        """Run tracking service continuously."""
        self.is_running = True
        self._start_time = datetime.now()
        update_interval = self._api_config.get("update_interval", 300)

        logger.info(f"Starting continuous tracking with {update_interval}s interval")

        while self.is_running:
            try:
                await self.run_tracking_cycle()
                await asyncio.sleep(update_interval)

            except KeyboardInterrupt:
                logger.info("Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"Error in main tracking loop: {e}")
                await asyncio.sleep(60)

        logger.info("Track service stopped")

    async def run_once(self):
        """Run a single tracking cycle."""
        self._start_time = datetime.now()
        await self.run_tracking_cycle()

    def stop(self):
        """Stop tracking service."""
        self.is_running = False
        logger.info("Stopping track service...")

    def get_service_status(self) -> dict:
        """Get service status and statistics."""
        uptime_seconds = 0
        if self._start_time:
            uptime_seconds = (datetime.now() - self._start_time).total_seconds()

        return {
            "is_running": self.is_running,
            "uptime_seconds": uptime_seconds,
            "cycle_count": self._cycle_count,
            "total_recalled": self._total_recalled,
            "total_stored": self._total_stored,
            "update_interval": self._api_config.get("update_interval", 300),
        }
