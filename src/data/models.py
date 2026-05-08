"""
SQLAlchemy ORM models for the aircraft tracking system.

This module defines the database schema using SQLAlchemy declarative models.
All database tables are defined here for centralized schema management.
"""

import secrets

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

# SQLAlchemy Base - shared across all models
Base = declarative_base()


# =============================================================================
# Multi-User Subscription System Models
# =============================================================================


class User(Base):
    """User account table for multi-user subscription system.

    Stores user accounts that can subscribe to aircraft tracking reports.
    Each user can have their own filters, cooldowns, and usage quotas.

    Attributes:
        id: Auto-incrementing primary key
        email: Unique email address (primary identifier)
        name: Display name
        status: Account status (active, suspended, deleted)
        api_key: Optional API key for programmatic access
        created_at: Account creation timestamp
        updated_at: Last update timestamp
    """

    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(100))
    status = Column(String(20), default="active", index=True)  # active, suspended, deleted
    api_key = Column(String(64), unique=True, index=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    subscriptions = relationship(
        "Subscription", back_populates="user", cascade="all, delete-orphan"
    )
    filters = relationship("UserFilter", back_populates="user", cascade="all, delete-orphan")
    cooldowns = relationship("UserCooldown", back_populates="user", cascade="all, delete-orphan")
    usage_records = relationship("UserUsage", back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_user_status", "status"),
        Index("idx_user_created", "created_at"),
    )

    def generate_api_key(self) -> str:
        """Generate a new API key for this user.

        Returns:
            The generated API key
        """
        self.api_key = secrets.token_hex(32)
        return self.api_key

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "status": self.status,
            "api_key": self.api_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Subscription(Base):
    """Subscription table for user subscription tiers.

    Manages subscription plans for users, including tier level,
    feature toggles, and subscription period.

    Attributes:
        id: Auto-incrementing primary key
        user_id: Foreign key to users table
        tier: Subscription tier (basic, premium, enterprise)
        status: Subscription status (active, expired, cancelled)
        enable_maps: Whether to include maps in reports
        enable_aircraft_images: Whether to include aircraft images
        enable_deep_analysis: Whether to enable LLM deep analysis
        starts_at: Subscription start date
        expires_at: Subscription expiration date (NULL for non-expiring)
        created_at: Record creation timestamp
    """

    __tablename__ = "subscriptions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tier = Column(String(20), default="basic", index=True)  # basic, premium, enterprise
    status = Column(String(20), default="active", index=True)  # active, expired, cancelled

    # Feature toggles (override tier defaults)
    enable_maps = Column(Boolean, default=True)
    enable_aircraft_images = Column(Boolean, default=True)
    enable_deep_analysis = Column(Boolean, default=False)

    # Report configuration (user-specific settings)
    cooldown_hours = Column(Numeric(6, 2), default=12.0)  # Cooldown hours between reports
    daily_report_limit = Column(Integer, default=-1)  # Daily report limit (-1 = unlimited)
    monthly_report_limit = Column(Integer, default=-1)  # Monthly report limit (-1 = unlimited)
    max_filters = Column(Integer, default=-1)  # Max number of filters (-1 = unlimited)

    # Subscription period
    starts_at = Column(DateTime, nullable=False, default=func.now())
    expires_at = Column(DateTime)  # NULL means no expiration

    created_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("User", back_populates="subscriptions")

    __table_args__ = (
        Index("idx_subscription_user", "user_id"),
        Index("idx_subscription_status", "status"),
        Index("idx_subscription_tier", "tier"),
        Index("idx_subscription_expires", "expires_at"),
    )

    def is_active(self) -> bool:
        """Check if subscription is currently active.

        Returns:
            True if subscription is active and not expired
        """
        from datetime import datetime

        if self.status != "active":
            return False
        if self.expires_at and self.expires_at < datetime.now():
            return False
        return True

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tier": self.tier,
            "status": self.status,
            "enable_maps": self.enable_maps,
            "enable_aircraft_images": self.enable_aircraft_images,
            "enable_deep_analysis": self.enable_deep_analysis,
            "cooldown_hours": float(self.cooldown_hours)
            if self.cooldown_hours is not None
            else 12.0,
            "daily_report_limit": self.daily_report_limit
            if self.daily_report_limit is not None
            else -1,
            "monthly_report_limit": self.monthly_report_limit
            if self.monthly_report_limit is not None
            else -1,
            "max_filters": self.max_filters if self.max_filters is not None else -1,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_active": self.is_active(),
        }


class UserFilter(Base):
    """User-specific SQL filter table.

    Stores custom SQL filter rules for each user.
    Each user can have multiple filters with different priorities.

    Attributes:
        id: Auto-incrementing primary key
        user_id: Foreign key to users table
        name: Human-readable filter name
        description: Optional description of what this filter does
        filter_sql: SQL WHERE clause for filtering aircraft
        is_active: Whether this filter is currently enabled
        priority: Priority order (higher = processed first)
        created_at: Record creation timestamp
        updated_at: Last update timestamp
    """

    __tablename__ = "user_filters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    filter_sql = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, index=True)
    priority = Column(Integer, default=0)  # Higher priority = processed first
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="filters")

    __table_args__ = (
        Index("idx_user_filter_user", "user_id"),
        Index("idx_user_filter_active", "user_id", "is_active"),
        Index("idx_user_filter_priority", "user_id", "priority"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "description": self.description,
            "filter_sql": self.filter_sql,
            "is_active": self.is_active,
            "priority": self.priority,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserCooldown(Base):
    """User-specific report cooldown tracking table.

    Tracks per-user cooldowns for aircraft reports.
    Ensures users don't receive duplicate reports for the same aircraft.

    Attributes:
        id: Auto-incrementing primary key
        user_id: Foreign key to users table
        aircraft_hex: ICAO hex code of the aircraft
        last_report_time: When the last report was sent
        last_latitude: Latitude when last report was sent
        last_longitude: Longitude when last report was sent
        report_count: Total reports sent for this aircraft to this user
    """

    __tablename__ = "user_cooldowns"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    aircraft_hex = Column(String(6), nullable=False)
    last_report_time = Column(DateTime, nullable=False)
    last_latitude = Column(Numeric(10, 7))
    last_longitude = Column(Numeric(11, 7))
    report_count = Column(Integer, default=1)

    # Relationships
    user = relationship("User", back_populates="cooldowns")

    __table_args__ = (
        Index("idx_user_aircraft_cooldown", "user_id", "aircraft_hex", unique=True),
        Index("idx_user_cooldown_time", "user_id", "last_report_time"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "aircraft_hex": self.aircraft_hex,
            "last_report_time": self.last_report_time.isoformat()
            if self.last_report_time
            else None,
            "last_latitude": float(self.last_latitude) if self.last_latitude else None,
            "last_longitude": float(self.last_longitude) if self.last_longitude else None,
            "report_count": self.report_count,
        }


class UserUsage(Base):
    """User usage tracking table for quota management.

    Tracks usage metrics per user per time period.
    Used to enforce subscription quotas.

    Attributes:
        id: Auto-incrementing primary key
        user_id: Foreign key to users table
        period_start: Start date of the usage period
        period_type: Type of period (daily, monthly)
        reports_sent: Number of reports sent in this period
        deep_analyses_used: Number of deep analyses performed
        emails_sent: Number of emails sent
    """

    __tablename__ = "user_usage"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    period_start = Column(Date, nullable=False)
    period_type = Column(String(10), default="monthly")  # daily, monthly
    reports_sent = Column(Integer, default=0)
    deep_analyses_used = Column(Integer, default=0)
    emails_sent = Column(Integer, default=0)

    # Relationships
    user = relationship("User", back_populates="usage_records")

    __table_args__ = (
        Index("idx_user_usage_period", "user_id", "period_start", "period_type", unique=True),
        Index("idx_user_usage_user", "user_id"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_type": self.period_type,
            "reports_sent": self.reports_sent,
            "deep_analyses_used": self.deep_analyses_used,
            "emails_sent": self.emails_sent,
        }


# =============================================================================
# Aircraft Tracking Models
# =============================================================================


class AircraftSnapshot(Base):
    """Aircraft data snapshot table.

    Stores point-in-time snapshots of aircraft positions and metadata
    received from the ADS-B Exchange API.

    Attributes:
        id: Auto-incrementing primary key
        snapshot_time: Timestamp when the snapshot was captured
        hex: ICAO 24-bit hex code (aircraft identifier)
        flight_number: Flight number (e.g., 'UAL123')
        registration: Aircraft registration (e.g., 'N12345')
        aircraft_type: ICAO aircraft type code (e.g., 'B738')
        latitude: Current latitude
        longitude: Current longitude
        altitude_baro: Barometric altitude in feet
        altitude_geom: Geometric altitude in feet
        ground_speed: Ground speed in knots
        track: Track/heading in degrees
        vertical_rate: Vertical rate in feet per minute
        squawk: Transponder squawk code
        emergency: Emergency status
        category: Aircraft category code
        country_of_registration: Country where aircraft is registered
        current_country: Country where aircraft is currently located
        is_military: Whether aircraft is military
        is_interesting: Whether aircraft meets interesting criteria
        raw_data: Full raw API response data (JSON)
    """

    __tablename__ = "aircraft_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_time = Column(DateTime, nullable=False, default=func.now())

    # Aircraft identification
    hex = Column(String(6), nullable=False, index=True)
    flight_number = Column(String(10), index=True)
    registration = Column(String(20), index=True)
    aircraft_type = Column(String(10), index=True)

    # Position information
    latitude = Column(Numeric(10, 7))
    longitude = Column(Numeric(11, 7))
    altitude_baro = Column(Integer)
    altitude_geom = Column(Integer)

    # Movement information
    ground_speed = Column(Numeric(6, 2))
    track = Column(Numeric(5, 2))
    vertical_rate = Column(Integer)

    # Status information
    squawk = Column(String(4))
    emergency = Column(String(20))
    category = Column(String(2))

    # Auto-calculated metadata
    country_of_registration = Column(String(50), index=True)
    current_country = Column(String(50), index=True)
    is_military = Column(Boolean, default=False, index=True)
    is_interesting = Column(Boolean, default=False, index=True)

    # Raw data storage
    raw_data = Column(JSON)

    # Index definitions for query optimization
    __table_args__ = (
        Index("idx_snapshot_time", "snapshot_time"),
        Index("idx_location", "latitude", "longitude"),
        Index("idx_recent_military", "snapshot_time", "is_military"),
        Index("idx_recent_interesting", "snapshot_time", "is_interesting"),
        Index("idx_hex_time", "hex", "snapshot_time"),
    )

    def to_dict(self) -> dict:
        """Convert snapshot to dictionary format.

        Returns a dictionary with keys matching the API response format
        for compatibility with existing code.

        Returns:
            Dictionary representation of the snapshot
        """
        return {
            "id": self.id,
            "snapshot_time": self.snapshot_time.isoformat() if self.snapshot_time else None,
            "hex": self.hex,
            "flight": self.flight_number,
            "r": self.registration,
            "t": self.aircraft_type,
            "lat": float(self.latitude) if self.latitude else None,
            "lon": float(self.longitude) if self.longitude else None,
            "alt_baro": self.altitude_baro,
            "alt_geom": self.altitude_geom,
            "gs": float(self.ground_speed) if self.ground_speed else None,
            "track": float(self.track) if self.track else None,
            "baro_rate": self.vertical_rate,
            "squawk": self.squawk,
            "emergency": self.emergency,
            "category": self.category,
            "country_of_registration": self.country_of_registration,
            "current_country": self.current_country,
            "is_military": self.is_military,
            "is_interesting": self.is_interesting,
            "raw_data": self.raw_data,
        }


class AircraftStaticInfo(Base):
    """Static aircraft information cache table.

    Caches static information about aircraft that doesn't change frequently,
    reducing the need for repeated API calls or web searches.

    Attributes:
        id: Auto-incrementing primary key
        registration: Aircraft registration (unique identifier)
        hex_code: ICAO 24-bit hex code
        aircraft_type: ICAO aircraft type code
        owner: Aircraft owner/operator name
        operator: Operating airline/organization
        manufacturer: Aircraft manufacturer
        model: Aircraft model name
        serial_number: Manufacturer serial number
        year_built: Year aircraft was manufactured
        country_of_registration: Country where registered
        ai_analysis: Cached AI analysis result
        last_updated: When this record was last updated
        data_source: Source of the information
    """

    __tablename__ = "aircraft_static_info"

    id = Column(Integer, primary_key=True, autoincrement=True)
    registration = Column(String(20), unique=True, nullable=False, index=True)
    hex_code = Column(String(6), index=True)
    aircraft_type = Column(String(10))
    owner = Column(String(200))
    operator = Column(String(200))
    manufacturer = Column(String(100))
    model = Column(String(100))
    serial_number = Column(String(50))
    year_built = Column(Integer)
    country_of_registration = Column(String(50))
    ai_analysis = Column(Text)

    images_downloaded = Column(Boolean, default=False, index=True)
    images_updated_at = Column(DateTime)

    last_updated = Column(DateTime, default=func.now(), onupdate=func.now())
    data_source = Column(String(50))

    # Source-prefixed fields: airport_data.com (ad_)
    ad_status = Column(String(50))  # Active, De-registered, Stored, etc.
    ad_owner = Column(String(255))
    ad_engines = Column(Integer)
    ad_seats = Column(Integer)
    ad_location = Column(String(255))
    ad_delivery_date = Column(String(50))
    ad_updated_at = Column(DateTime)

    # Source-prefixed fields: planespotters.net (ps_)
    ps_status = Column(String(50))
    ps_airline = Column(String(255))
    ps_first_flight = Column(DateTime)
    ps_delivery_date = Column(DateTime)
    ps_updated_at = Column(DateTime)

    # Source-prefixed fields: jetphotos.com (jp_)
    jp_airline = Column(String(255))
    jp_cn = Column(String(50))  # Serial number from JetPhotos
    jp_updated_at = Column(DateTime)

    # Relationship to AircraftImage
    images = relationship(
        "AircraftImage",
        back_populates="aircraft",
        cascade="all, delete-orphan",
        order_by="AircraftImage.display_order",
    )

    __table_args__ = (
        Index("idx_static_hex", "hex_code"),
        Index("idx_static_updated", "last_updated"),
        Index("idx_static_images_downloaded", "images_downloaded"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "registration": self.registration,
            "hex_code": self.hex_code,
            "aircraft_type": self.aircraft_type,
            "owner": self.owner,
            "operator": self.operator,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "serial_number": self.serial_number,
            "year_built": self.year_built,
            "country_of_registration": self.country_of_registration,
            "ai_analysis": self.ai_analysis,
            "images_downloaded": self.images_downloaded,
            "images_updated_at": self.images_updated_at.isoformat()
            if self.images_updated_at
            else None,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "data_source": self.data_source,
            # Effective fields (merged from multiple sources)
            "effective_status": self.effective_status,
            "effective_owner": self.effective_owner,
            "effective_serial": self.effective_serial,
            "effective_delivery_date": self.effective_delivery_date,
            "effective_seats": self.effective_seats,
            "effective_engines": self.effective_engines,
        }

    def get_image_paths(self) -> list[str]:
        """Get all image paths from the related AircraftImage records.

        Returns:
            List of image paths ordered by display_order.
        """
        return [img.image_path for img in self.images if img.image_path]

    # =========================================================================
    # Effective properties (merged from multiple data sources)
    # =========================================================================
    # Current strategy: Use airport_data (ad_*) as primary source
    # Future: May merge from planespotters (ps_*) and jetphotos (jp_*)

    @property
    def effective_status(self) -> str | None:
        """Aircraft status (from airport_data).

        Returns:
            Status string like 'Active', 'De-registered', 'Stored', etc.
        """
        return self.ad_status

    @property
    def effective_owner(self) -> str | None:
        """Aircraft owner (prioritizes ad_owner, falls back to owner).

        Returns:
            Owner/operator name.
        """
        return self.ad_owner or self.owner

    @property
    def effective_serial(self) -> str | None:
        """Aircraft serial number (manufacturer's construction number).

        Returns:
            Serial/CN number.
        """
        return self.serial_number

    @property
    def effective_delivery_date(self) -> str | None:
        """Aircraft delivery date (from airport_data).

        Returns:
            Delivery date string.
        """
        return self.ad_delivery_date

    @property
    def effective_seats(self) -> int | None:
        """Number of seats (from airport_data).

        Returns:
            Seat count.
        """
        return self.ad_seats

    @property
    def effective_engines(self) -> int | None:
        """Number of engines (from airport_data).

        Returns:
            Engine count.
        """
        return self.ad_engines


class AircraftImage(Base):
    """Aircraft image metadata table.

    Stores detailed metadata for each aircraft image, including photographer
    info, capture location, dates, and display ordering.

    Attributes:
        id: Auto-incrementing primary key
        registration: Aircraft registration (for quick lookups)
        aircraft_id: Foreign key to aircraft_static_info.id
        image_path: Path to image file (local or S3)
        source_url: Original photo page URL
        source: Image source (e.g., 'jetphotos')
        photographer: Photographer name
        photo_date: Date photo was taken
        upload_date: Date photo was uploaded to source
        location: Location where photo was taken
        airport_icao: Airport ICAO code
        airport_name: Airport name
        notes: Photo notes/remarks
        display_order: Order for displaying images (1 = first)
        is_primary: Whether this is the primary/main image
        width: Image width in pixels
        height: Image height in pixels
        file_size_bytes: File size in bytes
        jetphotos_id: JetPhotos photo ID (for deduplication)
        created_at: Record creation timestamp
        updated_at: Record update timestamp
    """

    __tablename__ = "aircraft_images"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    registration = Column(String(20), nullable=False, index=True)
    aircraft_id = Column(
        Integer, ForeignKey("aircraft_static_info.id", ondelete="SET NULL"), index=True
    )

    # Image storage info
    image_path = Column(String(500), nullable=False)
    source_url = Column(String(500))
    source = Column(String(50), default="jetphotos")

    # Photo metadata
    photographer = Column(String(200))
    photo_date = Column(Date)
    upload_date = Column(Date)
    location = Column(String(200))
    airport_icao = Column(String(4))
    airport_name = Column(String(200))
    notes = Column(Text)

    # Display ordering
    display_order = Column(Integer, default=1)
    is_primary = Column(Boolean, default=False)

    # Image properties
    width = Column(Integer)
    height = Column(Integer)
    file_size_bytes = Column(Integer)

    # Source-specific IDs (for deduplication)
    jetphotos_id = Column(String(20), unique=True)

    # Timestamps
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationship to AircraftStaticInfo
    aircraft = relationship("AircraftStaticInfo", back_populates="images")

    __table_args__ = (
        Index("idx_image_registration", "registration"),
        Index("idx_image_aircraft_id", "aircraft_id"),
        Index("idx_images_reg_order", "registration", "display_order"),
        Index("idx_image_source", "source"),
        Index("idx_image_photographer", "photographer"),
        Index("idx_image_photo_date", "photo_date"),
        Index("idx_image_airport", "airport_icao"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "registration": self.registration,
            "aircraft_id": self.aircraft_id,
            "image_path": self.image_path,
            "source_url": self.source_url,
            "source": self.source,
            "photographer": self.photographer,
            "photo_date": self.photo_date.isoformat() if self.photo_date else None,
            "upload_date": self.upload_date.isoformat() if self.upload_date else None,
            "location": self.location,
            "airport_icao": self.airport_icao,
            "airport_name": self.airport_name,
            "notes": self.notes,
            "display_order": self.display_order,
            "is_primary": self.is_primary,
            "width": self.width,
            "height": self.height,
            "file_size_bytes": self.file_size_bytes,
            "jetphotos_id": self.jetphotos_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Airport(Base):
    """Airport information table.

    Stores static information about airports worldwide.
    Data can be imported from OurAirports dataset.

    Attributes:
        id: Auto-incrementing primary key
        icao_code: ICAO airport code (e.g., 'ZBAA')
        iata_code: IATA airport code (e.g., 'PEK')
        name: Full airport name
        name_en: English name (if different)
        city: City name
        country: Country name
        country_code: ISO country code
        latitude: Airport latitude
        longitude: Airport longitude
        elevation_ft: Elevation in feet
        timezone: Timezone identifier (e.g., 'Asia/Shanghai')
        airport_type: Type of airport (large_airport, medium_airport, small_airport)
        created_at: Record creation timestamp
        updated_at: Record update timestamp
    """

    __tablename__ = "airports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    icao_code = Column(String(4), unique=True, nullable=False, index=True)
    iata_code = Column(String(3), index=True)
    name = Column(String(200), nullable=False)
    name_en = Column(String(200))
    city = Column(String(100))
    country = Column(String(100))
    country_code = Column(String(3))
    latitude = Column(Numeric(10, 7), nullable=False)
    longitude = Column(Numeric(11, 7), nullable=False)
    elevation_ft = Column(Integer)
    timezone = Column(String(50))
    airport_type = Column(String(30))
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_airport_iata", "iata_code"),
        Index("idx_airport_location", "latitude", "longitude"),
        Index("idx_airport_country", "country_code"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "icao_code": self.icao_code,
            "iata_code": self.iata_code,
            "name": self.name,
            "name_en": self.name_en,
            "city": self.city,
            "country": self.country,
            "country_code": self.country_code,
            "latitude": float(self.latitude) if self.latitude else None,
            "longitude": float(self.longitude) if self.longitude else None,
            "elevation_ft": self.elevation_ft,
            "timezone": self.timezone,
            "airport_type": self.airport_type,
        }


class FlightSchedule(Base):
    """Flight schedule table for airport arrivals/departures.

    Stores scheduled flight information scraped from FR24 and other sources.
    Used for tracking expected aircraft arrivals and departures at airports.

    Attributes:
        id: Auto-incrementing primary key
        flight_type: Type of flight ('arrival' or 'departure')
        airport_icao: ICAO code of the airport (4-letter)
        airport_iata: IATA code of the airport (3-letter)
        flight_number: Flight number (e.g., 'MU2851')
        callsign: Aircraft callsign
        fr24_flight_id: FR24 unique flight identifier
        airline_name: Full airline name
        airline_iata: Airline IATA code (2-letter)
        remote_airport_iata: Origin (for arrivals) or destination (for departures) IATA code
        remote_airport_name: Origin or destination airport name
        aircraft_type: ICAO aircraft type code (e.g., 'A20N')
        aircraft_registration: Aircraft registration (e.g., 'B-320H')
        scheduled_time: Scheduled arrival/departure time
        estimated_time: Estimated arrival/departure time
        actual_time: Actual arrival/departure time
        status: Flight status (Scheduled, En Route, Landed, Delayed, Cancelled, etc.)
        terminal: Terminal number/letter
        gate: Gate number
        scraped_at: When this data was last scraped
    """

    __tablename__ = "flight_schedules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    flight_type = Column(String(20), nullable=False, index=True)  # arrival, departure
    airport_icao = Column(String(4), index=True)
    airport_iata = Column(String(3), index=True)
    flight_number = Column(String(20), index=True)
    callsign = Column(String(20))
    fr24_flight_id = Column(String(50), nullable=False)
    airline_name = Column(String(200))
    airline_iata = Column(String(3))
    remote_airport_iata = Column(String(3), index=True)
    remote_airport_name = Column(String(200))
    aircraft_type = Column(String(10))
    aircraft_registration = Column(String(20), index=True)
    scheduled_time = Column(DateTime, nullable=False, index=True)
    estimated_time = Column(DateTime)
    actual_time = Column(DateTime)
    status = Column(String(30))
    terminal = Column(String(10))
    gate = Column(String(10))
    scraped_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index("idx_flight_schedule_airport", "airport_iata", "scheduled_time"),
        Index("idx_flight_schedule_airport_icao", "airport_icao", "scheduled_time"),
        Index("idx_flight_schedule_registration", "aircraft_registration", "scheduled_time"),
        Index("idx_flight_schedule_fr24", "fr24_flight_id", "scheduled_time", unique=True),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "flight_type": self.flight_type,
            "airport_icao": self.airport_icao,
            "airport_iata": self.airport_iata,
            "flight_number": self.flight_number,
            "callsign": self.callsign,
            "fr24_flight_id": self.fr24_flight_id,
            "airline_name": self.airline_name,
            "airline_iata": self.airline_iata,
            "remote_airport_iata": self.remote_airport_iata,
            "remote_airport_name": self.remote_airport_name,
            "aircraft_type": self.aircraft_type,
            "aircraft_registration": self.aircraft_registration,
            "scheduled_time": self.scheduled_time.isoformat() if self.scheduled_time else None,
            "estimated_time": self.estimated_time.isoformat() if self.estimated_time else None,
            "actual_time": self.actual_time.isoformat() if self.actual_time else None,
            "status": self.status,
            "terminal": self.terminal,
            "gate": self.gate,
            "scraped_at": self.scraped_at.isoformat() if self.scraped_at else None,
        }


# =============================================================================
# Geographic and Report Models (migrated from database.py)
# =============================================================================


class GeographicRegion(Base):
    """Geographic region definition table.

    Stores geographic region definitions for filtering aircraft
    by location (countries, areas, airspaces, zones).

    Attributes:
        id: Auto-incrementing primary key
        name: Region name
        region_type: Type of region (COUNTRY, AREA, AIRSPACE, ZONE)
        geometry_type: Type of geometry (POINT, CIRCLE, POLYGON, COUNTRY_BOUNDARY)
        center_lat: Center latitude for circular regions
        center_lon: Center longitude for circular regions
        radius_km: Radius in kilometers for circular regions
        boundary_points: JSON array of polygon boundary points
        country_code: ISO country code for country regions
        created_at: Record creation timestamp
    """

    __tablename__ = "geographic_regions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, index=True)
    region_type = Column(String(20), nullable=False, index=True)  # COUNTRY, AREA, AIRSPACE, ZONE

    # Geometry definition
    geometry_type = Column(String(20))  # POINT, CIRCLE, POLYGON, COUNTRY_BOUNDARY
    center_lat = Column(Numeric(10, 7))
    center_lon = Column(Numeric(11, 7))
    radius_km = Column(Numeric(8, 3))
    boundary_points = Column(JSON)  # Polygon boundary points
    country_code = Column(String(3))  # ISO country code

    created_at = Column(DateTime, default=func.now())

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "name": self.name,
            "region_type": self.region_type,
            "geometry_type": self.geometry_type,
            "center_lat": float(self.center_lat) if self.center_lat else None,
            "center_lon": float(self.center_lon) if self.center_lon else None,
            "radius_km": float(self.radius_km) if self.radius_km else None,
            "boundary_points": self.boundary_points,
            "country_code": self.country_code,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ReportCooldown(Base):
    """Persistent cooldown tracking for cross-process coordination.

    Tracks report cooldowns per aircraft to prevent duplicate reports
    across multiple processes/workers.

    Attributes:
        id: Auto-incrementing primary key
        aircraft_hex: ICAO hex code of the aircraft
        last_report_time: When the last report was sent
        last_latitude: Latitude when last report was sent
        last_longitude: Longitude when last report was sent
        report_count: Total reports sent for this aircraft
        updated_at: Last update timestamp
    """

    __tablename__ = "report_cooldowns"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    aircraft_hex = Column(String(6), nullable=False, unique=True, index=True)
    last_report_time = Column(DateTime, nullable=False)
    last_latitude = Column(Numeric(10, 7))
    last_longitude = Column(Numeric(11, 7))
    report_count = Column(Integer, default=1)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_cooldowns_time", "last_report_time"),)

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "aircraft_hex": self.aircraft_hex,
            "last_report_time": self.last_report_time.isoformat()
            if self.last_report_time
            else None,
            "last_latitude": float(self.last_latitude) if self.last_latitude else None,
            "last_longitude": float(self.last_longitude) if self.last_longitude else None,
            "report_count": self.report_count,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Social Media Note Analysis Models
# =============================================================================


class NoteAircraftAnalysis(Base):
    """LLM analysis result for social media notes.

    Stores aircraft registration extraction and attention analysis
    results from Xiaohongshu, Weibo, Douyin and other social platforms.

    Attributes:
        id: Auto-incrementing primary key
        note_id: Source note ID (from xiaohongshu_notes, etc.)
        source_type: Platform source (xiaohongshu, weibo, douyin)
        registrations: List of extracted registration numbers (JSONB)
        registration_details: Detailed extraction info with confidence scores (JSONB)
        attention_index: Calculated attention score (0-100)
        attention_level: Categorized level (high, medium, low)
        attention_reason: LLM explanation for attention score
        content_type: Note content category (spotting, news, accident, rumor, fan)
        sentiment: Overall sentiment (positive, negative, neutral)
        topics: Extracted topics/themes (JSONB)
        llm_model: Model used for analysis
        input_tokens: Token count for input
        output_tokens: Token count for output
        analyzed_at: When analysis was performed
        created_at: Record creation timestamp
    """

    __tablename__ = "note_aircraft_analysis"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    note_id = Column(String(50), nullable=False, unique=True, index=True)
    source_type = Column(String(20), nullable=False, index=True)  # xiaohongshu, weibo, douyin

    # Extracted aircraft registrations
    registrations = Column(JSON)  # ["B-1234", "N12345"]
    registration_details = Column(JSON)  # [{registration, confidence, context}]

    # Attention scoring
    attention_index = Column(Integer)  # 0-100
    attention_level = Column(String(10), index=True)  # high, medium, low
    attention_reason = Column(Text)

    # Content analysis
    content_type = Column(String(20), index=True)  # spotting, news, accident, rumor, fan
    sentiment = Column(String(10))  # positive, negative, neutral
    topics = Column(JSON)  # ["livery", "private jet", "celebrity"]

    # LLM metadata
    llm_model = Column(String(100))
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    raw_response = Column(Text)

    # Timestamps
    analyzed_at = Column(DateTime, default=func.now())
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_note_analysis_source", "source_type", "analyzed_at"),
        Index("idx_note_analysis_attention", "attention_level", "attention_index"),
        Index("idx_note_analysis_content_type", "content_type"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "note_id": self.note_id,
            "source_type": self.source_type,
            "registrations": self.registrations,
            "registration_details": self.registration_details,
            "attention_index": self.attention_index,
            "attention_level": self.attention_level,
            "attention_reason": self.attention_reason,
            "content_type": self.content_type,
            "sentiment": self.sentiment,
            "topics": self.topics,
            "llm_model": self.llm_model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "analyzed_at": self.analyzed_at.isoformat() if self.analyzed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AircraftAttentionAggregate(Base):
    """Aggregated attention metrics per aircraft registration.

    Consolidates attention data across all analyzed notes for each aircraft,
    enabling trend analysis and hotspot identification.

    Attributes:
        id: Auto-incrementing primary key
        registration: Aircraft registration number (unique)
        total_mentions: Total times mentioned across all notes
        avg_attention_index: Average attention index across mentions
        max_attention_index: Highest attention index received
        mentions_7d: Mentions in last 7 days
        mentions_30d: Mentions in last 30 days
        first_seen: When first mentioned
        last_seen: Most recent mention
        top_topics: Most common topics associated (JSONB)
        sentiment_distribution: Sentiment breakdown (JSONB)
        source_distribution: Platform breakdown (JSONB)
        trending_score: Calculated trend score based on recent activity
        updated_at: Last aggregation update
    """

    __tablename__ = "aircraft_attention_aggregate"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    registration = Column(String(20), nullable=False, unique=True, index=True)

    # Mention counts
    total_mentions = Column(Integer, default=0)
    avg_attention_index = Column(Numeric(5, 2))
    max_attention_index = Column(Integer)

    # Time-based trends
    mentions_7d = Column(Integer, default=0)
    mentions_30d = Column(Integer, default=0)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)

    # Aggregated analysis
    top_topics = Column(JSON)  # [{"topic": "livery", "count": 5}, ...]
    sentiment_distribution = Column(JSON)  # {"positive": 10, "neutral": 5, "negative": 2}
    source_distribution = Column(JSON)  # {"xiaohongshu": 15, "weibo": 2}
    content_type_distribution = Column(JSON)  # {"spotting": 10, "news": 5}

    # Trend analysis
    trending_score = Column(Numeric(5, 2))  # Weighted recent activity score

    # Timestamps
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_attention_agg_mentions", "total_mentions"),
        Index("idx_attention_agg_trending", "trending_score"),
        Index("idx_attention_agg_last_seen", "last_seen"),
    )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "registration": self.registration,
            "total_mentions": self.total_mentions,
            "avg_attention_index": float(self.avg_attention_index)
            if self.avg_attention_index
            else None,
            "max_attention_index": self.max_attention_index,
            "mentions_7d": self.mentions_7d,
            "mentions_30d": self.mentions_30d,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "top_topics": self.top_topics,
            "sentiment_distribution": self.sentiment_distribution,
            "source_distribution": self.source_distribution,
            "content_type_distribution": self.content_type_distribution,
            "trending_score": float(self.trending_score) if self.trending_score else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
