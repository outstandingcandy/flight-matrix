"""
Aircraft information enricher for adding historical data and context.
"""

import logging
import os
import time

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("aircraft_info_enricher")


class AircraftInfoEnricher:
    """
    Enrich aircraft data by adding historical information and context.
    """

    def __init__(self, database_manager=None, recent_tracks_count: int = 10):
        """
        Initialize the aircraft information enricher.

        Args:
            database_manager: Database manager instance (optional)
            recent_tracks_count: Number of recent track points shown in the history summary (default 10)
        """
        self.database_manager = database_manager
        self.recent_tracks_count = recent_tracks_count

    def get_historical_tracks(
        self, registration: str, days_back: int = 7, limit: int = 200
    ) -> list[dict]:
        """
        Get historical track data for an aircraft.

        Args:
            registration: Aircraft registration
            days_back: How many past days to query (default 7)
            limit: Maximum records to return (default 200)

        Returns:
            List of historical track data
        """
        historical_tracks = []

        if not registration or not self.database_manager:
            return historical_tracks

        try:
            # Compute time range
            start_time = int(time.time()) - (days_back * 24 * 3600)

            # Fetch historical tracks from the database
            historical_tracks = self.database_manager.get_flight_tracks_by_registration(
                registration, limit=limit, start_time=start_time
            )

            logger.info(
                f"Found {len(historical_tracks)} historical tracks for registration {registration}"
            )

        except Exception as e:
            logger.warning(f"Could not retrieve historical tracks: {e}")

        return historical_tracks

    def get_historical_aircraft_data(self, aircraft_data: dict, days_back: int = 7) -> dict:
        """
        Get historical flight data for comprehensive analysis.
        Combines current aircraft data with historical flight tracks.

        Args:
            aircraft_data: Current aircraft data
            days_back: How many past days to query (default 7)

        Returns:
            Enriched aircraft data including historical info
        """
        try:
            # Start from current aircraft data
            historical_data = aircraft_data.copy()

            # Extract identifiers
            registration = (aircraft_data.get("r") or "").strip()
            logger.info(
                f"Getting historical data for registration: '{registration}', database_manager exists: {self.database_manager is not None}"
            )

            # Try to get historical flight tracks from the database
            historical_tracks = []

            if self.database_manager:
                # Use the database manager if available
                historical_tracks = self.get_historical_tracks(registration, days_back)
                logger.info(
                    f"Query returned {len(historical_tracks)} tracks for {registration} (days_back={days_back})"
                )
            else:
                logger.warning("database_manager is None, trying fallback database connection")
                # Try to create a temporary database connection
                try:
                    from utils.database import DatabaseManager

                    db_path = os.path.join("data", "aircraft_data.db")
                    if os.path.exists(db_path):
                        db = DatabaseManager("sqlite:///" + db_path)
                        historical_tracks = self.get_historical_tracks_from_db(
                            db, registration, days_back
                        )
                        db.close()
                except Exception as db_error:
                    logger.warning(f"Could not access flight history database: {db_error}")

            # Analyze historical data
            flight_history = self._analyze_historical_tracks(historical_tracks)
            historical_data["flight_history"] = flight_history

            # Add analysis timestamp context
            historical_data["analysis_timestamp"] = aircraft_data.get("timestamp", "Unknown")

            logger.info(
                f"Enhanced aircraft data with {len(historical_tracks)} historical track points"
            )
            return historical_data

        except Exception as e:
            logger.error(f"Error getting historical aircraft data: {e}")
            # Return original data if historical retrieval fails
            return aircraft_data

    def _analyze_historical_tracks(self, tracks: list[dict]) -> dict:
        """
        Analyze historical track data and generate summary information.

        Args:
            tracks: List of historical track points

        Returns:
            Historical data analysis result
        """
        if not tracks:
            return {
                "tracks_count": 0,
                "analysis_summary": "No historical data available",
                "has_historical_data": False,
                "historical_tracks": [],
            }

        # Basic statistics - numeric values only
        altitudes = [
            t["alt_baro"]
            for t in tracks
            if t.get("alt_baro") and isinstance(t["alt_baro"], (int, float))
        ]
        speeds = [
            t["ground_speed"]
            for t in tracks
            if t.get("ground_speed") and isinstance(t["ground_speed"], (int, float))
        ]
        countries = [t["current_country"] for t in tracks if t.get("current_country")]

        # Compute statistics
        avg_altitude = sum(altitudes) / len(altitudes) if altitudes else None
        max_altitude = max(altitudes) if altitudes else None
        avg_speed = sum(speeds) / len(speeds) if speeds else None
        max_speed = max(speeds) if speeds else None

        # Country visit stats
        country_visits = {}
        for country in countries:
            country_visits[country] = country_visits.get(country, 0) + 1

        # Sort by visit count
        sorted_countries = sorted(country_visits.items(), key=lambda x: x[1], reverse=True)
        visited_countries = [c[0] for c in sorted_countries]

        # Time range
        timestamps = [t["datetime"] for t in tracks if t.get("datetime")]
        time_range = ""
        if timestamps:
            time_range = f"{timestamps[-1]} to {timestamps[0]}"

        # Generate a detailed track summary (most recent N points, configurable)
        recent_tracks_summary = []
        for t in tracks[: self.recent_tracks_count]:
            summary = f"[{t.get('datetime', 'Unknown time')}] "
            summary += f"Location: {t.get('current_country', 'Unknown')} "
            summary += f"({t.get('lat', 0):.2f}, {t.get('lon', 0):.2f}) "
            if t.get("alt_baro"):
                summary += f"Alt: {t['alt_baro']}ft "
            if t.get("ground_speed"):
                summary += f"Speed: {t['ground_speed']:.0f}kts "
            if t.get("flight_number"):
                summary += f"Flight: {t['flight_number']}"
            recent_tracks_summary.append(summary)

        # Generate the analysis summary
        analysis_summary = f"Found {len(tracks)} track points over the past week.\n"
        if visited_countries:
            analysis_summary += f"Countries visited: {', '.join(visited_countries[:5])}\n"
        if time_range:
            analysis_summary += f"Time range: {time_range}\n"
        if avg_altitude:
            analysis_summary += f"Average altitude: {avg_altitude:.0f} ft, Max: {max_altitude} ft\n"
        if avg_speed:
            analysis_summary += f"Average speed: {avg_speed:.1f} kts, Max: {max_speed:.1f} kts"

        return {
            "tracks_count": len(tracks),
            "analysis_summary": analysis_summary,
            "has_historical_data": len(tracks) > 1,
            "average_altitude": avg_altitude,
            "maximum_altitude": max_altitude,
            "average_speed": avg_speed,
            "maximum_speed": max_speed,
            "visited_countries": visited_countries,
            "time_range": time_range,
            "recent_tracks_summary": recent_tracks_summary,
            "historical_tracks": tracks[:50],  # Limit track points passed to AI
        }

    def get_historical_tracks_from_db(
        self, db, registration: str, days_back: int = 7
    ) -> list[dict]:
        """
        Get historical tracks directly from the database.

        Args:
            db: Database manager instance
            registration: Aircraft registration
            days_back: How many past days to query

        Returns:
            List of historical track data
        """
        if not registration:
            return []

        try:
            start_time = int(time.time()) - (days_back * 24 * 3600)
            tracks = db.get_flight_tracks_by_registration(
                registration, limit=200, start_time=start_time
            )
            return tracks
        except Exception as e:
            logger.warning(f"Error getting tracks from database: {e}")
            return []

    def extract_aircraft_identifiers(self, aircraft_data: dict) -> dict:
        """
        Extract all identifiers from aircraft data.

        Args:
            aircraft_data: Aircraft data

        Returns:
            Dictionary containing all identifiers
        """
        return {
            "flight_number": (aircraft_data.get("flight") or "").strip(),
            "registration": (aircraft_data.get("r") or "").strip(),
            "aircraft_type": aircraft_data.get("t", "Unknown aircraft"),
            "squawk": aircraft_data.get("squawk", "Unknown"),
            "callsign": aircraft_data.get("call", ""),
            "icao": aircraft_data.get("hex", "").upper(),
        }

    def extract_position_data(self, aircraft_data: dict) -> dict:
        """
        Extract position information from aircraft data.

        Args:
            aircraft_data: Aircraft data

        Returns:
            Dictionary containing position information
        """
        return {
            "latitude": aircraft_data.get("lat", "Unknown"),
            "longitude": aircraft_data.get("lon", "Unknown"),
            "altitude_ft": aircraft_data.get("alt_baro", aircraft_data.get("alt_geom", "Unknown")),
            "speed_kts": aircraft_data.get("gs", "Unknown"),
            "heading": aircraft_data.get("track", "Unknown"),
            "vertical_rate": aircraft_data.get(
                "baro_rate", aircraft_data.get("geom_rate", "Unknown")
            ),
        }

    def create_tracking_links(self, aircraft_data: dict) -> dict:
        """
        Create aircraft tracking links.

        Args:
            aircraft_data: Aircraft data

        Returns:
            Dictionary containing tracking links
        """
        icao = aircraft_data.get("hex", "").upper()
        registration = (aircraft_data.get("r") or "").strip()

        links = {
            "adsb_exchange": f"https://globe.adsbexchange.com/?icao={icao}" if icao else None,
            "flightradar24": f"https://www.flightradar24.com/data/aircraft/{registration.lower()}"
            if registration
            else None,
        }

        return {k: v for k, v in links.items() if v}  # Return only valid links

    def get_recent_tracks_for_map(
        self,
        registration: str,
        hours_back: int = 168,
        limit: int = 1000,
        last_flight_only: bool = True,
    ) -> list[dict]:
        """
        Get recent track points for map display.

        Args:
            registration: Aircraft registration
            hours_back: How many past hours to query (default 168 = 7 days)
            limit: Maximum records to return (default 1000, enough for long-haul flights)
            last_flight_only: Whether to return only the last complete flight (default True)

        Returns:
            List of track data
        """
        if not registration or not self.database_manager:
            return []

        try:
            start_time = int(time.time()) - (hours_back * 3600)
            tracks = self.database_manager.get_flight_tracks_by_registration(
                registration, limit=limit, start_time=start_time
            )

            if last_flight_only and tracks:
                tracks = self._extract_last_flight(tracks)

            return tracks
        except Exception as e:
            logger.warning(f"Could not get tracks for map: {e}")
            return []

    def _extract_last_flight(
        self, tracks: list[dict], gap_threshold_minutes: int = 6 * 60
    ) -> list[dict]:
        """
        Extract the last complete flight from track data.

        Identifies flight boundaries by detecting time gaps. If the gap between
        two track points exceeds the threshold (default 6 hours), they are
        considered to belong to different flights.

        Args:
            tracks: Track data list (ordered by time descending)
            gap_threshold_minutes: Time threshold (minutes) to identify a flight gap

        Returns:
            Track data list for the last flight
        """
        if not tracks or len(tracks) <= 1:
            return tracks

        last_flight_tracks = [tracks[0]]
        prev_timestamp = tracks[0].get("timestamp")

        for track in tracks[1:]:
            curr_timestamp = track.get("timestamp")

            if prev_timestamp and curr_timestamp:
                # Compute time gap in minutes
                gap_minutes = abs(prev_timestamp - curr_timestamp) / 60

                if gap_minutes > gap_threshold_minutes:
                    # Flight boundary detected, stop collecting
                    logger.info(
                        f"Flight boundary detected: {gap_minutes:.0f}min gap, "
                        f"extracted {len(last_flight_tracks)} track points for last flight"
                    )
                    break

            last_flight_tracks.append(track)
            prev_timestamp = curr_timestamp

        return last_flight_tracks
