"""
Flight-matrix aviation-domain scraper result models.

Framework-level models (ScraperTask / ScraperResult / TaskStatus / WorkerStatus
/ WorkerInfo / ScraperConfig / utc_now) are re-exported from the
resilient_scraper submodule. Only aviation-specific Pydantic classes live here.
They will move into the submodule alongside their owning scrapers in step 4.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Re-export framework-level models from submodule for backward compatibility.
from resilient_scraper.models import (
    ScraperConfig,
    ScraperResult,
    ScraperTask,
    TaskStatus,
    WorkerInfo,
    WorkerStatus,
    utc_now,
)

__all__ = [
    "AirportDataAircraftData",
    "AirportDataResult",
    "FR24AircraftResult",
    "FR24FlightsResult",
    "FR24MapAircraftData",
    "FR24MapResult",
    "FlightData",
    "ImageMetadata",
    "JetPhotosResult",
    "ScraperConfig",
    "ScraperResult",
    "ScraperTask",
    "TaskStatus",
    "WorkerInfo",
    "WorkerStatus",
    "utc_now",
]


class ImageMetadata(BaseModel):
    """Metadata for a single aircraft image.

    Attributes:
        image_path: Local or S3 path to the image.
        source_url: Original photo page URL.
        jetphotos_id: JetPhotos photo ID.
        photographer: Photographer name.
        photo_date: Date photo was taken.
        upload_date: Date photo was uploaded.
        location: Location where photo was taken.
        airport_icao: Airport ICAO code.
        airport_name: Airport name.
        file_size_bytes: File size in bytes.
        notes: Photo notes/remarks (e.g., special livery, historical info).
        camera: Camera model used to take the photo.
        views: Number of views on JetPhotos.
        likes: Number of likes on JetPhotos.
        badges: Photo badges (e.g., "Photo of the Day", "Editor's Pick").
        html_s3_path: S3 path to the saved HTML for debugging/re-extraction.
    """

    image_path: str
    source_url: str | None = None
    jetphotos_id: str | None = None
    photographer: str | None = None
    photo_date: str | None = None  # ISO date string
    upload_date: str | None = None  # ISO date string
    location: str | None = None
    airport_icao: str | None = None
    airport_name: str | None = None
    file_size_bytes: int | None = None
    notes: str | None = None
    camera: str | None = None
    views: int | None = None
    likes: int | None = None
    badges: str | None = None
    html_s3_path: str | None = None


class JetPhotosResult(ScraperResult):
    """Result from JetPhotos scraper.

    Attributes:
        registration: Aircraft registration number.
        image_paths: List of downloaded image paths (local or S3).
        image_count: Number of images downloaded.
        s3_uploaded: Whether images were uploaded to S3.
        images_metadata: Detailed metadata for each downloaded image.
    """

    registration: str = ""
    image_paths: list[str] = Field(default_factory=list)
    image_count: int = 0
    s3_uploaded: bool = False
    images_metadata: list[ImageMetadata] = Field(default_factory=list)


class FlightData(BaseModel):
    """Individual flight record (arrival or departure).

    Attributes:
        flight_type: Type of flight ('arrival' or 'departure').
        flight_number: Flight number (e.g., 'CA123').
        callsign: Aircraft callsign.
        airline_name: Name of the airline.
        airline_iata: IATA code of the airline.
        remote_airport_iata: Origin (arrivals) or destination (departures) IATA.
        remote_airport_name: Origin or destination airport name.
        aircraft_type: ICAO aircraft type code.
        aircraft_registration: Aircraft registration number.
        scheduled_time: Scheduled departure/arrival time.
        estimated_time: Estimated departure/arrival time.
        actual_time: Actual departure/arrival time.
        status: Flight status (Landed, En Route, Scheduled, Delayed, Cancelled).
        terminal: Terminal designation.
        gate: Gate designation.
        flight_id: FR24 internal flight identifier.
    """

    flight_type: str | None = None
    flight_number: str | None = None
    callsign: str | None = None
    airline_name: str | None = None
    airline_iata: str | None = None
    remote_airport_iata: str | None = None
    remote_airport_name: str | None = None
    aircraft_type: str | None = None
    aircraft_registration: str | None = None
    scheduled_time: datetime | None = None
    estimated_time: datetime | None = None
    actual_time: datetime | None = None
    status: str | None = None
    terminal: str | None = None
    gate: str | None = None
    flight_id: str | None = None


class FR24FlightsResult(ScraperResult):
    """Result from FR24 arrivals/departures scraper.

    Attributes:
        airport_code: Airport code used for scraping.
        airport_name: Full name of the airport.
        flight_type: Type of flights ('arrival' or 'departure').
        flights: List of extracted flight records.
        flights_count: Number of flights extracted.
        date_range_start: Earliest flight time in the data.
        date_range_end: Latest flight time in the data.
        load_more_clicks: Number of pagination clicks performed.
    """

    airport_code: str = ""
    airport_name: str = ""
    flight_type: str = ""  # "arrival" or "departure"
    flights: list[FlightData] = Field(default_factory=list)
    flights_count: int = 0
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    load_more_clicks: int = 0


class FR24AircraftResult(ScraperResult):
    """Result from FR24 aircraft schedule scraper.

    Attributes:
        aircraft_registration: Aircraft registration number used for scraping.
        aircraft_type: ICAO aircraft type code (e.g., 'A320', 'B738').
        aircraft_model: Full aircraft model name (e.g., 'Airbus A320-200').
        airline_name: Current operator/airline name.
        flights: List of extracted flight records.
        flights_count: Number of flights extracted.
        date_range_start: Earliest flight time in the data.
        date_range_end: Latest flight time in the data.
        load_more_clicks: Number of pagination clicks performed.
    """

    aircraft_registration: str = ""
    aircraft_type: str | None = None
    aircraft_model: str | None = None
    airline_name: str | None = None
    flights: list[FlightData] = Field(default_factory=list)
    flights_count: int = 0
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    load_more_clicks: int = 0


class FR24MapAircraftData(BaseModel):
    """Individual aircraft data from FR24 map.

    Attributes:
        fr24_id: FR24 internal aircraft/flight ID.
        flight_number: Flight number (e.g., 'CA123').
        callsign: Aircraft callsign.
        registration: Aircraft registration number.
        aircraft_type: ICAO aircraft type code (e.g., 'A320', 'B738').
        latitude: Current latitude.
        longitude: Current longitude.
        altitude: Current altitude in feet.
        ground_speed: Ground speed in knots.
        heading: Heading in degrees.
        vertical_speed: Vertical speed in feet per minute.
        squawk: Transponder squawk code.
        origin_iata: Origin airport IATA code.
        destination_iata: Destination airport IATA code.
        airline_iata: Airline IATA code.
        airline_name: Airline name.
        on_ground: Whether aircraft is on ground.
        timestamp: Data timestamp from FR24.
    """

    fr24_id: str | None = None
    flight_number: str | None = None
    callsign: str | None = None
    registration: str | None = None
    aircraft_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: int | None = None
    ground_speed: int | None = None
    heading: int | None = None
    vertical_speed: int | None = None
    squawk: str | None = None
    origin_iata: str | None = None
    destination_iata: str | None = None
    airline_iata: str | None = None
    airline_name: str | None = None
    on_ground: bool = False
    timestamp: datetime | None = None


class FR24MapResult(ScraperResult):
    """Result from FR24 map scraper.

    Attributes:
        center_lat: Center latitude of the map view.
        center_lon: Center longitude of the map view.
        zoom_level: Map zoom level.
        bounds: Map bounds (north, south, east, west).
        aircraft: List of aircraft in view.
        aircraft_count: Number of aircraft found.
        scraped_at: When the data was scraped.
    """

    center_lat: float = 0.0
    center_lon: float = 0.0
    zoom_level: int = 4
    bounds: dict[str, float] = Field(default_factory=dict)
    aircraft: list[FR24MapAircraftData] = Field(default_factory=list)
    aircraft_count: int = 0
    scraped_at: datetime | None = None


class AirportDataAircraftData(BaseModel):
    """Aircraft data from airport-data.com.

    Attributes:
        registration: Aircraft registration/tail number.
        year_built: Year the aircraft was built.
        manufacturer: Aircraft manufacturer name.
        model: Aircraft model name.
        serial_number: Construction/serial number (C/N).
        engines: Number of engines.
        seats: Seat count.
        location: Current location.
        owner: Aircraft owner (from detail page).
        status: Aircraft status (Active, De-registered, etc.).
        mode_s_code: Mode S transponder code.
        delivery_date: Delivery date (from detail page).
        source_url: URL of the source page.
    """

    registration: str
    year_built: int | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    engines: int | None = None
    seats: int | None = None
    location: str | None = None
    owner: str | None = None
    status: str | None = None
    mode_s_code: str | None = None
    delivery_date: str | None = None
    source_url: str | None = None


class AirportDataResult(ScraperResult):
    """Result from AirportData scraper.

    Attributes:
        scrape_mode: Scrape mode ("manufacturers", "manufacturer", or "aircraft").
        manufacturer_name: Name of the manufacturer being scraped.
        manufacturer_urls: List of manufacturer URLs (for manufacturers mode).
        aircraft: List of extracted aircraft data.
        aircraft_count: Number of aircraft found.
        pages_scraped: Number of pages scraped.
        records_updated: Number of aircraft_static_info records updated.
        s3_paths: List of S3 paths for uploaded HTML files.
    """

    scrape_mode: str = "manufacturer"
    manufacturer_name: str = ""
    manufacturer_urls: list[str] = Field(default_factory=list)
    aircraft: list[AirportDataAircraftData] = Field(default_factory=list)
    aircraft_count: int = 0
    pages_scraped: int = 0
    records_updated: int = 0
    s3_paths: list[str] = Field(default_factory=list)
