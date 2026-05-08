"""
FlightAnalysisAgent - Aviation intelligence analysis agent based on the AWS Bedrock API.

A professional aviation intelligence analyst that calls the Claude model directly via
the AWS Bedrock API. It accepts real-time aircraft flight data, combines it with
internet search, and analyzes and reports on an aircraft's affiliation, likely
destination, and mission purpose.

Based on AWS Bedrock API with Tavily search integration.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:
    import boto3
except ImportError:
    logging.error("boto3 package not found. Please install with: pip install boto3")
    raise ImportError("boto3 package is required")

from src.media.markdown_converter import convert_markdown_to_html
from src.utils.yaml_config import YAMLConfig

from .tavily_search import TavilySearchClient

logger = logging.getLogger("flight_analysis_agent_production")

# Global configuration instance
_config = None


def get_config() -> YAMLConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = YAMLConfig()
    return _config


# Global variables for storing the search client and cache manager
_search_client = None
_cache_manager = None


def get_search_client():
    """Get the search client instance."""
    global _search_client
    if _search_client is None:
        try:
            _search_client = TavilySearchClient()
            logger.info("Initialized TavilySearchClient")
        except Exception as e:
            logger.error(f"Failed to initialize TavilySearchClient: {e}")
            _search_client = None
    return _search_client


def get_cache_manager():
    """Get the cache manager instance."""
    global _cache_manager
    if _cache_manager is None:
        try:
            from src.aircraft.cache import AircraftCacheManager
            from src.utils.database import DatabaseManager

            db = DatabaseManager()
            _cache_manager = AircraftCacheManager(db)
            logger.info("Initialized AircraftCacheManager")
        except Exception as e:
            logger.error(f"Failed to initialize AircraftCacheManager: {e}")
    return _cache_manager


# ============================================================================
# Tool functions: fully autonomous agent decision-making
# ============================================================================


def get_cached_aircraft_info(registration: str = None, hex_code: str = None) -> str:
    """
    Query the database for cached aircraft static information.

    Returns cached data and freshness metadata; the agent autonomously decides
    whether a refresh is needed based on this information.

    Args:
        registration: Aircraft registration number
        hex_code: ICAO hex code

    Returns:
        Cached information and metadata in JSON format
    """
    logger.info(
        f"--- [Tool call: get_cached_aircraft_info] registration={registration}, hex={hex_code} ---"
    )

    if not registration and not hex_code:
        return json.dumps(
            {
                "found": False,
                "message": "Error: At least one of registration or hex_code is required",
                "data": None,
                "metadata": None,
            },
            ensure_ascii=False,
        )

    try:
        manager = get_cache_manager()
        if not manager:
            return json.dumps(
                {
                    "found": False,
                    "message": "Cache manager not available",
                    "data": None,
                    "metadata": None,
                },
                ensure_ascii=False,
            )

        result = manager.get_cached_info(registration=registration, hex_code=hex_code)
        return json.dumps(result, ensure_ascii=False, default=str)

    except Exception as e:
        logger.error(f"Error getting cached info: {e}")
        return json.dumps(
            {"found": False, "message": f"Error: {e!s}", "data": None, "metadata": None},
            ensure_ascii=False,
        )


def search_web(query: str, search_type: str = "basic") -> str:
    """
    Real-time web search for aviation-related information.

    The agent is free to construct queries to search for any aviation-related
    information. Search results are NOT automatically cached; call
    save_aircraft_info to persist them to the database.

    Args:
        query: Search query string
        search_type: Search depth - "basic" (1 credit) or "advanced" (2 credits, more detailed)

    Returns:
        Search results string
    """
    logger.info(f"--- [Tool: search_web] query='{query}', type={search_type} ---")

    try:
        search_client = get_search_client()
        if not search_client:
            return "Search service not available"

        # Select search depth based on type
        if search_type == "advanced":
            response = search_client.search(
                query=query, search_depth="advanced", include_raw_content=True, max_results=8
            )
        else:
            response = search_client.search(query, max_results=5)

        return search_client.format_results(response, query)

    except Exception as e:
        logger.error(f"Search failed: {e}")
        return f"Search failed: {e!s}"


def save_aircraft_info(
    registration: str,
    hex_code: str = None,
    owner: str = None,
    operator: str = None,
    aircraft_model: str = None,
    manufacturer: str = None,
    country: str = None,
    is_military: bool = False,
    is_government: bool = False,
    is_vip: bool = False,
    summary: str = None,
    tags: list = None,
) -> str:
    """
    Save aircraft static information to the database cache.

    After the agent discovers valuable information via search, it can call this
    tool to save the data to the cache so that subsequent analyses of the same
    aircraft can retrieve it directly from the cache.

    Args:
        registration: Aircraft registration number (required)
        hex_code: ICAO hex code
        owner: Owner
        operator: Operator
        aircraft_model: Aircraft model
        manufacturer: Manufacturer
        country: Country of registration
        is_military: Whether this is a military aircraft
        is_government: Whether this is a government aircraft
        is_vip: Whether this is a VIP aircraft
        summary: Background summary
        tags: List of tags

    Returns:
        Operation result JSON
    """
    logger.info(f"--- [Tool call: save_aircraft_info] registration={registration} ---")

    if not registration:
        return json.dumps(
            {"success": False, "message": "Error: registration is required"}, ensure_ascii=False
        )

    try:
        manager = get_cache_manager()
        if not manager:
            return json.dumps(
                {"success": False, "message": "Cache manager not available"}, ensure_ascii=False
            )

        result = manager.save_info(
            registration=registration,
            hex_code=hex_code,
            owner=owner,
            operator=operator,
            aircraft_model=aircraft_model,
            manufacturer=manufacturer,
            country=country,
            is_military=is_military,
            is_government=is_government,
            is_vip=is_vip,
            summary=summary,
            tags=tags,
            data_source="agent",
        )
        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Error saving aircraft info: {e}")
        return json.dumps({"success": False, "message": f"Error: {e!s}"}, ensure_ascii=False)


# Kept for backward compatibility with possible external callers (deprecated; use search_web instead)
def search_recent_news(owner: str = None, operator: str = None, location: str = None) -> str:
    """
    Search for recent news (owner/operator/destination-related latest news).

    Deprecated: retained only for backward compatibility. The agent now uses
    search_web directly.

    Args:
        owner: Owner name
        operator: Operator name
        location: Location/destination

    Returns:
        Formatted news search results
    """
    logger.info(
        f"--- [Compat tool call: search_recent_news] owner={owner}, operator={operator}, location={location} ---"
    )

    if not owner and not operator and not location:
        return "Error: At least one of owner, operator, or location is required"

    try:
        # Build search queries
        queries = []
        if owner:
            queries.append(f'"{owner}" news 2024 2025')
        if operator and operator != owner:
            queries.append(f'"{operator}" news 2024 2025')
        if location:
            queries.append(f'"{location}" events news 2024 2025')

        # Search via search_web
        results = []
        for q in queries[:2]:
            results.append(search_web(q, search_type="basic"))
        return "\n\n".join(results) if results else "No search queries constructed"

    except Exception as e:
        logger.error(f"Error searching recent news: {e}")
        return f"Error searching news: {e!s}"


@dataclass
class AnalysisProgress:
    """Analysis progress information."""

    step: int = 0
    total_steps: int = 10
    current_action: str = ""
    details: str = ""
    tool_calls: list[dict[str, Any]] = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "step": self.step,
            "total_steps": self.total_steps,
            "current_action": self.current_action,
            "details": self.details,
            "tool_calls": self.tool_calls,
            "progress_percent": int((self.step / self.total_steps) * 100)
            if self.total_steps > 0
            else 0,
        }


@dataclass
class TokenUsage:
    """Token usage statistics."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    api_calls: int = 0

    def add(self, input_tokens: int, output_tokens: int, api_calls: int = 1):
        """Accumulate token usage."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens = self.input_tokens + self.output_tokens
        self.api_calls += api_calls

    def to_dict(self) -> dict[str, int]:
        """Convert to dictionary."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "api_calls": self.api_calls,
        }

    def __str__(self) -> str:
        return f"TokenUsage(input={self.input_tokens}, output={self.output_tokens}, total={self.total_tokens}, calls={self.api_calls})"


@dataclass
class FlightHistoryData:
    """Historical flight data structure."""

    tracks_count: int = 0
    analysis_summary: str = ""
    has_historical_data: bool = False
    average_altitude: float | None = None
    maximum_altitude: float | None = None
    average_speed: float | None = None
    maximum_speed: float | None = None
    geographical_coverage: str | None = None
    operating_range: str | None = None
    activity_level: str | None = None
    visited_countries: list[str] = None
    time_range: str | None = None
    recent_tracks_summary: list[str] = None
    historical_tracks: list[dict] = None


@dataclass
class FlightData:
    """Standardized aircraft flight data structure (enhanced, supports historical data)."""

    flight_number: str | None = None
    registration: str | None = None
    country_of_registration: str | None = None
    current_position: str | None = None
    aircraft_type: str | None = None
    icao: str | None = None
    squawk: str | None = None
    callsign: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: int | None = None
    speed: float | None = None
    heading: float | None = None
    vertical_rate: float | None = None
    # Added support for historical data
    analysis_timestamp: str | None = None
    flight_history: FlightHistoryData | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlightData":
        """Create a FlightData object from a dictionary (supports historical data)."""
        flight_data = cls(
            flight_number=(data.get("flight") or "").strip() or None,
            registration=data.get("r") or None,
            country_of_registration=None,  # Will be inferred later
            current_position=None,  # Will be inferred later
            aircraft_type=data.get("t") or None,
            icao=data.get("hex") or None,
            squawk=data.get("squawk") or None,
            callsign=(data.get("flight") or "").strip() or None,
            latitude=data.get("lat"),
            longitude=data.get("lon"),
            altitude=data.get("alt_baro"),
            speed=data.get("gs"),
            heading=data.get("track"),
            vertical_rate=data.get("vert_rate"),
            analysis_timestamp=data.get("analysis_timestamp"),
        )

        # Process historical flight data
        if "flight_history" in data:
            history_data = data["flight_history"]
            if isinstance(history_data, dict):
                flight_data.flight_history = FlightHistoryData(
                    tracks_count=history_data.get("tracks_count", 0),
                    analysis_summary=history_data.get("analysis_summary", ""),
                    has_historical_data=history_data.get("has_historical_data", False),
                    average_altitude=history_data.get("average_altitude"),
                    maximum_altitude=history_data.get("maximum_altitude"),
                    average_speed=history_data.get("average_speed"),
                    maximum_speed=history_data.get("maximum_speed"),
                    geographical_coverage=history_data.get("geographical_coverage"),
                    operating_range=history_data.get("operating_range"),
                    activity_level=history_data.get("activity_level"),
                    visited_countries=history_data.get("visited_countries", []),
                    time_range=history_data.get("time_range"),
                    recent_tracks_summary=history_data.get("recent_tracks_summary", []),
                    historical_tracks=history_data.get("historical_tracks", []),
                )
            elif isinstance(history_data, list):
                # If a historical track list was passed in directly
                flight_data.flight_history = FlightHistoryData(
                    tracks_count=len(history_data),
                    has_historical_data=len(history_data) > 1,
                    historical_tracks=history_data,
                    analysis_summary=f"Found {len(history_data)} historical track points",
                )

        # Infer country of registration - priority: ICAO > registration > aircraft type
        # The ICAO address is the most accurate because it is assigned by the country.
        country_determined = False

        # First priority: determine by ICAO code (most accurate)
        if flight_data.icao:
            country_determined = FlightData._determine_country_by_icao(
                flight_data.icao, flight_data
            )

        # Second priority: determine by registration (if ICAO did not resolve it)
        if not country_determined and flight_data.registration:
            reg = (flight_data.registration or "").upper().strip()

            # Mainland China
            if reg.startswith("B-"):
                # Need to distinguish Mainland, Hong Kong, Macau, and Taiwan
                if reg.startswith("B-H") or reg.startswith("B-K") or reg.startswith("B-L"):
                    flight_data.country_of_registration = "Hong Kong"
                elif reg.startswith("B-M"):
                    flight_data.country_of_registration = "Macau"
                else:
                    flight_data.country_of_registration = "China"
                country_determined = True

            # United States
            elif reg.startswith("N"):
                flight_data.country_of_registration = "United States"
                country_determined = True

            # US military (specific format: XX-XXXX)
            elif (
                len(reg.split("-")) == 2
                and reg.split("-")[0].isdigit()
                and len(reg.split("-")[0]) == 2
            ):
                flight_data.country_of_registration = "United States (Military)"
                country_determined = True

            # Other known registration prefixes
            elif reg.startswith("G-"):
                flight_data.country_of_registration = "United Kingdom"
                country_determined = True
            elif reg.startswith("D-"):
                flight_data.country_of_registration = "Germany"
                country_determined = True
            elif reg.startswith("C-"):
                flight_data.country_of_registration = "Canada"
                country_determined = True
            elif reg.startswith("F-"):
                flight_data.country_of_registration = "France"
                country_determined = True
            elif reg.startswith("RA-") or reg.startswith("RA"):
                flight_data.country_of_registration = "Russia"
                country_determined = True
            elif reg.startswith("JA"):
                flight_data.country_of_registration = "Japan"
                country_determined = True
            elif reg.startswith("CB-"):
                flight_data.country_of_registration = "Chile (Military)"
                country_determined = True
            elif reg.startswith("VH-"):
                flight_data.country_of_registration = "Australia"
                country_determined = True
            elif (
                reg.startswith("PP-")
                or reg.startswith("PR-")
                or reg.startswith("PT-")
                or reg.startswith("PU-")
            ):
                flight_data.country_of_registration = "Brazil"
                country_determined = True
            elif reg.startswith("FAC"):
                flight_data.country_of_registration = "Colombia (Military)"
                country_determined = True
            elif reg.startswith("HK-") or reg.startswith("HJ-"):
                flight_data.country_of_registration = "Colombia"
                country_determined = True
            elif reg.startswith("9V-"):
                flight_data.country_of_registration = "Singapore"
                country_determined = True
            elif reg.startswith("HL"):
                flight_data.country_of_registration = "South Korea"
                country_determined = True
            elif reg.startswith("9M-"):
                flight_data.country_of_registration = "Malaysia"
                country_determined = True
            elif reg.startswith("PH-"):
                flight_data.country_of_registration = "Netherlands"
                country_determined = True
            elif reg.startswith("I-"):
                flight_data.country_of_registration = "Italy"
                country_determined = True
            elif reg.startswith("EC-"):
                flight_data.country_of_registration = "Spain"
                country_determined = True
            elif reg.startswith("HB-"):
                flight_data.country_of_registration = "Switzerland"
                country_determined = True
            elif reg.startswith("SE-"):
                flight_data.country_of_registration = "Sweden"
                country_determined = True
            elif reg.startswith("LN-"):
                flight_data.country_of_registration = "Norway"
                country_determined = True
            elif reg.startswith("OY-"):
                flight_data.country_of_registration = "Denmark"
                country_determined = True
            elif reg.startswith("OH-"):
                flight_data.country_of_registration = "Finland"
                country_determined = True
            elif reg.startswith("VT-"):
                flight_data.country_of_registration = "India"
                country_determined = True
            elif reg.startswith("HS-"):
                flight_data.country_of_registration = "Thailand"
                country_determined = True
            elif reg.startswith("PK-"):
                flight_data.country_of_registration = "Indonesia"
                country_determined = True
            elif reg.startswith("RP-"):
                flight_data.country_of_registration = "Philippines"
                country_determined = True
            elif reg.startswith("VN-"):
                flight_data.country_of_registration = "Vietnam"
                country_determined = True

        # Third priority: determine by aircraft type (when both ICAO and registration fail)
        if not country_determined and flight_data.aircraft_type:
            aircraft_type = (flight_data.aircraft_type or "").upper()
            country_determined = FlightData._determine_country_by_aircraft_type(
                aircraft_type, flight_data
            )

        # If still not determined, mark as Unknown
        if not country_determined:
            flight_data.country_of_registration = "Unknown"

        # Infer current position (simplified)
        if flight_data.latitude and flight_data.longitude:
            if 40 <= flight_data.latitude <= 42 and -96 <= flight_data.longitude <= -95:
                flight_data.current_position = "Omaha, United States (near Offutt Air Force Base)"
            elif 38 <= flight_data.latitude <= 39 and -78 <= flight_data.longitude <= -76:
                flight_data.current_position = "Washington D.C. area, United States"
            else:
                flight_data.current_position = (
                    f"Lat: {flight_data.latitude:.3f}, Lon: {flight_data.longitude:.3f}"
                )

        return flight_data

    @staticmethod
    def _determine_country_by_icao(icao, flight_data):
        """Determine country based on ICAO code."""
        if not icao or len(icao) < 6:
            return False

        # The first two characters of the ICAO code represent the country/region
        country_code = icao[:2]

        # Russia ICAO range (100000-17FFFF)
        if country_code in ["10", "11", "12", "13", "14", "15", "16", "17"]:
            flight_data.country_of_registration = "Russia"
            return True

        # France ICAO range (380000-3BFFFF)
        elif country_code in ["38", "39", "3A", "3B"]:
            flight_data.country_of_registration = "France"
            return True

        # Germany ICAO range (3C0000-3FFFFF)
        elif country_code in ["3C", "3D", "3E", "3F"]:
            flight_data.country_of_registration = "Germany"
            return True

        # United Kingdom ICAO range (400000-43FFFF)
        elif country_code in ["40", "41", "42", "43"]:
            flight_data.country_of_registration = "United Kingdom"
            return True

        # Italy ICAO range (300000-33FFFF)
        elif country_code in ["30", "31", "32", "33"]:
            flight_data.country_of_registration = "Italy"
            return True

        # Spain ICAO range (340000-37FFFF)
        elif country_code in ["34", "35", "36", "37"]:
            flight_data.country_of_registration = "Spain"
            return True

        # China ICAO range (780000-7BFFFF)
        elif country_code in ["78", "79", "7A", "7B"]:
            flight_data.country_of_registration = "China"
            return True

        # Australia ICAO range (7C0000-7FFFFF)
        elif country_code in ["7C", "7D", "7E", "7F"]:
            flight_data.country_of_registration = "Australia"
            return True

        # India ICAO range (800000-83FFFF)
        elif country_code in ["80", "81", "82", "83"]:
            flight_data.country_of_registration = "India"
            return True

        # Japan ICAO range (840000-87FFFF)
        elif country_code in ["84", "85", "86", "87"]:
            flight_data.country_of_registration = "Japan"
            return True

        # South Korea ICAO range (710000-717FFF)
        elif country_code == "71":
            flight_data.country_of_registration = "South Korea"
            return True

        # Denmark ICAO range (458000-45FFFF)
        elif country_code == "45":
            flight_data.country_of_registration = "Denmark"
            return True

        # Netherlands ICAO range (480000-4BFFFF)
        elif country_code in ["48", "49", "4A", "4B"]:
            flight_data.country_of_registration = "Netherlands"
            return True

        # Sweden ICAO range (4AC000-4AFFFF)
        elif country_code == "4A":
            flight_data.country_of_registration = "Sweden"
            return True

        # Norway ICAO range (478000-47FFFF)
        elif country_code == "47":
            flight_data.country_of_registration = "Norway"
            return True

        # Finland ICAO range (460000-467FFF)
        elif country_code == "46":
            flight_data.country_of_registration = "Finland"
            return True

        # United States ICAO range (A00000-AFFFFF)
        elif country_code in [
            "A0",
            "A1",
            "A2",
            "A3",
            "A4",
            "A5",
            "A6",
            "A7",
            "A8",
            "A9",
            "AA",
            "AB",
            "AC",
            "AD",
            "AE",
            "AF",
        ]:
            flight_data.country_of_registration = "United States"
            return True

        # Canada ICAO range (C00000-C3FFFF)
        elif country_code in ["C0", "C1", "C2", "C3"]:
            flight_data.country_of_registration = "Canada"
            return True

        # Brazil ICAO range (E40000-E7FFFF)
        elif country_code in ["E4", "E5", "E6", "E7"]:
            flight_data.country_of_registration = "Brazil"
            return True

        # Argentina ICAO range (E00000-E3FFFF)
        elif country_code in ["E0", "E1", "E2", "E3"]:
            flight_data.country_of_registration = "Argentina"
            return True

        return False

    @staticmethod
    def _determine_country_by_aircraft_type(aircraft_type, flight_data):
        """Determine country based on aircraft type."""
        if not aircraft_type:
            return False

        # Get the military aircraft type list from the configuration file
        config = get_config()
        aircraft_types_config = config.get_aircraft_types_config()

        chinese_military_types = aircraft_types_config.get("chinese_military", [])
        us_military_types = aircraft_types_config.get("us_military", [])
        russian_military_types = aircraft_types_config.get("russian_military", [])
        european_military_types = aircraft_types_config.get("european_military", [])

        if aircraft_type in chinese_military_types:
            flight_data.country_of_registration = "China (Military)"
            return True
        elif aircraft_type in us_military_types:
            flight_data.country_of_registration = "United States (Military)"
            return True
        elif aircraft_type in russian_military_types:
            flight_data.country_of_registration = "Russia (Military)"
            return True
        elif aircraft_type in european_military_types:
            flight_data.country_of_registration = "Europe (Military)"
            return True

        return False

    def to_analysis_string(self) -> str:
        """Convert to a formatted string for analysis (includes historical data)."""
        lines = []

        # Basic aircraft information
        if self.flight_number:
            lines.append(f"Flight Number: {self.flight_number}")
        if self.registration:
            lines.append(f"Registration: {self.registration}")
        if self.country_of_registration:
            lines.append(f"Country of Registration: {self.country_of_registration}")
        if self.current_position:
            lines.append(f"Current Position: {self.current_position}")
        if self.aircraft_type:
            lines.append(f"Aircraft Type: {self.aircraft_type}")
        if self.icao:
            lines.append(f"ICAO: {self.icao}")
        if self.squawk:
            lines.append(f"Squawk: {self.squawk}")
        if self.callsign:
            lines.append(f"Callsign: {self.callsign}")

        # Current flight status
        lines.append("")
        lines.append("=== CURRENT FLIGHT STATUS ===")
        if self.latitude is not None:
            lines.append(f"Latitude: {self.latitude}")
        if self.longitude is not None:
            lines.append(f"Longitude: {self.longitude}")
        if self.altitude is not None:
            lines.append(f"Altitude: {self.altitude} ft")
        if self.speed is not None:
            lines.append(f"Speed: {self.speed} knots")
        if self.heading is not None:
            lines.append(f"Heading: {self.heading}°")
        if self.vertical_rate is not None:
            lines.append(f"Vertical Rate: {self.vertical_rate} ft/min")
        if self.analysis_timestamp:
            lines.append(f"Data Timestamp: {self.analysis_timestamp}")

        # Historical flight data (if available)
        if self.flight_history and self.flight_history.has_historical_data:
            lines.append("")
            lines.append("=== HISTORICAL FLIGHT DATA (Past 7 Days) ===")
            lines.append(f"Total Track Points: {self.flight_history.tracks_count}")

            if self.flight_history.time_range:
                lines.append(f"Time Range: {self.flight_history.time_range}")

            if self.flight_history.visited_countries:
                lines.append(
                    f"Countries Visited: {', '.join(self.flight_history.visited_countries)}"
                )

            if self.flight_history.average_altitude:
                lines.append(f"Average Altitude: {self.flight_history.average_altitude:.0f} ft")
            if self.flight_history.maximum_altitude:
                lines.append(f"Maximum Altitude: {self.flight_history.maximum_altitude:.0f} ft")
            if self.flight_history.average_speed:
                lines.append(f"Average Speed: {self.flight_history.average_speed:.1f} knots")
            if self.flight_history.maximum_speed:
                lines.append(f"Maximum Speed: {self.flight_history.maximum_speed:.1f} knots")

            # Recent track summary
            if self.flight_history.recent_tracks_summary:
                lines.append("")
                lines.append("=== RECENT FLIGHT TRACK DETAILS ===")
                for track_summary in self.flight_history.recent_tracks_summary:
                    lines.append(track_summary)

            if self.flight_history.analysis_summary:
                lines.append("")
                lines.append("Flight Pattern Summary:")
                lines.append((self.flight_history.analysis_summary or "").strip())
        elif self.flight_history and not self.flight_history.has_historical_data:
            lines.append("")
            lines.append("=== HISTORICAL FLIGHT DATA ===")
            lines.append("No significant historical flight data available for this aircraft.")
            lines.append(f"Historical records found: {self.flight_history.tracks_count}")

        return "\n".join(lines)


# The system prompt is read from the configuration file (llm.system_prompt).
# Default system prompt (used when none is set in the configuration file).
DEFAULT_SYSTEM_PROMPT = """You are a professional aviation intelligence analyst. Your task is to analyze the aircraft flight information provided by the user and infer the aircraft's affiliation, likely destination, and mission purpose."""


def get_system_prompt() -> str:
    """Get the system prompt."""
    config = get_config()
    llm_config = config.get_llm_config()
    system_prompt = llm_config.get("system_prompt", "")
    return system_prompt.strip() if system_prompt else DEFAULT_SYSTEM_PROMPT


# Tool definitions (used by the Bedrock API)
# ============================================================================
# Tool configuration - fully autonomous agent architecture
# ============================================================================
TOOLS_CONFIG = [
    {
        "toolSpec": {
            "name": "get_cached_aircraft_info",
            "description": """Query the database for cached aircraft static information.

Returns:
- Cached owner, operator, history, and other static info
- Data update time and freshness level (fresh/good/stale/expired)
- Whether cache exists for this aircraft

Based on the returned freshness info, you can decide whether to call search_web to refresh the data.
Freshness levels:
- fresh (< 7 days): Data is very recent, safe to use
- good (7-30 days): Reasonably fresh, OK for static info
- stale (30-90 days): May be outdated, consider refreshing for important analysis
- expired (> 90 days): Likely outdated, recommend refreshing""",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "registration": {
                            "type": "string",
                            "description": "Aircraft registration number (e.g., N12345, G-ABCD, K-2999)",
                        },
                        "hex_code": {"type": "string", "description": "ICAO 24-bit hex code"},
                    },
                    "required": [],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_web",
            "description": """Real-time web search for aviation-related information.

Use this tool to:
- Search for aircraft owner/operator information
- Search for recent news and events
- Search for destination-related information
- Search for any aviation intelligence

Search results are NOT automatically cached. If you find valuable static information,
call save_aircraft_info to cache it for future use.

Search types:
- basic: 1 credit, standard search results
- advanced: 2 credits, more detailed results with raw page content""",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query string"},
                        "search_type": {
                            "type": "string",
                            "enum": ["basic", "advanced"],
                            "description": "Search depth: basic (1 credit) or advanced (2 credits, more detailed)",
                        },
                    },
                    "required": ["query"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "save_aircraft_info",
            "description": """Save aircraft static information to the database cache.

Use this tool to:
- Save newly discovered owner/operator information
- Update expired cache data
- Mark aircraft as military/government/VIP

After saving, the next analysis of the same aircraft can retrieve info directly from cache,
saving search costs and improving response time.""",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "registration": {
                            "type": "string",
                            "description": "Aircraft registration number (required)",
                        },
                        "hex_code": {"type": "string", "description": "ICAO 24-bit hex code"},
                        "owner": {"type": "string", "description": "Aircraft owner name"},
                        "operator": {"type": "string", "description": "Aircraft operator name"},
                        "aircraft_model": {
                            "type": "string",
                            "description": "Aircraft model (e.g., Boeing 737-800, Il-76MD)",
                        },
                        "manufacturer": {"type": "string", "description": "Aircraft manufacturer"},
                        "country": {"type": "string", "description": "Country of registration"},
                        "is_military": {
                            "type": "boolean",
                            "description": "Whether this is a military aircraft",
                        },
                        "is_government": {
                            "type": "boolean",
                            "description": "Whether this is a government aircraft",
                        },
                        "is_vip": {
                            "type": "boolean",
                            "description": "Whether this is a VIP/private aircraft",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Background summary of the aircraft and owner",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags for categorization (e.g., ['military', 'transport', 'strategic'])",
                        },
                    },
                    "required": ["registration"],
                }
            },
        }
    },
]

# Progress callback type definition
from collections.abc import Callable

ProgressCallback = Callable[[AnalysisProgress], None]


class FlightAnalysisAgent:
    """
    FlightAnalysisAgent - Aviation intelligence analysis agent based on the AWS Bedrock API.

    Calls the Claude model directly via the AWS Bedrock API and integrates Tavily
    search to provide professional aviation intelligence analysis.
    """

    def __init__(
        self,
        enable_web_search: bool = True,
        provider_config: dict | None = None,
        progress_callback: ProgressCallback | None = None,
    ):
        """
        Initialize the aviation analysis agent.

        Args:
            enable_web_search: Whether to enable web search
            provider_config: LLM provider configuration
            progress_callback: Progress callback used to report analysis progress
        """
        self.enable_web_search = enable_web_search
        self.provider_config = provider_config or {}
        self.progress_callback = progress_callback
        self.current_progress = AnalysisProgress()

        # Initialize the Bedrock client
        try:
            # Get AWS configuration from config or environment variables
            region_name = self.provider_config.get("aws_region") or os.getenv(
                "AWS_REGION", "us-west-2"
            )
            self.model_id = self.provider_config.get(
                "bedrock_model_id", "anthropic.claude-sonnet-4-20250514-v1:0"
            )

            self.bedrock = boto3.client(service_name="bedrock-runtime", region_name=region_name)
            logger.info(
                f"Initialized Bedrock client in region {region_name}, model: {self.model_id}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize Bedrock client: {e}")
            raise

        # Token usage statistics
        self.last_token_usage: TokenUsage | None = None
        self.total_token_usage: TokenUsage = TokenUsage()

        logger.info(f"FlightAnalysisAgent initialized (web_search: {enable_web_search})")

    def _report_progress(
        self, step: int, action: str, details: str = "", tool_call: dict | None = None
    ):
        """Report analysis progress.

        Args:
            step: Current step number
            action: Current action description
            details: Detailed information
            tool_call: Tool call information
        """
        self.current_progress.step = step
        self.current_progress.current_action = action
        self.current_progress.details = details
        if tool_call:
            self.current_progress.tool_calls.append(tool_call)

        logger.info(f"Progress [{step}/{self.current_progress.total_steps}]: {action} - {details}")

        if self.progress_callback:
            try:
                self.progress_callback(self.current_progress)
            except Exception as e:
                logger.warning(f"Progress callback failed: {e}")

    def _call_bedrock_with_tools(
        self, user_message: str, max_iterations: int = 10
    ) -> tuple[str, TokenUsage]:
        """
        Call the Bedrock API with tool support.

        Args:
            user_message: User-provided input message
            max_iterations: Maximum number of tool-call iterations

        Returns:
            tuple: (analysis result text, token usage statistics)
        """
        messages = []
        token_usage = TokenUsage()
        system_prompt = get_system_prompt()

        # Reset progress
        self.current_progress = AnalysisProgress()
        self._report_progress(1, "Initializing analysis", "Preparing analysis environment...")

        for iteration in range(max_iterations):
            logger.info(f"--- Bedrock API Call Iteration {iteration + 1} ---")
            self._report_progress(
                2 + iteration, "AI analyzing", f"Reasoning round {iteration + 1}..."
            )
            # Build request
            if iteration == 0:
                # Initial request
                request_body = {
                    "modelId": self.model_id,
                    "messages": [{"role": "user", "content": [{"text": user_message}]}],
                    "system": [{"text": system_prompt}],
                    "toolConfig": {"tools": TOOLS_CONFIG, "toolChoice": {"auto": {}}},
                    "inferenceConfig": {"maxTokens": 4096, "temperature": 0.7},
                }
            else:
                # Continue the conversation (include previous message history)
                request_body = {
                    "modelId": self.model_id,
                    "messages": messages,
                    "system": [{"text": system_prompt}],
                    "toolConfig": {"tools": TOOLS_CONFIG, "toolChoice": {"auto": {}}},
                    "inferenceConfig": {"maxTokens": 4096, "temperature": 0.7},
                }

            try:
                # Call the Bedrock API
                response = self.bedrock.converse(**request_body)

                # Collect token usage statistics
                usage = response.get("usage", {})
                input_tokens = usage.get("inputTokens", 0)
                output_tokens = usage.get("outputTokens", 0)
                token_usage.add(input_tokens, output_tokens)
                logger.info(
                    f"Iteration {iteration + 1} tokens: input={input_tokens}, output={output_tokens}"
                )

                # Get response content
                output = response.get("output", {})
                message = output.get("message", {})

                # Append the message to the history
                if iteration == 0:
                    messages.append({"role": "user", "content": [{"text": user_message}]})

                messages.append(message)

                # Check for tool calls and collect all tool-call results
                tool_uses = message.get("content", [])
                tool_results = []

                for content in tool_uses:
                    if content.get("toolUse"):
                        tool_use = content["toolUse"]
                        tool_name = tool_use["name"]
                        tool_input = tool_use["input"]
                        tool_use_id = tool_use["toolUseId"]

                        logger.info(f"Tool call {iteration + 1}: {tool_name}({tool_input})")

                        # Report tool-call progress
                        tool_descriptions = {
                            "get_cached_aircraft_info": "Query cached data",
                            "search_web": "Web search",
                            "save_aircraft_info": "Save aircraft info",
                        }
                        tool_desc = tool_descriptions.get(tool_name, tool_name)
                        tool_details = ""
                        if tool_name == "search_web":
                            tool_details = tool_input.get("query", "")[:50]
                        elif tool_name == "get_cached_aircraft_info":
                            tool_details = tool_input.get("registration") or tool_input.get(
                                "hex_code", ""
                            )

                        self._report_progress(
                            2 + iteration,
                            f"Tool call: {tool_desc}",
                            tool_details,
                            {"tool": tool_name, "input": tool_input, "iteration": iteration + 1},
                        )

                        # Execute tool call - fully autonomous agent architecture
                        if tool_name == "get_cached_aircraft_info":
                            registration = tool_input.get("registration", "")
                            hex_code = tool_input.get("hex_code", "")
                            tool_result = get_cached_aircraft_info(registration, hex_code)
                        elif tool_name == "search_web":
                            query = tool_input.get("query", "")
                            search_type = tool_input.get("search_type", "basic")
                            tool_result = search_web(query, search_type)
                        elif tool_name == "save_aircraft_info":
                            tool_result = save_aircraft_info(
                                registration=tool_input.get("registration", ""),
                                hex_code=tool_input.get("hex_code"),
                                owner=tool_input.get("owner"),
                                operator=tool_input.get("operator"),
                                aircraft_model=tool_input.get("aircraft_model"),
                                manufacturer=tool_input.get("manufacturer"),
                                country=tool_input.get("country"),
                                is_military=tool_input.get("is_military", False),
                                is_government=tool_input.get("is_government", False),
                                is_vip=tool_input.get("is_vip", False),
                                summary=tool_input.get("summary"),
                                tags=tool_input.get("tags"),
                            )
                        else:
                            tool_result = f"Unknown tool: {tool_name}"

                        # Collect tool results
                        tool_results.append(
                            {
                                "toolResult": {
                                    "toolUseId": tool_use_id,
                                    "content": [{"text": tool_result}],
                                }
                            }
                        )

                # If there were tool calls, append all results to the same message
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})

                # If there were no tool calls, return the final result
                if not tool_results:
                    # Extract text content
                    for content in message.get("content", []):
                        if content.get("text"):
                            logger.info(f"Analysis completed. Total {token_usage}")
                            self._report_progress(10, "Analysis complete", "Generating report...")
                            return content["text"], token_usage

                    # If no text was found, return an error
                    self._report_progress(
                        10, "Analysis complete", "Failed to generate analysis result"
                    )
                    return "Failed to generate analysis result", token_usage

            except Exception as e:
                logger.error(f"Bedrock API call failed at iteration {iteration}: {e}")
                if iteration == 0:
                    raise
                else:
                    # If this is not the first call, try to return any existing partial result
                    for msg in reversed(messages):
                        if msg.get("role") == "assistant":
                            for content in msg.get("content", []):
                                if content.get("text"):
                                    return content["text"], token_usage
                    return f"Error during analysis: {e!s}", token_usage

        # Reached the maximum iteration count
        logger.warning(f"Reached maximum iterations ({max_iterations}) for tool calls")

        # Try to return the last assistant message
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                for content in msg.get("content", []):
                    if content.get("text"):
                        return content["text"], token_usage

        return "Analysis incomplete (maximum tool-call iterations reached)", token_usage

    def analyze_aircraft(
        self, aircraft_data: dict[str, Any], return_markdown: bool = False
    ) -> str | dict[str, str]:
        """
        Analyze aircraft data and generate an intelligence report.

        Args:
            aircraft_data: Aircraft data obtained from ADS-B Exchange
            return_markdown: If True, return a dictionary containing both markdown and html

        Returns:
            If return_markdown=False: intelligence analysis report (HTML format)
            If return_markdown=True: {'html': html_report, 'markdown': markdown_report}
        """
        try:
            # 1. Convert data format
            flight_data = FlightData.from_dict(aircraft_data)
            logger.info(
                f"Starting analysis for aircraft: {flight_data.registration or flight_data.icao or 'Unknown'}"
            )

            # 2. Build analysis input
            flight_input = flight_data.to_analysis_string()

            # 3. Call the Bedrock API to run the analysis
            logger.info("Sending request to aviation intelligence analyst...")
            analysis_markdown_result, token_usage = self._call_bedrock_with_tools(flight_input)
            html_analysis_result = convert_markdown_to_html(analysis_markdown_result)

            # 4. Save token usage statistics
            self.last_token_usage = token_usage
            self.total_token_usage.add(
                token_usage.input_tokens, token_usage.output_tokens, token_usage.api_calls
            )

            logger.info(
                f"Token usage - current: {token_usage}, cumulative: {self.total_token_usage}"
            )

            # 5. Wrap the result in HTML format (for compatibility with the existing system)
            html_report = self._wrap_result_as_html(flight_data, html_analysis_result, token_usage)

            logger.info(f"Analysis complete; report length: {len(html_report)} characters")

            if return_markdown:
                return {"html": html_report, "markdown": analysis_markdown_result}
            return html_report

        except Exception as e:
            logger.error(f"FlightAnalysisAgent analysis failed: {e}")
            return self._generate_error_report(aircraft_data, str(e))

    def _wrap_result_as_html(
        self, flight_data: FlightData, agent_result: str, token_usage: TokenUsage | None = None
    ) -> str:
        """Wrap the agent's analysis result into an HTML-formatted report."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Get the HTML template from the configuration file
        config = get_config()
        templates = config.get_templates_config()
        html_template = templates.get("report_html", "")

        # Token statistics
        input_tokens = token_usage.input_tokens if token_usage else 0
        output_tokens = token_usage.output_tokens if token_usage else 0
        total_tokens = token_usage.total_tokens if token_usage else 0
        api_calls = token_usage.api_calls if token_usage else 0

        if html_template:
            # Use the template from the configuration file
            html_report = html_template.format(
                flight_number=flight_data.flight_number or "Unknown",
                registration=flight_data.registration or "Unknown",
                country_of_registration=flight_data.country_of_registration or "Unknown",
                icao=flight_data.icao or "Unknown",
                aircraft_type=flight_data.aircraft_type or "Unknown",
                squawk=flight_data.squawk or "Unknown",
                current_position=flight_data.current_position or "Unknown",
                altitude=f"{flight_data.altitude:,} ft"
                if isinstance(flight_data.altitude, (int, float)) and flight_data.altitude
                else (str(flight_data.altitude) if flight_data.altitude else "Unknown"),
                speed=f"{flight_data.speed} knots" if flight_data.speed else "Unknown",
                heading=f"{flight_data.heading}°" if flight_data.heading else "Unknown",
                agent_result=agent_result,
                timestamp=timestamp,
                input_tokens=f"{input_tokens:,}",
                output_tokens=f"{output_tokens:,}",
                total_tokens=f"{total_tokens:,}",
                api_calls=api_calls,
            )
        else:
            # Use the default inline template as a fallback
            html_report = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <title>Aviation Intelligence Analysis Report</title>
            </head>
            <body>
                <h1>Aviation Intelligence Analysis Report</h1>
                <h2>Aircraft Information</h2>
                <p>Registration: {flight_data.registration or "Unknown"}</p>
                <p>ICAO: {flight_data.icao or "Unknown"}</p>
                <p>Aircraft Type: {flight_data.aircraft_type or "Unknown"}</p>
                <h2>Analysis Result</h2>
                <div>{agent_result}</div>
                <p>Generated at: {timestamp}</p>
                <p>Token usage: input {input_tokens:,} / output {output_tokens:,} / total {total_tokens:,} (API calls: {api_calls})</p>
            </body>
            </html>
            """

        return (html_report or "").strip()

    def _generate_error_report(self, aircraft_data: dict[str, Any], error_msg: str) -> str:
        """Generate an error report."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Get the error template from the configuration file
        config = get_config()
        templates = config.get_templates_config()
        error_template = templates.get("error_html", "")

        registration = aircraft_data.get("r", "Unknown")
        flight_number = (aircraft_data.get("flight") or "").strip() or "Unknown"
        icao = aircraft_data.get("hex", "Unknown")
        aircraft_type = aircraft_data.get("t", "Unknown")
        altitude = f"{aircraft_data.get('alt_baro', 'Unknown')} ft"
        speed = f"{aircraft_data.get('gs', 'Unknown')} knots"
        squawk = aircraft_data.get("squawk", "Unknown")

        if error_template:
            # Use the template from the configuration file
            return error_template.format(
                error_msg=error_msg,
                registration=registration,
                flight_number=flight_number,
                icao=icao,
                aircraft_type=aircraft_type,
                altitude=altitude,
                speed=speed,
                squawk=squawk,
                timestamp=timestamp,
            )
        else:
            # Use the default inline template as a fallback
            return f"""
            <div style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background: #f8f9fa;">
                <div style="background: #dc3545; color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px;">
                    <h1 style="margin: 0;">Aviation Analysis System Error</h1>
                </div>
                <div style="background: white; padding: 20px; border-radius: 10px; border-left: 5px solid #dc3545;">
                    <h2>Error Details</h2>
                    <p><strong>Error message:</strong> {error_msg}</p>
                    <p><strong>Aircraft identifier:</strong> {registration}</p>
                    <p><strong>ICAO:</strong> {icao}</p>
                    <p><strong>Time:</strong> {timestamp}</p>
                </div>
            </div>
            """

    def get_last_token_usage(self) -> TokenUsage | None:
        """Get the token usage statistics for the most recent analysis."""
        return self.last_token_usage

    def get_total_token_usage(self) -> TokenUsage:
        """Get cumulative token usage statistics."""
        return self.total_token_usage

    def reset_token_usage(self):
        """Reset token usage statistics."""
        self.last_token_usage = None
        self.total_token_usage = TokenUsage()
        logger.info("Token usage statistics reset")

    def get_token_usage_summary(self) -> dict[str, Any]:
        """Get a summary of token usage statistics."""
        return {
            "last_analysis": self.last_token_usage.to_dict() if self.last_token_usage else None,
            "total": self.total_token_usage.to_dict(),
            "model_id": self.model_id,
        }


if __name__ == "__main__":
    # Test code
    import os
    import sys

    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    from src.analysis.tavily_search import TavilySearchClient
    from src.media.markdown_converter import convert_markdown_to_html

    def test_bedrock_agent():
        """Test the AWS Bedrock-based aviation intelligence analyst."""
        print("Testing the AWS Bedrock-based aviation intelligence analyst")
        print("=" * 60)

        # Test data - simulated E-4B Nightwatch
        test_data = {
            "r": "75-0125",  # US military registration
            "flight": "GORDO14",  # Military callsign
            "t": "B742",  # Boeing 747-200
            "hex": "ADFEB6",  # ICAO code
            "lat": 41.107815,  # Near Omaha
            "lon": -95.889854,  # Near Offutt Air Force Base
            "alt_baro": 1200,  # Low altitude, just after takeoff
            "gs": 162.7,  # Takeoff speed
            "track": 309.26,  # Northwest heading
            "vert_rate": 448,  # Climbing
            "squawk": "2470",  # Military transponder code
        }

        # Create the analyst and run the analysis
        analyst = FlightAnalysisAgent(enable_web_search=True)
        report = analyst.analyze_aircraft(test_data)

        # Save the report
        with open("bedrock_agent_report.html", "w", encoding="utf-8") as f:
            f.write(report)

        print("Analysis complete")
        print(f"Report length: {len(report)} characters")
        print("Report saved to: bedrock_agent_report.html")

        # Display token usage statistics
        token_summary = analyst.get_token_usage_summary()
        print("\nToken usage statistics:")
        if token_summary["last_analysis"]:
            last = token_summary["last_analysis"]
            print(
                f"  This analysis: input {last['input_tokens']:,} / output {last['output_tokens']:,} / total {last['total_tokens']:,}"
            )
            print(f"  API call count: {last['api_calls']}")
        print(f"  Model: {token_summary['model_id']}")

    test_bedrock_agent()
