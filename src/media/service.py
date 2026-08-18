"""
Media service for generating maps and retrieving aircraft images.

This module provides media generation functionality.
It is responsible ONLY for generating/retrieving media files, not for email sending.

Separation of concerns:
- This module: Generates maps, finds aircraft images
- Content module: Builds HTML/text email content with image references
- Email module: Sends the prepared content with attachments
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path

from src.media.image_loader import load_image_bytes
from src.storage.base import ObjectStorage

logger = logging.getLogger("media.service")

# Where images fetched from object storage are materialised. The email content
# builder takes file paths, not bytes, by design -- it documents that it is
# "NOT responsible for downloading images" -- so keys have to become local
# files somewhere before they reach it.
_HOLDING_DIR_PREFIX = "flight-matrix-images-"


class MediaService:
    """Service for generating maps and retrieving aircraft images.

    This class is responsible for:
    - Generating flight track maps
    - Generating current position maps
    - Finding/retrieving aircraft images

    This class is NOT responsible for:
    - Building email content
    - Sending emails
    - Running AI analysis
    """

    def __init__(
        self,
        enable_maps: bool = True,
        enable_aircraft_images: bool = True,
        database_manager=None,
        recent_tracks_count: int = 10,
        storage: ObjectStorage | None = None,
        images_dir: str = "data/jetphotos_images",
    ):
        """Initialize the media service.

        Args:
            enable_maps: Whether to enable map generation
            enable_aircraft_images: Whether to enable aircraft image retrieval
            database_manager: Database manager for queries
            recent_tracks_count: Number of recent track points to include in summary
            storage: Object storage that database image keys are read from. On
                the aws and gcp targets this is the only place the files exist;
                ``None`` falls back to local files only.
            images_dir: Local directory scanned when the database has no rows,
                and searched by basename for legacy stored paths.
        """
        self.enable_maps = enable_maps
        self.enable_aircraft_images = enable_aircraft_images
        self.database_manager = database_manager
        self.recent_tracks_count = recent_tracks_count
        self.storage = storage
        self.images_dir = images_dir
        self._holding_dir: str | None = None

        # Initialize map generator
        self.map_generator = None
        if enable_maps:
            try:
                from src.media.map_generator import MapGenerator

                self.map_generator = MapGenerator(enable_maps=True)
                logger.info("Map generator initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize map generator: {e}")
                self.enable_maps = False

        # Initialize aircraft info enricher for track data
        self.enricher = None
        if database_manager:
            try:
                from src.aircraft.enricher import AircraftInfoEnricher

                self.enricher = AircraftInfoEnricher(
                    database_manager, recent_tracks_count=recent_tracks_count
                )
                logger.info("Aircraft info enricher initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize enricher: {e}")

        logger.info(
            f"Media service initialized (maps: {self.enable_maps}, "
            f"images: {self.enable_aircraft_images}, "
            f"storage: {type(storage).__name__ if storage else 'local files only'})"
        )

    def generate_maps(
        self, aircraft_data: dict, registration: str | None = None, icao: str | None = None
    ) -> list[str]:
        """Generate map images for aircraft.

        Args:
            aircraft_data: Aircraft data dictionary
            registration: Aircraft registration number
            icao: Aircraft ICAO code

        Returns:
            List of map image file paths (detail view, globe view)
        """
        if not self.enable_maps or not self.map_generator:
            return []

        map_paths = []
        aircraft_id = registration or icao or aircraft_data.get("hex", "unknown")

        try:
            # Try to get track data for flight track map (7 days to capture complete flight paths)
            tracks = None
            if self.enricher and registration:
                tracks = self.enricher.get_recent_tracks_for_map(registration, hours_back=168)

            if tracks and len(tracks) > 1:
                # Generate flight track maps
                detail_path = self.map_generator.generate_flight_track_map(
                    tracks,
                    aircraft_id=aircraft_id,
                    title=f"Flight Track (Detail): {aircraft_id}",
                    map_mode="detail",
                )
                if detail_path:
                    map_paths.append(detail_path)

                globe_path = self.map_generator.generate_flight_track_map(
                    tracks,
                    aircraft_id=aircraft_id,
                    title=f"Flight Track (Globe): {aircraft_id}",
                    map_mode="globe",
                )
                if globe_path:
                    map_paths.append(globe_path)
            else:
                # Generate current position maps
                detail_path = self.map_generator.generate_current_position_map(
                    aircraft_data, map_mode="detail"
                )
                if detail_path:
                    map_paths.append(detail_path)

                globe_path = self.map_generator.generate_current_position_map(
                    aircraft_data, map_mode="globe"
                )
                if globe_path:
                    map_paths.append(globe_path)

            logger.info(f"Generated {len(map_paths)} maps for {aircraft_id}")
            return map_paths

        except Exception as e:
            logger.error(f"Failed to generate maps: {e}")
            return []

    def get_aircraft_images(self, registration: str) -> list[str]:
        """Get local file paths for an aircraft's images.

        Checks the database first. Those rows hold object-storage keys, so each
        one is fetched and written to a local file; callers downstream (the
        email content builder) take paths, not bytes. Falls back to scanning the
        local images directory when the database has nothing.

        Args:
            registration: Aircraft registration number

        Returns:
            List of readable local file paths (up to 3)
        """
        if not self.enable_aircraft_images or not registration:
            return []

        # 1. Try database first
        keys = self._get_database_images(registration)
        if keys:
            image_paths = self._materialise(keys)
            if image_paths:
                logger.info(f"Found {len(image_paths)} database images for {registration}")
                return image_paths
            logger.warning(
                f"{len(keys)} database image rows for {registration} but none could be "
                f"fetched from storage or disk"
            )

        # 2. Fall back to filesystem
        image_paths = self._get_filesystem_images(registration)
        if image_paths:
            logger.info(f"Found {len(image_paths)} filesystem images for {registration}")

        return image_paths

    def _materialise(self, keys: list[str]) -> list[str]:
        """Fetch stored images and write them to local files.

        The holding directory is cleared on every call rather than on teardown:
        `ReportService` builds one `MediaService` per process and then loops
        forever, so per-instance cleanup would let temp files grow without
        bound. Each call holds at most three files.

        Args:
            keys: Values from ``aircraft_images.image_path`` — object keys,
                local paths, or full public URLs.

        Returns:
            Paths to the images that could be fetched, in the input order.
            Unfetchable entries are skipped, not represented.
        """
        holding_dir = self._reset_holding_dir()
        paths = []

        for index, key in enumerate(keys):
            data = load_image_bytes(key, self.storage, local_dirs=[self.images_dir])
            if data is None:
                continue

            suffix = Path(key).suffix or ".jpg"
            # Index-prefixed so two source keys with the same basename cannot
            # overwrite each other.
            local_path = Path(holding_dir) / f"{index:02d}_{Path(key).stem}{suffix}"
            try:
                local_path.write_bytes(data)
            except OSError as e:
                logger.error(f"Failed to write image to {local_path}: {e}")
                continue

            paths.append(str(local_path))

        return paths

    def _reset_holding_dir(self) -> str:
        """Return an empty directory for this call's materialised images."""
        if self._holding_dir and os.path.isdir(self._holding_dir):
            shutil.rmtree(self._holding_dir, ignore_errors=True)

        self._holding_dir = tempfile.mkdtemp(prefix=_HOLDING_DIR_PREFIX)
        return self._holding_dir

    def _get_database_images(self, registration: str) -> list[str]:
        """Query aircraft_images table for stored image paths.

        Uses aircraft_images as the single source of truth for images. The
        returned values are object-storage keys and are deliberately *not*
        filtered by `os.path.exists`: on the aws and gcp targets the files are
        in the bucket, never on the report host's disk, and filtering here made
        every emailed report silently photo-less.

        Args:
            registration: Aircraft registration number

        Returns:
            Up to three stored image paths ordered by display_order. Three is
            all the email template renders, and each one now costs a storage
            fetch, so the limit is applied in SQL rather than downstream.
        """
        if not self.database_manager:
            return []

        try:
            session = self.database_manager.get_session()
            from sqlalchemy import text

            # Query from aircraft_images table ordered by display_order
            result = session.execute(
                text("""
                SELECT image_path
                FROM aircraft_images
                WHERE registration = :reg
                AND image_path IS NOT NULL
                AND image_path != ''
                ORDER BY display_order ASC
                LIMIT 3
            """),
                {"reg": registration},
            ).fetchall()

            session.close()

            return [row[0] for row in result if row[0]] if result else []

        except Exception as e:
            logger.warning(f"Error querying aircraft_images for images: {e}")
            return []

    def _get_filesystem_images(self, registration: str) -> list[str]:
        """Find aircraft images in the filesystem.

        Args:
            registration: Aircraft registration number

        Returns:
            List of image file paths
        """
        images_dir = self.images_dir
        if not os.path.exists(images_dir):
            return []

        try:
            # Handle special characters in registration
            safe_reg = registration.replace("/", "-").replace("\\", "-").replace(":", "-")

            existing = list(Path(images_dir).glob(f"{safe_reg}_*.jpg")) + list(
                Path(images_dir).glob(f"{safe_reg}_*.png")
            )

            if existing:
                # Sort by modification time, take newest 3
                sorted_images = sorted(existing, key=lambda p: p.stat().st_mtime, reverse=True)
                return [str(p) for p in sorted_images[:3]]

            return []

        except Exception as e:
            logger.warning(f"Error finding filesystem images: {e}")
            return []

    def get_static_info(self, registration: str, hex_code: str = None) -> dict | None:
        """Get aircraft static information from cache.

        Args:
            registration: Aircraft registration number
            hex_code: ICAO hex code (optional)

        Returns:
            Static info dictionary or None
        """
        if not self.database_manager or not registration:
            return None

        try:
            from src.aircraft.cache import AircraftCacheManager

            manager = AircraftCacheManager(self.database_manager)
            result = manager.get_cached_info(registration, hex_code)

            if result.get("found") and result.get("data"):
                data = result["data"]
                # Normalize field names for consistency
                return {
                    "registration": data.get("registration"),
                    "hex_code": data.get("hex"),
                    "owner": data.get("owner"),
                    "operator": data.get("operator"),
                    "manufacturer": data.get("manufacturer"),
                    "model": data.get("aircraft_model"),
                    "aircraft_type": None,  # Not stored in cache
                    "serial_number": None,  # Not returned by get_cached_info
                    "year_built": None,  # Not returned by get_cached_info
                    "country_of_registration": data.get("country"),
                    "ai_analysis": data.get("summary"),
                    "data_source": result.get("metadata", {}).get("data_source"),
                }

            return None

        except Exception as e:
            logger.warning(f"Error fetching static info: {e}")
            return None

    def get_flight_endpoints(self, registration: str) -> dict | None:
        """Get flight departure and arrival information from track data.

        Analyzes the last flight's track points to identify starting and ending locations.

        Args:
            registration: Aircraft registration number

        Returns:
            Dictionary with departure and arrival info, or None
        """
        if not self.enricher or not registration:
            return None

        try:
            # Get the last flight's track data
            tracks = self.enricher.get_recent_tracks_for_map(
                registration, hours_back=168, last_flight_only=True
            )

            if not tracks or len(tracks) < 2:
                return None

            # Tracks are in reverse chronological order (newest first)
            # So the first track is arrival, last track is departure
            arrival_track = tracks[0]
            departure_track = tracks[-1]

            endpoints = {
                "departure": {
                    "lat": departure_track.get("lat"),
                    "lon": departure_track.get("lon"),
                    "time": departure_track.get("datetime"),
                    "country": departure_track.get("current_country"),
                    "location_name": None,  # Will be filled by reverse geocoding
                },
                "arrival": {
                    "lat": arrival_track.get("lat"),
                    "lon": arrival_track.get("lon"),
                    "time": arrival_track.get("datetime"),
                    "country": arrival_track.get("current_country"),
                    "location_name": None,
                },
                "track_count": len(tracks),
            }

            # Try to get location names via reverse geocoding
            endpoints["departure"]["location_name"] = self._reverse_geocode(
                departure_track.get("lat"), departure_track.get("lon")
            )
            endpoints["arrival"]["location_name"] = self._reverse_geocode(
                arrival_track.get("lat"), arrival_track.get("lon")
            )

            logger.info(
                f"Flight endpoints for {registration}: "
                f"{endpoints['departure'].get('location_name', 'Unknown')} -> "
                f"{endpoints['arrival'].get('location_name', 'Unknown')}"
            )

            return endpoints

        except Exception as e:
            logger.warning(f"Error getting flight endpoints: {e}")
            return None

    def _reverse_geocode(self, lat: float, lon: float) -> str | None:
        """Reverse geocode coordinates to location name.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Location name string or None
        """
        if lat is None or lon is None:
            return None

        try:
            from geopy.exc import GeocoderTimedOut
            from geopy.geocoders import Nominatim

            geolocator = Nominatim(user_agent="flight-matrix", timeout=5)
            location = geolocator.reverse(f"{lat}, {lon}", language="zh")

            if location:
                # Extract city or relevant location info
                address = location.raw.get("address", {})
                city = address.get("city") or address.get("town") or address.get("county")
                state = address.get("state")
                country = address.get("country")

                parts = [p for p in [city, state, country] if p]
                return ", ".join(parts[:2]) if parts else location.address[:50]

            return None

        except GeocoderTimedOut:
            logger.warning("Geocoder timed out")
            return None
        except Exception as e:
            logger.warning(f"Reverse geocoding error: {e}")
            return None
