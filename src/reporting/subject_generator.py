"""
Report Subject Generator
Generates email subject lines for aircraft detection reports.
"""

from src.aircraft.classification import AircraftClassification


class ReportSubjectGenerator:
    """Generates formatted subject lines for aircraft reports"""

    SUFFIX = "Aircraft Detected"

    def __init__(self):
        self.classification = AircraftClassification

    def generate(self, aircraft: dict) -> str:
        """Generate report subject line for aircraft

        Args:
            aircraft: Aircraft data dictionary

        Returns:
            Formatted subject string
        """
        parts = []

        # Priority indicator (emoji + label)
        priority = self._build_priority_prefix(aircraft)
        if priority:
            parts.append(priority)

        # Main identifier
        identifier = self._build_identifier(aircraft)
        parts.append(identifier)

        # Aircraft type
        type_suffix = self._build_type_suffix(aircraft)
        if type_suffix:
            parts.append(type_suffix)

        # Altitude
        altitude_suffix = self._build_altitude_suffix(aircraft.get("alt_baro"))
        if altitude_suffix:
            parts.append(altitude_suffix)

        return " ".join(parts) + f" - {self.SUFFIX}"

    def _build_priority_prefix(self, aircraft: dict) -> str | None:
        """Build priority emoji and label prefix"""
        return self.classification.get_priority_indicator(aircraft)

    def _build_identifier(self, aircraft: dict) -> str:
        """Build primary aircraft identifier"""
        flight = (aircraft.get("flight") or "").strip()
        registration = (aircraft.get("r") or "").strip()
        hex_code = aircraft.get("hex", "")

        if flight:
            return f"Flight {flight}"
        elif registration:
            return f"Aircraft {registration}"
        else:
            return f"Aircraft {hex_code}"

    def _build_type_suffix(self, aircraft: dict) -> str | None:
        """Build aircraft type suffix"""
        aircraft_type = (aircraft.get("t") or "").strip()
        if aircraft_type and aircraft_type != "Unknown":
            return f"({aircraft_type})"
        return None

    def _build_altitude_suffix(self, altitude: int | None) -> str | None:
        """Build altitude display string

        Args:
            altitude: Barometric altitude in feet

        Returns:
            Formatted altitude string or None
        """
        if not altitude:
            return None
        if altitude == "ground":
            return "on ground"
        if altitude <= 0:
            return None
        if altitude >= 30000:
            # Flight level format for high altitude
            return f"at FL{altitude // 100}"
        else:
            # Standard format with comma separator
            return f"at {altitude:,}ft"
