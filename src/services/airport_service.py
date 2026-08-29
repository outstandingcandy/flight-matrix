"""
Airport Service Module

Provides business logic for airport-related operations:
- Airport search and lookup
- Aircraft near airport queries using geospatial calculations
- Aircraft flight status determination (approaching, departing, cruising)
"""

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from src.data.models import AircraftSnapshot, Airport
from src.geo.geo import haversine_distance
from src.web.time_helpers import naive_utc_now

logger = logging.getLogger(__name__)


# Configuration constants (can be overridden via config)
DEFAULT_RADIUS_KM = 100
MAX_RADIUS_KM = 500
APPROACH_ALTITUDE_FT = 10000
APPROACH_VERTICAL_RATE = -500  # fpm, negative = descending
DEPARTURE_VERTICAL_RATE = 500  # fpm, positive = climbing


class AirportService:
    """Service for airport-related operations."""

    def __init__(self, session: Session, config: dict | None = None):
        """Initialize airport service.

        Args:
            session: SQLAlchemy database session
            config: Optional configuration dictionary
        """
        self.session = session
        self.config = config or {}

        # Load configuration
        airport_config = self.config.get("airport_board", {})
        self.default_radius_km = airport_config.get("default_radius_km", DEFAULT_RADIUS_KM)
        self.max_radius_km = airport_config.get("max_radius_km", MAX_RADIUS_KM)

        flight_config = self.config.get("flight_status", {})
        self.approach_altitude_ft = flight_config.get("approach_altitude_ft", APPROACH_ALTITUDE_FT)
        self.approach_vertical_rate = flight_config.get(
            "approach_vertical_rate", APPROACH_VERTICAL_RATE
        )
        self.departure_vertical_rate = flight_config.get(
            "departure_vertical_rate", DEPARTURE_VERTICAL_RATE
        )

    def search_airports(
        self, query: str, limit: int = 20, airport_types: list[str] | None = None
    ) -> list[dict]:
        """Search airports by name, city, IATA code, or ICAO code.

        Args:
            query: Search query string
            limit: Maximum number of results
            airport_types: Optional list of airport types to filter

        Returns:
            List of airport dictionaries
        """
        if not query or len(query) < 2:
            return []

        query_upper = query.upper().strip()

        try:
            # Build search conditions with priority levels
            # High priority: exact IATA/ICAO code matches
            exact_conditions = [
                Airport.iata_code == query_upper,  # Exact IATA match
                Airport.icao_code == query_upper,  # Exact ICAO match
            ]

            # Medium priority: code prefix matches
            prefix_conditions = [
                Airport.iata_code.ilike(f"{query_upper}%"),  # IATA prefix
                Airport.icao_code.ilike(f"{query_upper}%"),  # ICAO prefix
            ]

            # Low priority: name/city matches (only for larger airports, word-start match)
            # Match at start of name/city or after a space (word boundary)
            name_conditions = [
                and_(
                    or_(
                        Airport.name.ilike(f"{query}%"),  # Name starts with query
                        Airport.name.ilike(f"% {query}%"),  # Word in name starts with query
                        Airport.city.ilike(f"{query}%"),  # City starts with query
                    ),
                    Airport.airport_type.in_(["large_airport", "medium_airport"]),
                )
            ]

            # Combine all conditions
            all_conditions = exact_conditions + prefix_conditions + name_conditions

            # Base query
            db_query = self.session.query(Airport).filter(or_(*all_conditions))

            # Filter by airport type if specified
            if airport_types:
                db_query = db_query.filter(Airport.airport_type.in_(airport_types))

            # Order by relevance using CASE expression
            # Priority: 1=exact IATA, 2=exact ICAO, 3=IATA prefix, 4=ICAO prefix, 5=name/city match
            relevance_order = case(
                (Airport.iata_code == query_upper, 1),
                (Airport.icao_code == query_upper, 2),
                (Airport.iata_code.ilike(f"{query_upper}%"), 3),
                (Airport.icao_code.ilike(f"{query_upper}%"), 4),
                else_=5,
            )

            # Airport size priority
            size_order = case(
                (Airport.airport_type == "large_airport", 1),
                (Airport.airport_type == "medium_airport", 2),
                else_=3,
            )

            # Order by relevance, then by airport size
            airports = (
                db_query.order_by(relevance_order, size_order, Airport.name).limit(limit).all()
            )

            logger.debug(f"Search '{query}' returned {len(airports)} results")

            return [airport.to_dict() for airport in airports]

        except Exception as e:
            logger.error(f"Error searching airports: {e}")
            return []

    def get_airport_by_code(self, code: str) -> dict | None:
        """Get airport by ICAO or IATA code.

        Args:
            code: Airport code (ICAO or IATA)

        Returns:
            Airport dictionary or None if not found
        """
        if not code:
            return None

        code_upper = code.upper().strip()

        try:
            # Try ICAO code first (4 characters)
            if len(code_upper) == 4:
                airport = (
                    self.session.query(Airport).filter(Airport.icao_code == code_upper).first()
                )
                if airport:
                    return airport.to_dict()

            # Try IATA code (3 characters)
            if len(code_upper) == 3:
                airport = (
                    self.session.query(Airport).filter(Airport.iata_code == code_upper).first()
                )
                if airport:
                    return airport.to_dict()

            # Try both
            airport = (
                self.session.query(Airport)
                .filter(or_(Airport.icao_code == code_upper, Airport.iata_code == code_upper))
                .first()
            )

            return airport.to_dict() if airport else None

        except Exception as e:
            logger.error(f"Error getting airport {code}: {e}")
            return None

    def get_aircraft_near_airport(
        self,
        airport_code: str,
        radius_km: float | None = None,
        hours_back: float = 0.5,
        limit: int = 200,
    ) -> dict:
        """Get aircraft currently near an airport.

        Uses Haversine formula to calculate distance from airport.
        Returns aircraft with their distance and estimated flight status.

        Args:
            airport_code: Airport ICAO or IATA code
            radius_km: Search radius in kilometers (default: configured default)
            hours_back: How far back to look for recent snapshots
            limit: Maximum number of aircraft to return

        Returns:
            Dictionary with airport info and nearby aircraft list
        """
        # Get airport
        airport = self.get_airport_by_code(airport_code)
        if not airport:
            return {"error": f"Airport not found: {airport_code}", "aircraft": []}

        airport_lat = airport["latitude"]
        airport_lon = airport["longitude"]

        # Determine radius
        if radius_km is None:
            radius_km = self.default_radius_km
        radius_km = min(radius_km, self.max_radius_km)

        try:
            # Calculate bounding box for initial filter (optimization)
            # 1 degree latitude ≈ 111 km
            lat_delta = radius_km / 111.0
            # 1 degree longitude varies by latitude
            lon_delta = radius_km / (111.0 * math.cos(math.radians(airport_lat)))

            min_lat = airport_lat - lat_delta
            max_lat = airport_lat + lat_delta
            min_lon = airport_lon - lon_delta
            max_lon = airport_lon + lon_delta

            # Query recent snapshots within bounding box
            cutoff_time = naive_utc_now() - timedelta(hours=hours_back)

            # Use subquery to get latest snapshot for each aircraft
            subquery = (
                self.session.query(
                    AircraftSnapshot.hex, func.max(AircraftSnapshot.snapshot_time).label("max_time")
                )
                .filter(
                    AircraftSnapshot.snapshot_time >= cutoff_time,
                    AircraftSnapshot.latitude.between(min_lat, max_lat),
                    AircraftSnapshot.longitude.between(min_lon, max_lon),
                    AircraftSnapshot.latitude.isnot(None),
                    AircraftSnapshot.longitude.isnot(None),
                )
                .group_by(AircraftSnapshot.hex)
                .subquery()
            )

            # Join to get full aircraft data
            snapshots = (
                self.session.query(AircraftSnapshot)
                .join(
                    subquery,
                    and_(
                        AircraftSnapshot.hex == subquery.c.hex,
                        AircraftSnapshot.snapshot_time == subquery.c.max_time,
                    ),
                )
                .all()
            )

            # Calculate actual distance and filter by radius
            aircraft_list = []
            for snapshot in snapshots:
                if snapshot.latitude is None or snapshot.longitude is None:
                    continue

                lat = float(snapshot.latitude)
                lon = float(snapshot.longitude)
                distance_km = haversine_distance(airport_lat, airport_lon, lat, lon)

                if distance_km <= radius_km:
                    # Determine flight status
                    status = self._determine_flight_status(
                        altitude=snapshot.altitude_baro,
                        vertical_rate=snapshot.vertical_rate,
                        distance_km=distance_km,
                    )

                    aircraft_data = {
                        "hex": snapshot.hex,
                        "registration": snapshot.registration,
                        "flight_number": snapshot.flight_number,
                        "aircraft_type": snapshot.aircraft_type,
                        "latitude": lat,
                        "longitude": lon,
                        "altitude_baro": snapshot.altitude_baro,
                        "ground_speed": float(snapshot.ground_speed)
                        if snapshot.ground_speed
                        else None,
                        "track": float(snapshot.track) if snapshot.track else None,
                        "vertical_rate": snapshot.vertical_rate,
                        "distance_km": round(distance_km, 2),
                        "flight_status": status,
                        "is_military": snapshot.is_military,
                        "country_of_registration": snapshot.country_of_registration,
                        "snapshot_time": snapshot.snapshot_time.isoformat()
                        if snapshot.snapshot_time
                        else None,
                    }
                    aircraft_list.append(aircraft_data)

            # Sort by distance
            aircraft_list.sort(key=lambda x: x["distance_km"])

            # Limit results
            aircraft_list = aircraft_list[:limit]

            # Categorize aircraft
            approaching = [a for a in aircraft_list if a["flight_status"] == "approaching"]
            departing = [a for a in aircraft_list if a["flight_status"] == "departing"]
            cruising = [a for a in aircraft_list if a["flight_status"] == "cruising"]
            ground = [a for a in aircraft_list if a["flight_status"] == "ground"]

            return {
                "airport": airport,
                "radius_km": radius_km,
                "query_time": naive_utc_now().isoformat(),
                "total_count": len(aircraft_list),
                "approaching_count": len(approaching),
                "departing_count": len(departing),
                "cruising_count": len(cruising),
                "ground_count": len(ground),
                "aircraft": aircraft_list,
                "approaching": approaching,
                "departing": departing,
                "cruising": cruising,
                "ground": ground,
            }

        except Exception as e:
            logger.error(f"Error getting aircraft near airport {airport_code}: {e}")
            return {"error": str(e), "aircraft": []}

    def _determine_flight_status(
        self, altitude: int | None, vertical_rate: int | None, distance_km: float
    ) -> str:
        """Determine aircraft flight status relative to airport.

        Args:
            altitude: Aircraft altitude in feet (barometric)
            vertical_rate: Vertical rate in feet per minute
            distance_km: Distance from airport in kilometers

        Returns:
            Flight status: 'approaching', 'departing', 'cruising', 'ground'
        """
        if altitude is None:
            return "unknown"

        # On ground (very low altitude)
        if altitude < 500:
            return "ground"

        # Close to airport and descending = approaching
        if distance_km < 50:  # Within 50km
            if altitude < self.approach_altitude_ft:
                if vertical_rate is not None and vertical_rate < self.approach_vertical_rate:
                    return "approaching"
                elif vertical_rate is not None and vertical_rate > self.departure_vertical_rate:
                    return "departing"
                else:
                    # Low altitude, not clearly climbing or descending
                    return "approaching" if altitude < 5000 else "cruising"

        # Low altitude and climbing = departing
        if altitude < self.approach_altitude_ft and vertical_rate is not None:
            if vertical_rate > self.departure_vertical_rate:
                return "departing"
            elif vertical_rate < self.approach_vertical_rate:
                return "approaching"

        return "cruising"

    def get_popular_airports(self, country_code: str | None = None, limit: int = 20) -> list[dict]:
        """Get popular (large) airports.

        Args:
            country_code: Optional country code to filter
            limit: Maximum number of results

        Returns:
            List of airport dictionaries
        """
        try:
            query = self.session.query(Airport).filter(Airport.airport_type == "large_airport")

            if country_code:
                query = query.filter(Airport.country_code == country_code.upper())

            airports = query.order_by(Airport.name).limit(limit).all()

            return [airport.to_dict() for airport in airports]

        except Exception as e:
            logger.error(f"Error getting popular airports: {e}")
            return []

    def get_airports_in_region(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        airport_types: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get airports within a geographic bounding box.

        Args:
            min_lat: Minimum latitude
            max_lat: Maximum latitude
            min_lon: Minimum longitude
            max_lon: Maximum longitude
            airport_types: Optional list of airport types to filter
            limit: Maximum number of results

        Returns:
            List of airport dictionaries
        """
        try:
            query = self.session.query(Airport).filter(
                Airport.latitude.between(min_lat, max_lat),
                Airport.longitude.between(min_lon, max_lon),
            )

            if airport_types:
                query = query.filter(Airport.airport_type.in_(airport_types))

            airports = query.limit(limit).all()

            return [airport.to_dict() for airport in airports]

        except Exception as e:
            logger.error(f"Error getting airports in region: {e}")
            return []
