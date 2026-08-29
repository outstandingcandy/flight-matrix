"""
Aircraft Service Module

Provides business logic for aircraft-related operations:
- Advanced aircraft search (by registration, flight number, type series)
- Aircraft details lookup and enrichment
- Live position tracking
- Historical track retrieval
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, true
from sqlalchemy.orm import Session

from src.data.models import AircraftSnapshot, AircraftStaticInfo
from src.web.time_helpers import naive_utc_now

logger = logging.getLogger(__name__)


# Widebody aircraft type codes
WIDEBODY_TYPES = {
    "A330",
    "A332",
    "A333",
    "A338",
    "A339",  # Airbus A330 family
    "A340",
    "A342",
    "A343",
    "A345",
    "A346",  # Airbus A340 family
    "A350",
    "A359",
    "A35K",  # Airbus A350 family
    "A380",
    "A388",  # Airbus A380
    "B747",
    "B741",
    "B742",
    "B743",
    "B744",
    "B748",
    "B74S",  # Boeing 747
    "B767",
    "B762",
    "B763",
    "B764",  # Boeing 767
    "B777",
    "B772",
    "B773",
    "B77L",
    "B77W",
    "B778",
    "B779",  # Boeing 777
    "B787",
    "B788",
    "B789",
    "B78X",  # Boeing 787
    "IL96",
    "IL86",  # Ilyushin
    "A310",
    "A300",  # Legacy Airbus widebodies
    "DC10",
    "MD11",  # McDonnell Douglas
}

# Cargo aircraft indicators
CARGO_INDICATORS = {"F", "BCF", "SF", "ERF", "BDSF"}


class AircraftService:
    """Service for aircraft-related operations."""

    def __init__(self, session: Session, config: dict | None = None):
        """Initialize aircraft service.

        Args:
            session: SQLAlchemy database session
            config: Optional configuration dictionary
        """
        self.session = session
        self.config = config or {}

        # Load configuration
        search_config = self.config.get("search_track", {})
        self.max_historical_days = search_config.get("max_historical_days", 30)

    def search_aircraft(
        self,
        registration: str | None = None,
        flight_number: str | None = None,
        type_series: str | None = None,
        operator: str | None = None,
        is_military: bool | None = None,
        is_widebody: bool | None = None,
        is_cargo: bool | None = None,
        hours_back: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Search aircraft with multiple criteria.

        Args:
            registration: Aircraft registration (partial match)
            flight_number: Flight number (partial match)
            type_series: Aircraft type series (e.g., 'A350' matches all A350 variants)
            operator: Operator name (partial match)
            is_military: Filter military aircraft
            is_widebody: Filter widebody aircraft
            is_cargo: Filter cargo aircraft
            hours_back: How far back to search (None = search all)
            limit: Maximum number of results

        Returns:
            List of aircraft dictionaries
        """
        try:
            # Build base conditions
            conditions = []

            # Only add time filter if hours_back is specified
            if hours_back is not None:
                cutoff_time = naive_utc_now() - timedelta(hours=hours_back)
                conditions.append(AircraftSnapshot.snapshot_time >= cutoff_time)

            # Registration filter
            if registration:
                conditions.append(AircraftSnapshot.registration.ilike(f"%{registration.upper()}%"))

            # Flight number filter
            if flight_number:
                conditions.append(
                    AircraftSnapshot.flight_number.ilike(f"%{flight_number.upper()}%")
                )

            # Aircraft type filter
            if type_series:
                type_upper = type_series.upper()
                conditions.append(AircraftSnapshot.aircraft_type.ilike(f"{type_upper}%"))

            # Military filter
            if is_military is not None:
                conditions.append(AircraftSnapshot.is_military == is_military)

            # Widebody filter
            if is_widebody is not None:
                if is_widebody:
                    # Match widebody types
                    widebody_conditions = [
                        AircraftSnapshot.aircraft_type.like(f"{wt}%") for wt in WIDEBODY_TYPES
                    ]
                    conditions.append(or_(*widebody_conditions))

            # Get unique aircraft with latest position
            subquery = (
                self.session.query(
                    AircraftSnapshot.hex, func.max(AircraftSnapshot.snapshot_time).label("max_time")
                )
                .filter(and_(true(), *conditions))
                .group_by(AircraftSnapshot.hex)
                .subquery()
            )

            # Join to get full data
            snapshots = (
                self.session.query(AircraftSnapshot)
                .join(
                    subquery,
                    and_(
                        AircraftSnapshot.hex == subquery.c.hex,
                        AircraftSnapshot.snapshot_time == subquery.c.max_time,
                    ),
                )
                .limit(limit)
                .all()
            )

            # Get all registrations for batch image lookup
            registrations = [s.registration for s in snapshots if s.registration]

            # Batch query for images from aircraft_static_info
            static_images = self._get_images_for_registrations(registrations)

            # Convert to dictionaries with classification
            results = []
            for snapshot in snapshots:
                aircraft_type = snapshot.aircraft_type or ""

                # Determine if widebody
                is_wb = any(aircraft_type.startswith(wt) for wt in WIDEBODY_TYPES)

                # Determine if cargo (check type suffix)
                is_cargo_aircraft = any(aircraft_type.endswith(ind) for ind in CARGO_INDICATORS)

                # Get images from aircraft_static_info (single source of truth)
                images = static_images.get(snapshot.registration, [])

                result = {
                    "hex": snapshot.hex,
                    "registration": snapshot.registration,
                    "flight_number": snapshot.flight_number,
                    "aircraft_type": aircraft_type,
                    "latitude": float(snapshot.latitude) if snapshot.latitude else None,
                    "longitude": float(snapshot.longitude) if snapshot.longitude else None,
                    "altitude_baro": snapshot.altitude_baro,
                    "ground_speed": float(snapshot.ground_speed) if snapshot.ground_speed else None,
                    "track": float(snapshot.track) if snapshot.track else None,
                    "vertical_rate": snapshot.vertical_rate,
                    "is_military": snapshot.is_military,
                    "is_widebody": is_wb,
                    "is_cargo": is_cargo_aircraft,
                    "country_of_registration": snapshot.country_of_registration,
                    "current_country": snapshot.current_country,
                    "snapshot_time": snapshot.snapshot_time.isoformat()
                    if snapshot.snapshot_time
                    else None,
                    "images": images,
                }
                results.append(result)

            # Filter by cargo if specified
            if is_cargo is not None:
                results = [r for r in results if r["is_cargo"] == is_cargo]

            # Filter by widebody if specified (post-filter for better accuracy)
            if is_widebody is not None:
                results = [r for r in results if r["is_widebody"] == is_widebody]

            return results

        except Exception as e:
            logger.error(f"Error searching aircraft: {e}")
            return []

    def get_aircraft_live_position(self, identifier: str) -> dict | None:
        """Get live position of an aircraft.

        Args:
            identifier: Aircraft registration or hex code

        Returns:
            Aircraft position data or None
        """
        try:
            identifier_upper = identifier.upper().strip()

            # Search by registration or hex
            snapshot = (
                self.session.query(AircraftSnapshot)
                .filter(
                    or_(
                        AircraftSnapshot.registration == identifier_upper,
                        AircraftSnapshot.hex == identifier_upper,
                    )
                )
                .order_by(AircraftSnapshot.snapshot_time.desc())
                .first()
            )

            if not snapshot:
                return None

            # Check if data is recent (within last hour)
            age = naive_utc_now() - snapshot.snapshot_time
            is_live = age.total_seconds() < 3600

            aircraft_type = snapshot.aircraft_type or ""
            is_wb = any(aircraft_type.startswith(wt) for wt in WIDEBODY_TYPES)

            return {
                "hex": snapshot.hex,
                "registration": snapshot.registration,
                "flight_number": snapshot.flight_number,
                "aircraft_type": aircraft_type,
                "latitude": float(snapshot.latitude) if snapshot.latitude else None,
                "longitude": float(snapshot.longitude) if snapshot.longitude else None,
                "altitude_baro": snapshot.altitude_baro,
                "altitude_geom": snapshot.altitude_geom,
                "ground_speed": float(snapshot.ground_speed) if snapshot.ground_speed else None,
                "track": float(snapshot.track) if snapshot.track else None,
                "vertical_rate": snapshot.vertical_rate,
                "squawk": snapshot.squawk,
                "is_military": snapshot.is_military,
                "is_widebody": is_wb,
                "country_of_registration": snapshot.country_of_registration,
                "current_country": snapshot.current_country,
                "snapshot_time": snapshot.snapshot_time.isoformat()
                if snapshot.snapshot_time
                else None,
                "is_live": is_live,
                "data_age_seconds": int(age.total_seconds()),
            }

        except Exception as e:
            logger.error(f"Error getting live position for {identifier}: {e}")
            return None

    def get_aircraft_details(self, identifier: str) -> dict | None:
        """Get detailed aircraft information.

        Combines data from AircraftStaticInfo and AircraftSnapshot tables.

        Args:
            identifier: Aircraft registration or hex code

        Returns:
            Aircraft details or None
        """
        try:
            identifier_upper = identifier.upper().strip()

            # Get static info
            static_info = (
                self.session.query(AircraftStaticInfo)
                .filter(
                    or_(
                        AircraftStaticInfo.registration == identifier_upper,
                        AircraftStaticInfo.hex_code == identifier_upper,
                    )
                )
                .first()
            )

            # Get latest snapshot for additional data
            snapshot = (
                self.session.query(AircraftSnapshot)
                .filter(
                    or_(
                        AircraftSnapshot.registration == identifier_upper,
                        AircraftSnapshot.hex == identifier_upper,
                    )
                )
                .order_by(AircraftSnapshot.snapshot_time.desc())
                .first()
            )

            if not static_info and not snapshot:
                return None

            # Combine data
            result = {
                "registration": None,
                "hex_code": None,
                "aircraft_type_code": None,
                "aircraft_type_full": None,
                "manufacturer": None,
                "model": None,
                "sub_model": None,
                "serial_number": None,
                "year_built": None,
                "current_age_years": None,
                "owner": None,
                "operator": None,
                "livery_name": None,
                "country_of_registration": None,
                "is_widebody": False,
                "is_cargo": False,
                "is_military": False,
                "is_special": False,
                "special_tags": None,
                "photo_url": None,
                "images": [],
                "data_source": None,
                "last_updated": None,
            }

            # Fill from static info (including images - single source of truth)
            if static_info:
                result.update(
                    {
                        "registration": static_info.registration,
                        "hex_code": static_info.hex_code,
                        "aircraft_type_code": static_info.aircraft_type,
                        "manufacturer": static_info.manufacturer,
                        "model": static_info.model,
                        "serial_number": static_info.serial_number,
                        "year_built": static_info.year_built,
                        "owner": static_info.owner,
                        "operator": static_info.operator,
                        "country_of_registration": static_info.country_of_registration,
                        "images": static_info.get_image_paths(),
                        "data_source": static_info.data_source,
                        "last_updated": static_info.last_updated.isoformat()
                        if static_info.last_updated
                        else None,
                    }
                )

            # Fill from snapshot if needed
            if snapshot:
                if not result["registration"]:
                    result["registration"] = snapshot.registration
                if not result["hex_code"]:
                    result["hex_code"] = snapshot.hex
                if not result["aircraft_type_code"]:
                    result["aircraft_type_code"] = snapshot.aircraft_type
                if not result["country_of_registration"]:
                    result["country_of_registration"] = snapshot.country_of_registration

                # Determine widebody from type
                aircraft_type = result["aircraft_type_code"] or ""
                if not result["is_widebody"]:
                    result["is_widebody"] = any(
                        aircraft_type.startswith(wt) for wt in WIDEBODY_TYPES
                    )

                result["is_military"] = result["is_military"] or snapshot.is_military

            return result

        except Exception as e:
            logger.error(f"Error getting aircraft details for {identifier}: {e}")
            return None

    def get_aircraft_history(
        self,
        identifier: str,
        date: datetime | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Get historical track data for an aircraft.

        Args:
            identifier: Aircraft registration or hex code
            date: Specific date to query (optional)
            start_time: Start of time range (optional)
            end_time: End of time range (optional)
            limit: Maximum number of track points

        Returns:
            List of track points
        """
        try:
            identifier_upper = identifier.upper().strip()

            # Build query
            query = self.session.query(AircraftSnapshot).filter(
                or_(
                    AircraftSnapshot.registration == identifier_upper,
                    AircraftSnapshot.hex == identifier_upper,
                )
            )

            # Apply time filters
            if date:
                # Query for specific date
                start_of_day = datetime.combine(date.date(), datetime.min.time())
                end_of_day = datetime.combine(date.date(), datetime.max.time())
                query = query.filter(
                    AircraftSnapshot.snapshot_time.between(start_of_day, end_of_day)
                )
            elif start_time or end_time:
                if start_time:
                    query = query.filter(AircraftSnapshot.snapshot_time >= start_time)
                if end_time:
                    query = query.filter(AircraftSnapshot.snapshot_time <= end_time)
            else:
                # Default: last 24 hours
                cutoff_time = naive_utc_now() - timedelta(hours=24)
                query = query.filter(AircraftSnapshot.snapshot_time >= cutoff_time)

            # Filter for valid coordinates and order by time
            query = (
                query.filter(
                    AircraftSnapshot.latitude.isnot(None), AircraftSnapshot.longitude.isnot(None)
                )
                .order_by(AircraftSnapshot.snapshot_time.asc())
                .limit(limit)
            )

            snapshots = query.all()

            # Convert to track points
            track_points = []
            for snapshot in snapshots:
                point = {
                    "timestamp": snapshot.snapshot_time.timestamp()
                    if snapshot.snapshot_time
                    else None,
                    "datetime": snapshot.snapshot_time.isoformat()
                    if snapshot.snapshot_time
                    else None,
                    "latitude": float(snapshot.latitude),
                    "longitude": float(snapshot.longitude),
                    "altitude_baro": snapshot.altitude_baro,
                    "altitude_geom": snapshot.altitude_geom,
                    "ground_speed": float(snapshot.ground_speed) if snapshot.ground_speed else None,
                    "track": float(snapshot.track) if snapshot.track else None,
                    "vertical_rate": snapshot.vertical_rate,
                    "flight_number": snapshot.flight_number,
                    "current_country": snapshot.current_country,
                }
                track_points.append(point)

            return track_points

        except Exception as e:
            logger.error(f"Error getting aircraft history for {identifier}: {e}")
            return []

    def get_aircraft_flight_dates(self, identifier: str, days_back: int = 30) -> list[dict]:
        """Get list of dates with flight activity for an aircraft.

        Useful for showing available dates for historical playback.

        Args:
            identifier: Aircraft registration or hex code
            days_back: How many days back to check

        Returns:
            List of dates with flight counts
        """
        try:
            identifier_upper = identifier.upper().strip()
            cutoff_time = naive_utc_now() - timedelta(days=days_back)

            # Get distinct dates with counts
            results = (
                self.session.query(
                    func.date(AircraftSnapshot.snapshot_time).label("flight_date"),
                    func.count().label("point_count"),
                )
                .filter(
                    or_(
                        AircraftSnapshot.registration == identifier_upper,
                        AircraftSnapshot.hex == identifier_upper,
                    ),
                    AircraftSnapshot.snapshot_time >= cutoff_time,
                )
                .group_by(func.date(AircraftSnapshot.snapshot_time))
                .order_by(func.date(AircraftSnapshot.snapshot_time).desc())
                .all()
            )

            return [{"date": str(r.flight_date), "point_count": r.point_count} for r in results]

        except Exception as e:
            logger.error(f"Error getting flight dates for {identifier}: {e}")
            return []

    def get_recent_unique_aircraft(
        self,
        hours_back: float = 24,
        is_military: bool | None = None,
        is_widebody: bool | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get recently seen unique aircraft.

        Args:
            hours_back: How far back to look
            is_military: Filter military aircraft
            is_widebody: Filter widebody aircraft
            limit: Maximum number of results

        Returns:
            List of unique aircraft
        """
        try:
            cutoff_time = naive_utc_now() - timedelta(hours=hours_back)

            # Build conditions
            conditions = [
                AircraftSnapshot.snapshot_time >= cutoff_time,
                AircraftSnapshot.registration.isnot(None),
                AircraftSnapshot.registration != "",
            ]

            if is_military is not None:
                conditions.append(AircraftSnapshot.is_military == is_military)

            # Get unique aircraft
            subquery = (
                self.session.query(
                    AircraftSnapshot.registration,
                    func.max(AircraftSnapshot.snapshot_time).label("max_time"),
                )
                .filter(and_(true(), *conditions))
                .group_by(AircraftSnapshot.registration)
                .subquery()
            )

            snapshots = (
                self.session.query(AircraftSnapshot)
                .join(
                    subquery,
                    and_(
                        AircraftSnapshot.registration == subquery.c.registration,
                        AircraftSnapshot.snapshot_time == subquery.c.max_time,
                    ),
                )
                .order_by(AircraftSnapshot.snapshot_time.desc())
                .limit(limit * 2)
                .all()
            )

            # Convert and filter
            results = []
            for snapshot in snapshots:
                aircraft_type = snapshot.aircraft_type or ""
                is_wb = any(aircraft_type.startswith(wt) for wt in WIDEBODY_TYPES)

                # Apply widebody filter
                if is_widebody is not None and is_wb != is_widebody:
                    continue

                result = {
                    "registration": snapshot.registration,
                    "hex": snapshot.hex,
                    "flight_number": snapshot.flight_number,
                    "aircraft_type": aircraft_type,
                    "is_military": snapshot.is_military,
                    "is_widebody": is_wb,
                    "country_of_registration": snapshot.country_of_registration,
                    "current_country": snapshot.current_country,
                    "last_seen": snapshot.snapshot_time.isoformat()
                    if snapshot.snapshot_time
                    else None,
                }
                results.append(result)

                if len(results) >= limit:
                    break

            return results

        except Exception as e:
            logger.error(f"Error getting recent unique aircraft: {e}")
            return []

    def _get_images_for_registrations(self, registrations: list[str]) -> dict[str, list[str]]:
        """Get images for multiple registrations from aircraft_images table.

        Batch query for efficiency. Uses the aircraft_images table as the
        single source of truth for image paths.

        Args:
            registrations: List of aircraft registration numbers

        Returns:
            Dictionary mapping registration to list of image paths
        """
        if not registrations:
            return {}

        try:
            from sqlalchemy import text

            # Remove None and empty strings
            valid_regs = [r for r in registrations if r]
            if not valid_regs:
                return {}

            # Build query with IN clause
            placeholders = ", ".join([f":reg{i}" for i in range(len(valid_regs))])
            params = {f"reg{i}": reg for i, reg in enumerate(valid_regs)}

            # Query aircraft_images table ordered by display_order
            result = self.session.execute(
                text(f"""
                SELECT registration, image_path
                FROM aircraft_images
                WHERE registration IN ({placeholders})
                AND image_path IS NOT NULL
                AND image_path != ''
                ORDER BY registration, display_order ASC
            """),
                params,
            ).fetchall()

            # Build result dictionary
            images_dict: dict[str, list[str]] = {}
            for row in result:
                reg = row[0]
                image_path = row[1]
                if reg not in images_dict:
                    images_dict[reg] = []
                images_dict[reg].append(image_path)

            return images_dict

        except Exception as e:
            logger.warning(f"Error getting images for registrations: {e}")
            return {}
