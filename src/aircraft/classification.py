"""
Aircraft Classification Constants and Logic
Centralizes aircraft type detection, classification, and display formatting.
"""


class AircraftClassification:
    """Centralized aircraft classification constants and methods"""

    # Heavy cargo aircraft types
    HEAVY_CARGO_TYPES: tuple[str, ...] = ("B742", "B744", "IL76", "AN124", "AN225")

    # Military aircraft types
    MILITARY_TYPES: tuple[str, ...] = (
        "C17",
        "C130",
        "KC135",
        "KC46",
        "B52",
        "F16",
        "F18",
        "F22",
        "F35",
    )

    # Military registration prefixes (US military format)
    MILITARY_PREFIXES: tuple[str, ...] = (
        "86-",
        "87-",
        "91-",
        "92-",
        "01-",
        "02-",
        "03-",
        "04-",
        "05-",
    )

    # Government aircraft registration prefixes
    GOVERNMENT_PREFIXES: tuple[str, ...] = ("N1",)

    # VIP aircraft registration prefixes
    VIP_PREFIXES: tuple[str, ...] = ("VP-",)

    # Priority indicator emojis
    EMOJI_MILITARY = "🚁"
    EMOJI_HEAVY_CARGO = "✈️"
    EMOJI_GOVERNMENT = "🏛️"

    # Priority labels
    LABEL_MILITARY = "MILITARY"
    LABEL_HEAVY_CARGO = "HEAVY CARGO"
    LABEL_GOVERNMENT = "GOVERNMENT"

    @classmethod
    def is_military_by_registration(cls, registration: str) -> bool:
        """Check if registration indicates military aircraft"""
        if not registration:
            return False
        reg = registration.strip()
        return any(reg.startswith(prefix) for prefix in cls.MILITARY_PREFIXES)

    @classmethod
    def is_military_by_type(cls, aircraft_type: str) -> bool:
        """Check if aircraft type is military"""
        if not aircraft_type:
            return False
        return aircraft_type.strip() in cls.MILITARY_TYPES

    @classmethod
    def is_military(cls, aircraft_data: dict) -> bool:
        """Check if aircraft is military based on all available data"""
        # Explicit military flag
        if aircraft_data.get("is_military"):
            return True

        # Check dbFlags (bit 0 indicates military)
        db_flags = aircraft_data.get("dbFlags")
        if db_flags and (db_flags & 1):
            return True

        # Check registration
        registration = (aircraft_data.get("r") or "").strip()
        if cls.is_military_by_registration(registration):
            return True

        # Check type
        aircraft_type = (aircraft_data.get("t") or "").strip()
        if cls.is_military_by_type(aircraft_type):
            return True

        return False

    @classmethod
    def is_heavy_cargo(cls, aircraft_type: str) -> bool:
        """Check if aircraft type is heavy cargo"""
        if not aircraft_type:
            return False
        return aircraft_type.strip() in cls.HEAVY_CARGO_TYPES

    @classmethod
    def is_government(cls, registration: str) -> bool:
        """Check if registration indicates government aircraft"""
        if not registration:
            return False
        reg = registration.strip()
        return any(reg.startswith(prefix) for prefix in cls.GOVERNMENT_PREFIXES)

    @classmethod
    def is_vip(cls, registration: str) -> bool:
        """Check if registration indicates VIP aircraft"""
        if not registration:
            return False
        reg = registration.strip()
        return any(reg.startswith(prefix) for prefix in cls.VIP_PREFIXES)

    @classmethod
    def is_interesting(cls, aircraft_data: dict) -> bool:
        """Check if aircraft is interesting (military, heavy cargo, government, or VIP)"""
        if cls.is_military(aircraft_data):
            return True

        aircraft_type = (aircraft_data.get("t") or "").strip()
        if cls.is_heavy_cargo(aircraft_type):
            return True

        registration = (aircraft_data.get("r") or "").strip()
        if cls.is_government(registration) or cls.is_vip(registration):
            return True

        return False

    @classmethod
    def get_classification(cls, aircraft_data: dict) -> str:
        """Get classification category for aircraft

        Returns: 'military', 'heavy_cargo', 'government', 'vip', or 'standard'
        """
        if cls.is_military(aircraft_data):
            return "military"

        aircraft_type = (aircraft_data.get("t") or "").strip()
        if cls.is_heavy_cargo(aircraft_type):
            return "heavy_cargo"

        registration = (aircraft_data.get("r") or "").strip()
        if cls.is_government(registration):
            return "government"
        if cls.is_vip(registration):
            return "vip"

        return "standard"

    @classmethod
    def get_priority_indicator(cls, aircraft_data: dict) -> str | None:
        """Get emoji and label for priority aircraft

        Returns: String like "🚁 MILITARY" or None for standard aircraft
        """
        classification = cls.get_classification(aircraft_data)

        if classification == "military":
            return f"{cls.EMOJI_MILITARY} {cls.LABEL_MILITARY}"
        elif classification == "heavy_cargo":
            return f"{cls.EMOJI_HEAVY_CARGO} {cls.LABEL_HEAVY_CARGO}"
        elif classification == "government":
            return f"{cls.EMOJI_GOVERNMENT} {cls.LABEL_GOVERNMENT}"

        return None
