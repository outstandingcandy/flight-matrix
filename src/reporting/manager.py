import logging
import time
from datetime import datetime

from src.geo.geo import haversine_distance

logger = logging.getLogger("report_manager")


class ReportManager:
    """
    Manages aircraft report generation cooldown and frequency control.
    """

    def __init__(self, cooldown_hours: float = 1.0, min_move_distance_km: float = 1.0):
        """
        Initialize the report manager.

        Args:
            cooldown_hours: Cooldown period between reports in hours (default 1 hour)
            min_move_distance_km: Minimum movement distance in km before a new report is generated
        """
        self.cooldown_seconds = cooldown_hours * 3600
        self.min_move_distance_km = min_move_distance_km
        self.last_report_times: dict[str, float] = {}
        self.last_report_positions: dict[str, tuple[float, float]] = {}  # {aircraft_id: (lat, lon)}
        logger.info(
            f"Report manager initialized with {cooldown_hours}h cooldown, {min_move_distance_km}km min move distance"
        )

    def should_generate_report(
        self, aircraft_id: str, lat: float | None = None, lon: float | None = None
    ) -> bool:
        """
        Check whether a report should be generated for the given aircraft.

        Args:
            aircraft_id: Unique aircraft identifier (usually the hex code)
            lat: Current latitude (optional, used to check movement)
            lon: Current longitude (optional, used to check movement)

        Returns:
            True if should generate report, False if still in cooldown or hasn't moved
        """
        current_time = time.time()

        # Check whether there is a record for this aircraft
        if aircraft_id not in self.last_report_times:
            # First time seeing this aircraft; a report is allowed
            logger.info(f"First detection of aircraft {aircraft_id}, report allowed")
            return True

        # Check cooldown
        last_report_time = self.last_report_times[aircraft_id]
        time_since_last = current_time - last_report_time

        if time_since_last < self.cooldown_seconds:
            # Still in cooldown
            remaining_time = self.cooldown_seconds - time_since_last
            logger.debug(
                f"Aircraft {aircraft_id} still in cooldown, {remaining_time / 3600:.1f}h remaining"
            )
            return False

        # Cooldown has elapsed; check whether the aircraft has moved
        if lat is not None and lon is not None:
            if aircraft_id in self.last_report_positions:
                # Previous position recorded; check movement distance
                last_lat, last_lon = self.last_report_positions[aircraft_id]
                distance = haversine_distance(last_lat, last_lon, lat, lon)

                if distance < self.min_move_distance_km:
                    logger.debug(
                        f"Aircraft {aircraft_id} hasn't moved significantly ({distance:.2f}km < {self.min_move_distance_km}km), report skipped"
                    )
                    return False
                else:
                    logger.info(
                        f"Aircraft {aircraft_id} moved {distance:.2f}km since last report, report allowed"
                    )
                    return True
            else:
                # Cooldown has elapsed but no previous position recorded (position may be missing on first report)
                # Save the current position but do not generate a report (wait for next movement)
                self.last_report_positions[aircraft_id] = (lat, lon)
                logger.debug(
                    f"Aircraft {aircraft_id} cooldown expired but no previous position recorded. Position saved, report skipped until movement detected."
                )
                return False

        # Cooldown has elapsed, but no current position info; skip report
        logger.debug(
            f"Aircraft {aircraft_id} cooldown expired but no position available, report skipped"
        )
        return False

    def mark_report_generated(
        self, aircraft_id: str, lat: float | None = None, lon: float | None = None
    ):
        """
        Mark that a report has been generated for the given aircraft.

        Args:
            aircraft_id: Unique aircraft identifier
            lat: Current latitude (optional, used to record position)
            lon: Current longitude (optional, used to record position)
        """
        current_time = time.time()
        self.last_report_times[aircraft_id] = current_time

        # Record position
        if lat is not None and lon is not None:
            self.last_report_positions[aircraft_id] = (lat, lon)

        timestamp = datetime.fromtimestamp(current_time).isoformat()
        pos_info = f" at position ({lat:.4f}, {lon:.4f})" if lat and lon else ""
        logger.info(f"Marked report generated for aircraft {aircraft_id} at {timestamp}{pos_info}")

    def cleanup_old_records(self, max_age_hours: float = 24.0):
        """
        Clean up records older than the given age to save memory.

        Args:
            max_age_hours: Maximum record retention time in hours (default 24 hours)
        """
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600

        aircraft_to_remove = []
        for aircraft_id, last_time in self.last_report_times.items():
            if current_time - last_time > max_age_seconds:
                aircraft_to_remove.append(aircraft_id)

        for aircraft_id in aircraft_to_remove:
            del self.last_report_times[aircraft_id]
            # Also clean up position records
            if aircraft_id in self.last_report_positions:
                del self.last_report_positions[aircraft_id]
            logger.debug(f"Removed old record for aircraft {aircraft_id}")

        if aircraft_to_remove:
            logger.info(f"Cleaned up {len(aircraft_to_remove)} old aircraft records")

    def get_stats(self) -> dict:
        """
        Get report manager statistics.

        Returns:
            Dictionary of statistics
        """
        current_time = time.time()
        active_cooldowns = 0

        for last_time in self.last_report_times.values():
            if current_time - last_time < self.cooldown_seconds:
                active_cooldowns += 1

        return {
            "total_tracked_aircraft": len(self.last_report_times),
            "active_cooldowns": active_cooldowns,
            "cooldown_period_hours": self.cooldown_seconds / 3600,
            "oldest_record_hours": (current_time - min(self.last_report_times.values())) / 3600
            if self.last_report_times
            else 0,
        }

    def get_aircraft_status(
        self, aircraft_id: str, lat: float | None = None, lon: float | None = None
    ) -> dict:
        """
        Get the report status for a specific aircraft.

        Args:
            aircraft_id: Aircraft identifier
            lat: Current latitude (optional, used to compute movement distance)
            lon: Current longitude (optional, used to compute movement distance)

        Returns:
            Dictionary of status information
        """
        if aircraft_id not in self.last_report_times:
            return {
                "aircraft_id": aircraft_id,
                "has_previous_report": False,
                "can_generate_report": True,
                "last_report_time": None,
                "time_since_last_report_hours": None,
                "remaining_cooldown_hours": 0,
                "last_position": None,
                "distance_moved_km": None,
            }

        current_time = time.time()
        last_time = self.last_report_times[aircraft_id]
        time_since_last = current_time - last_time
        remaining_cooldown = max(0, self.cooldown_seconds - time_since_last)

        # Compute movement distance
        last_position = self.last_report_positions.get(aircraft_id)
        distance_moved = None
        if last_position and lat is not None and lon is not None:
            distance_moved = haversine_distance(last_position[0], last_position[1], lat, lon)

        # Determine whether a report can be generated
        can_generate = False
        if time_since_last >= self.cooldown_seconds:
            # Cooldown has elapsed
            if distance_moved is not None:
                # Movement distance available; check whether it's sufficient
                can_generate = distance_moved >= self.min_move_distance_km
            elif last_position is None and lat is not None and lon is not None:
                # No previous position recorded but we have one now; do not allow report (record position first)
                can_generate = False
            else:
                # No position information; do not allow report
                can_generate = False

        return {
            "aircraft_id": aircraft_id,
            "has_previous_report": True,
            "can_generate_report": can_generate,
            "last_report_time": datetime.fromtimestamp(last_time).isoformat(),
            "time_since_last_report_hours": time_since_last / 3600,
            "remaining_cooldown_hours": remaining_cooldown / 3600,
            "last_position": last_position,
            "distance_moved_km": distance_moved,
        }
