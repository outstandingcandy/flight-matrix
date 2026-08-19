"""
Task Scheduler - Manages task lifecycle for all scraper types.

Features:
- Recreates completed tasks based on configured intervals
- Cleans up orphan tasks from terminated workers
- Supports all task types (fr24_arrivals, fr24_departures, jetphotos, etc.)

Usage:
    # Run as standalone scheduler
    python -m src.scraper.task_scheduler --config config/config.yaml

    # Or import and use programmatically
    from src.scraper.task_scheduler import TaskScheduler
    scheduler = TaskScheduler(config, database_url)
    scheduler.run()
"""

import argparse
import logging
import os
import re
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

# boto3 is imported lazily inside AWS-specific methods so that local
# (SQLite / STAGE=local) deployments can run the scheduler without any
# AWS credentials or IAM setup.
from sqlalchemy import create_engine, text

logger = logging.getLogger("scraper.task_scheduler")


class TaskScheduler:
    """Universal task scheduler for all scraper types.

    Manages the task lifecycle:
    - Monitors completed tasks and recreates them based on configured intervals
    - Detects and cleans up orphan tasks from terminated workers
    - Supports configurable task types and schedules

    Attributes:
        config: Configuration dictionary.
        database_url: PostgreSQL connection URL.
        check_interval: Seconds between scheduler checks.
        orphan_cleanup_threshold: Seconds without heartbeat before worker is considered dead.
    """

    def __init__(
        self,
        config: dict[str, Any],
        database_url: str,
        check_interval: int | None = None,
    ) -> None:
        """Initialize the task scheduler.

        Args:
            config: Configuration dictionary with scraper settings.
            database_url: PostgreSQL connection URL.
            check_interval: Seconds between scheduler checks (overrides config).
        """
        self.config = config
        self.database_url = database_url

        # Read scheduler config (required)
        scraper_config = config.get("scraper")
        if not scraper_config:
            raise ValueError("Missing required config section: scraper")

        scheduler_config = scraper_config.get("scheduler")
        if not scheduler_config:
            raise ValueError("Missing required config section: scraper.scheduler")

        worker_config = scraper_config.get("worker")
        if not worker_config:
            raise ValueError("Missing required config section: scraper.worker")

        # Required scheduler settings
        self.check_interval = check_interval or self._require_config(
            scheduler_config, "check_interval", "scraper.scheduler"
        )
        self.orphan_cleanup_threshold = self._require_config(
            scheduler_config, "orphan_cleanup_threshold", "scraper.scheduler"
        )
        # AWS-integration settings default to off so the scheduler boots on
        # a laptop / SQLite without any IAM setup. Prod YAML opts in.
        self.auto_terminate_unhealthy = bool(
            scheduler_config.get("auto_terminate_unhealthy", False)
        )

        auto_scale_config = scheduler_config.get("auto_scale") or {}
        self.auto_scale_enabled = bool(auto_scale_config.get("enabled", False))
        self.asg_name: str | None = auto_scale_config.get("asg_name") or None
        self.min_instances = int(auto_scale_config.get("min_instances", 0) or 0)
        self.max_instances = int(auto_scale_config.get("max_instances", 0) or 0)
        self.tasks_per_worker = int(auto_scale_config.get("tasks_per_worker", 50) or 50)
        self.scale_down_cooldown = int(auto_scale_config.get("scale_down_cooldown", 5) or 5)
        self._scale_down_counter = 0  # Track consecutive low-task cycles

        # Task timeout config (use worker's task_timeout)
        self.task_timeout = self._require_config(worker_config, "task_timeout", "scraper.worker")

        self.engine = create_engine(database_url, echo=False, pool_pre_ping=True)
        self._running = False

        self.is_postgres = self.engine.dialect.name == "postgresql"
        # Local = anything on SQLite, or STAGE=local. Used to short-circuit
        # any path that would call AWS APIs.
        self.is_local = (os.environ.get("STAGE", "").lower() == "local") or not self.is_postgres

        # Per-airport settings: {airport_code: {priority, min_cycle_gap}}
        self.airport_settings: dict[str, dict[str, int]] = {}

        # Retention pruning is hourly, not per cycle: check_interval is measured
        # in seconds and the horizons are measured in days, so running it on every
        # cycle would issue thousands of DELETEs a day to expire the same rows.
        self._last_prune: datetime | None = None

        # Build task type configurations
        self.task_types = self._build_task_configs()

        logger.info(
            f"TaskScheduler initialized: task_types={list(self.task_types.keys())}, "
            f"airport_settings={len(self.airport_settings)} airports, "
            f"auto_terminate={self.auto_terminate_unhealthy}, "
            f"task_timeout={self.task_timeout}s, "
            f"auto_scale={self.auto_scale_enabled}, "
            f"is_local={self.is_local} (dialect={self.engine.dialect.name})"
        )

    def _require_config(self, config: dict[str, Any], key: str, section: str) -> Any:
        """Get a required config value, raising error if missing.

        Args:
            config: Config dictionary to read from.
            key: Config key to read.
            section: Config section name for error message.

        Returns:
            The config value.

        Raises:
            ValueError: If the config key is missing.
        """
        if key not in config:
            raise ValueError(f"Missing required config: {section}.{key}")
        return config[key]

    def _build_task_configs(self) -> dict[str, dict[str, Any]]:
        """Build task configurations from config.yaml.

        Returns:
            Dictionary mapping task_type to its configuration.
        """
        scraper_config = self.config.get("scraper", {}).get("scrapers", {})
        scheduler_config = self.config.get("scraper", {}).get("scheduler", {})
        task_types = {}

        for task_type, type_config in scraper_config.items():
            if not type_config.get("enabled", False):
                continue

            # Base config for all task types
            config_entry = {
                "enabled": True,
                "min_cycle_gap": type_config.get(
                    "min_cycle_gap", scheduler_config.get("min_cycle_gap", 3600)
                ),
                "priority": type_config.get("priority", 0),
            }

            # FR24 specific: airport-based tasks
            if task_type in ["fr24_arrivals", "fr24_departures", "fr24_airport"]:
                # Check for new airport_groups config structure
                airport_groups = type_config.get("airport_groups", [])
                default_priority = type_config.get(
                    "default_priority", type_config.get("priority", 10)
                )
                default_gap = type_config.get(
                    "default_min_cycle_gap",
                    type_config.get("min_cycle_gap", scheduler_config.get("min_cycle_gap", 3600)),
                )

                all_airports = []

                if airport_groups:
                    # New config structure with airport groups
                    for group in airport_groups:
                        group_priority = group.get("priority", default_priority)
                        group_gap = group.get("min_cycle_gap", default_gap)
                        group_airports = [
                            a.upper().strip() for a in group.get("airports", []) if a and a.strip()
                        ]

                        for airport in group_airports:
                            self.airport_settings[airport] = {
                                "priority": group_priority,
                                "min_cycle_gap": group_gap,
                            }
                            all_airports.append(airport)

                        logger.info(
                            f"  {task_type} group '{group.get('name', 'unnamed')}': "
                            f"{len(group_airports)} airports, priority={group_priority}, "
                            f"min_cycle_gap={group_gap}s ({group_gap // 60}min)"
                        )
                else:
                    # Legacy config structure with flat airports list
                    all_airports = [
                        a.upper().strip()
                        for a in type_config.get("airports", [])
                        if a and a.strip()
                    ]
                    # Apply default settings to all airports
                    for airport in all_airports:
                        self.airport_settings[airport] = {
                            "priority": default_priority,
                            "min_cycle_gap": default_gap,
                        }

                if all_airports:
                    config_entry["airports"] = all_airports
                    config_entry["payload"] = {
                        "max_clicks": type_config.get("max_load_more_clicks", 10),
                        "load_more_delay": type_config.get("load_more_delay", 2.0),
                    }
                    task_types[task_type] = config_entry

            # JetPhotos: auto-create tasks for aircraft without images
            elif task_type == "jetphotos":
                config_entry["auto_create"] = True
                config_entry["batch_size"] = type_config.get("batch_size", 100)
                config_entry["min_cycle_gap"] = type_config.get("min_cycle_gap", 86400)  # 24 hours
                task_types[task_type] = config_entry

            # FR24 Map: region-based real-time aircraft tracking
            elif task_type == "fr24_map":
                global_coverage = type_config.get("global_coverage", False)
                max_priority = type_config.get("max_priority", 3)
                include_oceans = type_config.get("include_oceans", False)

                if global_coverage:
                    # Use predefined global regions from fr24_map_source
                    from src.scraper.sources.fr24_map_source import GLOBAL_REGIONS

                    regions = []
                    for name, region_config in GLOBAL_REGIONS.items():
                        # Filter by priority
                        if region_config.get("priority", 1) > max_priority:
                            continue
                        # Filter ocean regions if not enabled
                        if not include_oceans:
                            if any(
                                x in name.lower()
                                for x in ["ocean", "pacific", "atlantic", "arctic"]
                            ):
                                continue
                        regions.append(
                            {
                                "name": name,
                                "lat": region_config["lat"],
                                "lon": region_config["lon"],
                                "zoom": region_config.get("zoom", 5),
                                "priority": region_config.get("priority", 1),
                            }
                        )
                    # Sort by priority
                    regions.sort(key=lambda x: x.get("priority", 1))
                    logger.info(
                        f"FR24 Map global coverage enabled: {len(regions)} regions "
                        f"(max_priority={max_priority}, include_oceans={include_oceans})"
                    )
                else:
                    # Use custom regions from config
                    regions = type_config.get("regions", [])

                if regions:
                    config_entry["regions"] = regions
                    config_entry["min_cycle_gap"] = type_config.get(
                        "min_cycle_gap", 60
                    )  # Default 60s
                    config_entry["auto_create"] = True
                    task_types[task_type] = config_entry

            # ADS-B Exchange Map: region-based aircraft tracking.
            #
            # Uses ADSBX_REGIONS, not fr24_map's GLOBAL_REGIONS — one ADSBx
            # request has measured 11,998 aircraft over 85 degrees of latitude and
            # 182 of longitude, so the 50 small FR24 windows re-scraped the same
            # airspace up to eight times per cycle (see the adsbx_map_source
            # module docstring). There is no
            # ocean filter here: every ADSBx window spans ocean and land alike.
            # Each task also carries a dbFlags bit for the scraper's URL filter.
            elif task_type == "adsbx_map":
                global_coverage = type_config.get("global_coverage", False)
                max_priority = type_config.get("max_priority", 5)
                db_flags = int(type_config.get("db_flags", 1))

                if global_coverage:
                    from src.scraper.sources.adsbx_map_source import ADSBX_REGIONS

                    regions = []
                    for name, region_config in ADSBX_REGIONS.items():
                        if region_config.get("priority", 1) > max_priority:
                            continue
                        regions.append(
                            {
                                "name": name,
                                "lat": region_config["lat"],
                                "lon": region_config["lon"],
                                "zoom": region_config.get("zoom", 5),
                                "priority": region_config.get("priority", 1),
                            }
                        )
                    regions.sort(key=lambda x: x.get("priority", 1))
                    logger.info(
                        f"ADSBx Map global coverage enabled: {len(regions)} regions "
                        f"(max_priority={max_priority}, dbFlags={db_flags})"
                    )
                else:
                    regions = type_config.get("regions", [])

                if regions:
                    config_entry["regions"] = regions
                    config_entry["db_flags"] = db_flags
                    config_entry["min_cycle_gap"] = type_config.get("min_cycle_gap", 1800)
                    config_entry["target"] = str(type_config.get("target", "military")).lower()
                    config_entry["retention"] = type_config.get("retention", {})
                    config_entry["auto_create"] = True
                    task_types[task_type] = config_entry

            # FR24 Aircraft: registration-based flight history
            elif task_type == "fr24_aircraft":
                config_entry["auto_create"] = True
                config_entry["top_count"] = type_config.get("top_count", 1000)
                config_entry["min_cycle_gap"] = type_config.get("min_cycle_gap", 28800)  # 8 hours
                config_entry["payload"] = {
                    "max_clicks": type_config.get("max_load_earlier_clicks", 0),
                }
                task_types[task_type] = config_entry

        return task_types

    def cleanup_orphan_tasks(self) -> dict[str, int]:
        """Clean up orphan tasks from terminated workers.

        When EC2 instances are forcefully terminated, workers don't have a chance
        to release their tasks. This method detects and resets such orphan tasks.

        Returns:
            Dictionary with cleanup statistics.
        """
        stats = {"workers_cleaned": 0, "tasks_reset": 0}

        # Python-computed cutoff so the SQL is dialect-agnostic (both
        # Postgres and SQLite bind datetimes correctly through SQLAlchemy).
        cutoff = datetime.now(UTC) - timedelta(seconds=self.orphan_cleanup_threshold)
        with self.engine.connect() as conn:
            # Find stale workers (no heartbeat since cutoff)
            stale_workers = conn.execute(
                text("""
                    SELECT worker_id FROM scraper_workers
                    WHERE status = 'active'
                    AND last_heartbeat < :cutoff
                """),
                {"cutoff": cutoff},
            ).fetchall()

            if not stale_workers:
                return stats

            stale_worker_ids = [row.worker_id for row in stale_workers]
            logger.info(f"Found {len(stale_worker_ids)} stale workers to clean up")

            # Reset their processing tasks to pending
            for worker_id in stale_worker_ids:
                result = conn.execute(
                    text("""
                        UPDATE scraper_tasks
                        SET status = 'pending', claimed_by = NULL, claimed_at = NULL
                        WHERE status = 'processing'
                        AND claimed_by = :worker_id
                    """),
                    {"worker_id": worker_id},
                )
                stats["tasks_reset"] += result.rowcount

                # Mark worker as stopped
                conn.execute(
                    text("""
                        UPDATE scraper_workers
                        SET status = 'stopped'
                        WHERE worker_id = :worker_id
                    """),
                    {"worker_id": worker_id},
                )
                stats["workers_cleaned"] += 1

            conn.commit()

            if stats["tasks_reset"] > 0 or stats["workers_cleaned"] > 0:
                logger.info(
                    f"Cleanup complete: {stats['workers_cleaned']} workers stopped, "
                    f"{stats['tasks_reset']} tasks reset"
                )

            # Terminate unhealthy EC2 instances so ASG replaces them
            if stale_worker_ids and self.auto_terminate_unhealthy:
                terminated = self._terminate_unhealthy_instances(stale_worker_ids)
                stats["instances_terminated"] = terminated

        return stats

    def cleanup_stuck_tasks(self) -> dict[str, int]:
        """Reset tasks that have been in processing state for too long.

        Unlike cleanup_orphan_tasks which only handles dead workers,
        this resets tasks stuck even if the worker is still alive
        (e.g., browser hung, task taking too long).

        Returns:
            Dictionary with cleanup statistics.
        """
        stats = {"tasks_reset": 0}

        cutoff = datetime.now(UTC) - timedelta(seconds=self.task_timeout)
        with self.engine.connect() as conn:
            # Find tasks stuck in processing since cutoff
            result = conn.execute(
                text("""
                    UPDATE scraper_tasks
                    SET status = 'pending', claimed_by = NULL, claimed_at = NULL
                    WHERE status = 'processing'
                    AND claimed_at < :cutoff
                    RETURNING id, task_type, task_key
                """),
                {"cutoff": cutoff},
            )

            reset_tasks = result.fetchall()
            stats["tasks_reset"] = len(reset_tasks)

            if reset_tasks:
                conn.commit()
                for task in reset_tasks:
                    logger.info(
                        f"Reset stuck task: {task.task_type}/{task.task_key} "
                        f"(processing > {self.task_timeout}s)"
                    )

        return stats

    def _terminate_unhealthy_instances(self, worker_ids: list[str]) -> int:
        """Terminate EC2 instances for stale workers.

        Extracts IP from worker_id (format: worker-ip-10-0-1-51-xxx) and
        terminates the corresponding EC2 instance. ASG will automatically
        launch a replacement.

        Args:
            worker_ids: List of stale worker IDs.

        Returns:
            Number of instances terminated.
        """
        if self.is_local:
            return 0

        import boto3
        from botocore.exceptions import ClientError

        terminated = 0
        ec2 = boto3.client("ec2")

        for worker_id in worker_ids:
            # Extract IP from worker_id: worker-ip-10-0-1-51-xxx -> 10.0.1.51
            match = re.search(r"worker-ip-(\d+)-(\d+)-(\d+)-(\d+)-", worker_id)
            if not match:
                logger.warning(f"Cannot extract IP from worker_id: {worker_id}")
                continue

            ip = f"{match.group(1)}.{match.group(2)}.{match.group(3)}.{match.group(4)}"

            try:
                # Find instance by private IP
                response = ec2.describe_instances(
                    Filters=[{"Name": "private-ip-address", "Values": [ip]}]
                )

                for reservation in response.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        instance_id = instance["InstanceId"]
                        state = instance["State"]["Name"]

                        if state in ("running", "pending"):
                            logger.info(f"Terminating unhealthy instance {instance_id} ({ip})")
                            ec2.terminate_instances(InstanceIds=[instance_id])
                            terminated += 1

            except ClientError as e:
                logger.error(f"Error terminating instance for {ip}: {e}")

        if terminated > 0:
            logger.info(f"Terminated {terminated} unhealthy instances")

        return terminated

    def auto_scale_workers(self) -> dict[str, Any]:
        """Auto-scale ASG based on pending task count.

        Scales up when there are many pending tasks, scales down when few.
        Uses cooldown period to prevent scale-down flapping.

        Returns:
            Dictionary with scaling statistics.
        """
        if self.is_local or not self.auto_scale_enabled or not self.asg_name:
            return {"enabled": False}

        import boto3
        from botocore.exceptions import ClientError

        stats = {
            "enabled": True,
            "pending_tasks": 0,
            "current_capacity": 0,
            "desired_capacity": 0,
            "action": "none",
        }

        try:
            # Get pending task count
            with self.engine.connect() as conn:
                result = conn.execute(
                    text("""
                        SELECT COUNT(*) FROM scraper_tasks
                        WHERE status IN ('pending', 'processing')
                    """)
                )
                pending = result.fetchone()[0]
                stats["pending_tasks"] = pending

            # Get current ASG capacity
            autoscaling = boto3.client("autoscaling")
            response = autoscaling.describe_auto_scaling_groups(
                AutoScalingGroupNames=[self.asg_name]
            )

            if not response.get("AutoScalingGroups"):
                logger.warning(f"ASG not found: {self.asg_name}")
                return stats

            asg = response["AutoScalingGroups"][0]
            current_capacity = asg["DesiredCapacity"]
            stats["current_capacity"] = current_capacity

            # Check and sync ASG Min/Max limits with config
            current_min = asg["MinSize"]
            current_max = asg["MaxSize"]

            if current_min != self.min_instances or current_max != self.max_instances:
                logger.info(
                    f"Syncing ASG limits: min {current_min}->{self.min_instances}, "
                    f"max {current_max}->{self.max_instances}"
                )
                autoscaling.update_auto_scaling_group(
                    AutoScalingGroupName=self.asg_name,
                    MinSize=self.min_instances,
                    MaxSize=self.max_instances,
                )
                stats["asg_limits_synced"] = True

            # Calculate desired capacity based on pending tasks
            # Each worker can handle tasks_per_worker tasks
            desired = max(
                self.min_instances,
                min(
                    self.max_instances,
                    (pending + self.tasks_per_worker - 1) // self.tasks_per_worker,
                ),
            )
            stats["desired_capacity"] = desired

            # Scale up immediately, scale down with cooldown
            if desired > current_capacity:
                # Scale up
                self._scale_down_counter = 0
                logger.info(
                    f"Scaling up: {current_capacity} -> {desired} "
                    f"(pending={pending}, tasks_per_worker={self.tasks_per_worker})"
                )
                autoscaling.set_desired_capacity(
                    AutoScalingGroupName=self.asg_name,
                    DesiredCapacity=desired,
                )
                stats["action"] = f"scale_up_{current_capacity}_to_{desired}"

            elif desired < current_capacity:
                # Scale down with cooldown to prevent flapping
                self._scale_down_counter += 1
                if self._scale_down_counter >= self.scale_down_cooldown:
                    logger.info(
                        f"Scaling down: {current_capacity} -> {desired} "
                        f"(pending={pending}, cooldown reached)"
                    )
                    autoscaling.set_desired_capacity(
                        AutoScalingGroupName=self.asg_name,
                        DesiredCapacity=desired,
                    )
                    stats["action"] = f"scale_down_{current_capacity}_to_{desired}"
                    self._scale_down_counter = 0
                else:
                    stats["action"] = (
                        f"scale_down_pending_{self._scale_down_counter}/{self.scale_down_cooldown}"
                    )
            else:
                self._scale_down_counter = 0
                stats["action"] = "no_change"

        except ClientError as e:
            logger.error(f"Auto-scaling error: {e}")
            stats["error"] = str(e)

        return stats

    def create_tasks_for_airports(
        self,
        task_type: str,
        airports: list[str],
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Create tasks for specified airports if not already queued.

        Uses per-airport settings from self.airport_settings for priority and min_cycle_gap.

        Args:
            task_type: Task type (e.g., "fr24_arrivals", "fr24_airport").
            airports: List of airport codes.
            payload: Additional task payload.

        Returns:
            Number of tasks created.
        """
        import json

        created = 0
        default_gap = self.config.get("scraper", {}).get("scheduler", {}).get("min_cycle_gap", 3600)

        with self.engine.connect() as conn:
            for airport in airports:
                # Get per-airport settings
                airport_config = self.airport_settings.get(airport, {})
                min_cycle_gap = airport_config.get("min_cycle_gap", default_gap)
                priority = airport_config.get("priority", 10)

                # Check if task already exists (pending or processing)
                result = conn.execute(
                    text("""
                        SELECT id FROM scraper_tasks
                        WHERE task_type = :task_type
                        AND task_key = :airport
                        AND status IN ('pending', 'processing', 'claimed')
                    """),
                    {"task_type": task_type, "airport": airport},
                )

                if result.fetchone():
                    continue  # Task already queued

                # Check if recently completed (respect per-airport min_cycle_gap)
                result = conn.execute(
                    text("""
                        SELECT completed_at FROM scraper_tasks
                        WHERE task_type = :task_type
                        AND task_key = :airport
                        AND status IN ('completed', 'no_data')
                        ORDER BY completed_at DESC
                        LIMIT 1
                    """),
                    {"task_type": task_type, "airport": airport},
                )

                row = result.fetchone()
                if row and row.completed_at:
                    elapsed = (
                        datetime.now(UTC) - row.completed_at.replace(tzinfo=UTC)
                    ).total_seconds()
                    if elapsed < min_cycle_gap:
                        continue  # Too soon to recreate

                # Create new task with per-airport priority
                conn.execute(
                    text("""
                        INSERT INTO scraper_tasks
                        (task_type, task_key, status, priority, payload, created_at, scheduled_for)
                        VALUES (:task_type, :airport, 'pending', :priority, :payload,
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                    {
                        "task_type": task_type,
                        "airport": airport,
                        "priority": priority,
                        "payload": json.dumps(payload or {}),
                    },
                )
                created += 1
                logger.debug(
                    f"Created task for {airport} (priority={priority}, min_gap={min_cycle_gap}s)"
                )

            conn.commit()

        if created > 0:
            logger.info(f"Created {created} new {task_type} tasks")

        return created

    def create_fr24_map_tasks(
        self,
        regions: list[dict[str, Any]],
        priority: int = 0,
        min_cycle_gap: int = 60,
    ) -> int:
        """Create fr24_map tasks for specified regions.

        Args:
            regions: List of region configs with lat, lon, zoom, and optional name.
            priority: Task priority.
            min_cycle_gap: Minimum seconds between task cycles per region.

        Returns:
            Number of tasks created.
        """
        import json

        created = 0
        with self.engine.connect() as conn:
            for region in regions:
                lat = region.get("lat")
                lon = region.get("lon")
                zoom = region.get("zoom", 4)
                name = region.get("name", f"{lat}_{lon}_{zoom}")

                if lat is None or lon is None:
                    logger.warning(f"Skipping region without lat/lon: {region}")
                    continue

                # Task key format: "lat,lon,zoom" (matches CLI format)
                task_key = f"{lat},{lon},{zoom}"

                # Check if task already exists (pending or processing)
                result = conn.execute(
                    text("""
                        SELECT id FROM scraper_tasks
                        WHERE task_type = 'fr24_map'
                        AND task_key = :task_key
                        AND status IN ('pending', 'processing', 'claimed')
                    """),
                    {"task_key": task_key},
                )

                if result.fetchone():
                    continue  # Task already queued

                # Check if recently completed (respect min_cycle_gap)
                result = conn.execute(
                    text("""
                        SELECT completed_at FROM scraper_tasks
                        WHERE task_type = 'fr24_map'
                        AND task_key = :task_key
                        AND status IN ('completed', 'no_data')
                        ORDER BY completed_at DESC
                        LIMIT 1
                    """),
                    {"task_key": task_key},
                )

                row = result.fetchone()
                if row and row.completed_at:
                    elapsed = (
                        datetime.now(UTC) - row.completed_at.replace(tzinfo=UTC)
                    ).total_seconds()
                    if elapsed < min_cycle_gap:
                        continue  # Too soon to recreate

                # Create new task
                payload = {
                    "lat": lat,
                    "lon": lon,
                    "zoom": zoom,
                    "region_name": name,
                }

                conn.execute(
                    text("""
                        INSERT INTO scraper_tasks
                        (task_type, task_key, status, priority, payload, created_at, scheduled_for)
                        VALUES ('fr24_map', :task_key, 'pending', :priority, :payload,
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                    {
                        "task_key": task_key,
                        "priority": priority,
                        "payload": json.dumps(payload),
                    },
                )
                created += 1
                logger.debug(f"Created fr24_map task for region '{name}': {task_key}")

            conn.commit()

        if created > 0:
            logger.info(f"Created {created} new fr24_map tasks")

        return created

    def create_adsbx_map_tasks(
        self,
        regions: list[dict[str, Any]],
        priority: int = 0,
        min_cycle_gap: int = 60,
        db_flags: int = 1,
    ) -> int:
        """Create adsbx_map tasks for specified regions.

        Mirrors :meth:`create_fr24_map_tasks` but attaches ``dbFlags`` to the
        payload so the scraper's URL filter (``?dbFlags=N``) and its
        military-only gate both receive the same bit set.

        Args:
            regions: Region configs with lat, lon, zoom, and optional name.
            priority: Task priority.
            min_cycle_gap: Minimum seconds between task cycles per region.
            db_flags: tar1090 filter bit (1=military, 2=interesting, etc.).

        Returns:
            Number of tasks created.
        """
        import json

        created = 0
        with self.engine.connect() as conn:
            for region in regions:
                lat = region.get("lat")
                lon = region.get("lon")
                zoom = region.get("zoom", 4)
                name = region.get("name", f"{lat}_{lon}_{zoom}")

                if lat is None or lon is None:
                    logger.warning(f"Skipping adsbx region without lat/lon: {region}")
                    continue

                # Use the region name as the task key so the scheduler's
                # "already queued / recently completed" dedup matches the
                # local ADSBxMapTaskSource's key space exactly.
                task_key = str(name)

                result = conn.execute(
                    text("""
                        SELECT id FROM scraper_tasks
                        WHERE task_type = 'adsbx_map'
                        AND task_key = :task_key
                        AND status IN ('pending', 'processing', 'claimed')
                    """),
                    {"task_key": task_key},
                )
                if result.fetchone():
                    continue

                result = conn.execute(
                    text("""
                        SELECT completed_at FROM scraper_tasks
                        WHERE task_type = 'adsbx_map'
                        AND task_key = :task_key
                        AND status IN ('completed', 'no_data')
                        ORDER BY completed_at DESC
                        LIMIT 1
                    """),
                    {"task_key": task_key},
                )
                row = result.fetchone()
                if row and row.completed_at:
                    elapsed = (
                        datetime.now(UTC) - row.completed_at.replace(tzinfo=UTC)
                    ).total_seconds()
                    if elapsed < min_cycle_gap:
                        continue

                payload = {
                    "lat": lat,
                    "lon": lon,
                    "zoom": zoom,
                    "region_name": name,
                    "dbFlags": db_flags,
                }
                conn.execute(
                    text("""
                        INSERT INTO scraper_tasks
                        (task_type, task_key, status, priority, payload, created_at, scheduled_for)
                        VALUES ('adsbx_map', :task_key, 'pending', :priority, :payload,
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                    {
                        "task_key": task_key,
                        "priority": priority,
                        "payload": json.dumps(payload),
                    },
                )
                created += 1
                logger.debug(f"Created adsbx_map task for region '{name}': {task_key}")

            conn.commit()

        if created > 0:
            logger.info(f"Created {created} new adsbx_map tasks")

        return created

    def prune_adsbx_positions(self, target: str, retention: dict[str, Any]) -> int:
        """Expire rows from ``adsbx_positions`` per the configured horizons.

        Only the ``positions`` target is pruned. ``adsbx_military_positions`` and
        ``aircraft_snapshots`` have accumulated without any retention policy since
        they were created, so applying one to them here would silently delete
        history nobody asked to lose; ``adsbx_positions`` is new and is the table
        whose full-fleet volume made retention necessary in the first place.

        Runs at most once an hour regardless of how often the cycle fires.

        Args:
            target: The configured ``scraper.scrapers.adsbx_map.target``.
            retention: Mapping with optional ``civil_hours`` / ``military_hours``.

        Returns:
            Rows deleted this call, or 0 when skipped.
        """
        if target != "positions":
            return 0

        now = datetime.now(UTC)
        if self._last_prune is not None and (now - self._last_prune) < timedelta(hours=1):
            return 0
        self._last_prune = now

        from src.scraper.sinks.adsbx_map_sink import POSITIONS_TABLE, prune_positions

        return prune_positions(
            self.engine,
            POSITIONS_TABLE,
            civil_hours=int(retention.get("civil_hours", 168)),
            military_hours=int(retention.get("military_hours", 720)),
        )

    def create_image_download_tasks(
        self,
        batch_size: int = 100,
        priority: int = 0,
    ) -> int:
        """Create jetphotos tasks for aircraft without images.

        Queries aircraft_static_info for registrations where images_downloaded is false,
        and creates jetphotos tasks for them (if not already queued).

        Args:
            batch_size: Maximum number of tasks to create per cycle.
            priority: Task priority.

        Returns:
            Number of tasks created.
        """
        import json

        created = 0
        with self.engine.connect() as conn:
            # Find aircraft without images that don't have pending/processing/no_data tasks
            result = conn.execute(
                text("""
                    SELECT asi.registration
                    FROM aircraft_static_info asi
                    WHERE (asi.images_downloaded = false OR asi.images_downloaded IS NULL)
                    AND NOT EXISTS (
                        SELECT 1 FROM scraper_tasks st
                        WHERE st.task_type = 'jetphotos'
                        AND st.task_key = asi.registration
                        AND st.status IN ('pending', 'processing', 'claimed', 'no_data')
                    )
                    ORDER BY asi.last_updated DESC NULLS LAST
                    LIMIT :batch_size
                """),
                {"batch_size": batch_size},
            )

            registrations = [row.registration for row in result]

            if not registrations:
                return 0

            # Create tasks for each registration
            for registration in registrations:
                conn.execute(
                    text("""
                        INSERT INTO scraper_tasks
                        (task_type, task_key, status, priority, payload, created_at, scheduled_for)
                        VALUES ('jetphotos', :registration, 'pending', :priority, :payload,
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                    {
                        "registration": registration,
                        "priority": priority,
                        "payload": json.dumps({}),
                    },
                )
                created += 1

            conn.commit()

        if created > 0:
            logger.info(f"Created {created} new jetphotos tasks for aircraft without images")

        return created

    def create_trending_aircraft_search_tasks(
        self,
        top_count: int = 10,
        search_max_results: int = 10,
        priority: int = 0,
        min_cycle_gap: int = 604800,  # 7 days
    ) -> int:
        """Create xiaohongshu_search_author tasks for top trending aircraft.

        Queries aircraft_attention_aggregate for top N by trending_score,
        then creates search tasks for each registration.

        Args:
            top_count: Number of top aircraft to search.
            search_max_results: Max search results per aircraft.
            priority: Task priority.
            min_cycle_gap: Minimum seconds between searches for same registration.

        Returns:
            Number of tasks created.
        """
        import json

        created = 0
        with self.engine.connect() as conn:
            # Get top trending aircraft registrations
            result = conn.execute(
                text("""
                    SELECT registration, trending_score
                    FROM aircraft_attention_aggregate
                    WHERE registration IS NOT NULL
                    ORDER BY trending_score DESC NULLS LAST
                    LIMIT :top_count
                """),
                {"top_count": top_count},
            )

            registrations = [(row.registration, row.trending_score) for row in result]

            for registration, score in registrations:
                # Check if task already exists (pending or processing)
                existing = conn.execute(
                    text("""
                        SELECT id FROM scraper_tasks
                        WHERE task_type = 'xiaohongshu_search_author'
                        AND task_key = :registration
                        AND status IN ('pending', 'processing', 'claimed')
                    """),
                    {"registration": registration},
                ).fetchone()

                if existing:
                    continue

                # Check if recently searched (respect min_cycle_gap)
                recent = conn.execute(
                    text("""
                        SELECT completed_at FROM scraper_tasks
                        WHERE task_type = 'xiaohongshu_search_author'
                        AND task_key = :registration
                        AND status IN ('completed', 'no_data')
                        ORDER BY completed_at DESC
                        LIMIT 1
                    """),
                    {"registration": registration},
                ).fetchone()

                if recent and recent.completed_at:
                    elapsed = (
                        datetime.now(UTC) - recent.completed_at.replace(tzinfo=UTC)
                    ).total_seconds()
                    if elapsed < min_cycle_gap:
                        continue

                # Create new search task
                conn.execute(
                    text("""
                        INSERT INTO scraper_tasks
                        (task_type, task_key, status, priority, payload,
                         created_at, scheduled_for)
                        VALUES ('xiaohongshu_search_author', :registration,
                                'pending', :priority, :payload,
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                    {
                        "registration": registration,
                        "priority": priority,
                        "payload": json.dumps(
                            {
                                "max_results": search_max_results,
                                "source": "trending_aircraft",
                                "trending_score": float(score) if score else 0,
                            }
                        ),
                    },
                )
                created += 1
                logger.debug(
                    f"Created search task for trending aircraft {registration} (score={score})"
                )

            conn.commit()

        if created > 0:
            logger.info(
                f"Created {created} xiaohongshu_search_author tasks "
                f"for top {top_count} trending aircraft"
            )

        return created

    def create_fr24_aircraft_tasks(
        self,
        top_count: int = 1000,
        priority: int = 0,
        min_cycle_gap: int = 28800,  # 8 hours
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Create fr24_aircraft tasks for top trending aircraft.

        Queries aircraft_attention_aggregate for top N by trending_score,
        then creates fr24_aircraft tasks for each registration.

        Args:
            top_count: Number of top aircraft to scrape (default: 1000).
            priority: Task priority.
            min_cycle_gap: Minimum seconds between scrapes (default: 8 hours).
            payload: Additional task payload (e.g., max_clicks).

        Returns:
            Number of tasks created.
        """
        import json

        created = 0
        with self.engine.connect() as conn:
            # Get top trending aircraft registrations
            result = conn.execute(
                text("""
                    SELECT registration, trending_score
                    FROM aircraft_attention_aggregate
                    WHERE registration IS NOT NULL
                    ORDER BY trending_score DESC NULLS LAST
                    LIMIT :top_count
                """),
                {"top_count": top_count},
            )

            registrations = [(row.registration, row.trending_score) for row in result]

            # Regex to validate aircraft registration
            # Valid formats: B-1234, N12345, 10+01 (German military), etc.
            import re

            valid_reg_pattern = re.compile(
                r"^[A-Z0-9][-+A-Z0-9]*[A-Z0-9]$|^[A-Z0-9]{2}$", re.IGNORECASE
            )

            for registration, score in registrations:
                # Skip invalid registrations
                if not valid_reg_pattern.match(registration):
                    logger.debug(f"Skipping invalid registration: {registration}")
                    continue

                # Check if task already exists (pending or processing)
                existing = conn.execute(
                    text("""
                        SELECT id FROM scraper_tasks
                        WHERE task_type = 'fr24_aircraft'
                        AND task_key = :registration
                        AND status IN ('pending', 'processing', 'claimed')
                    """),
                    {"registration": registration},
                ).fetchone()

                if existing:
                    continue

                # Check if recently scraped (respect min_cycle_gap)
                recent = conn.execute(
                    text("""
                        SELECT completed_at FROM scraper_tasks
                        WHERE task_type = 'fr24_aircraft'
                        AND task_key = :registration
                        AND status IN ('completed', 'no_data')
                        ORDER BY completed_at DESC
                        LIMIT 1
                    """),
                    {"registration": registration},
                ).fetchone()

                if recent and recent.completed_at:
                    elapsed = (
                        datetime.now(UTC) - recent.completed_at.replace(tzinfo=UTC)
                    ).total_seconds()
                    if elapsed < min_cycle_gap:
                        continue

                # Build task payload
                task_payload = {
                    "source": "trending_aircraft",
                    "trending_score": float(score) if score else 0,
                }
                if payload:
                    task_payload.update(payload)

                # Create new task
                conn.execute(
                    text("""
                        INSERT INTO scraper_tasks
                        (task_type, task_key, status, priority, payload,
                         created_at, scheduled_for)
                        VALUES ('fr24_aircraft', :registration,
                                'pending', :priority, :payload,
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                    {
                        "registration": registration,
                        "priority": priority,
                        "payload": json.dumps(task_payload),
                    },
                )
                created += 1
                logger.debug(f"Created fr24_aircraft task for {registration} (score={score})")

            conn.commit()

        if created > 0:
            logger.info(
                f"Created {created} fr24_aircraft tasks for top {top_count} trending aircraft"
            )

        return created

    def schedule_cycle(self) -> dict[str, Any]:
        """Run one scheduling cycle.

        Returns:
            Dictionary with cycle statistics.
        """
        results: dict[str, Any] = {
            "cleanup": self.cleanup_orphan_tasks(),
            "stuck_tasks": self.cleanup_stuck_tasks(),
            "tasks_created": {},
        }

        # Create tasks for each configured task type
        for task_type, config in self.task_types.items():
            # Skip task types that don't auto-create
            if not config.get("auto_create", True):
                continue

            # FR24 airport-based tasks (uses per-airport settings from self.airport_settings)
            if "airports" in config:
                created = self.create_tasks_for_airports(
                    task_type=task_type,
                    airports=config["airports"],
                    payload=config.get("payload"),
                )
                results["tasks_created"][task_type] = created

            # JetPhotos image download tasks
            elif task_type == "jetphotos":
                created = self.create_image_download_tasks(
                    batch_size=config.get("batch_size", 100),
                    priority=config.get("priority", 0),
                )
                results["tasks_created"][task_type] = created

            # FR24 Map region-based tasks
            elif task_type == "fr24_map" and "regions" in config:
                created = self.create_fr24_map_tasks(
                    regions=config["regions"],
                    priority=config.get("priority", 0),
                    min_cycle_gap=config.get("min_cycle_gap", 60),
                )
                results["tasks_created"][task_type] = created

            # ADS-B Exchange Map region-based tasks
            elif task_type == "adsbx_map" and "regions" in config:
                created = self.create_adsbx_map_tasks(
                    regions=config["regions"],
                    priority=config.get("priority", 0),
                    min_cycle_gap=config.get("min_cycle_gap", 1800),
                    db_flags=config.get("db_flags", 1),
                )
                results["tasks_created"][task_type] = created
                results["adsbx_pruned"] = self.prune_adsbx_positions(
                    config.get("target", "military"),
                    config.get("retention") or {},
                )

            # FR24 Aircraft flight history tasks
            elif task_type == "fr24_aircraft":
                created = self.create_fr24_aircraft_tasks(
                    top_count=config.get("top_count", 1000),
                    priority=config.get("priority", 0),
                    min_cycle_gap=config.get("min_cycle_gap", 28800),
                    payload=config.get("payload"),
                )
                results["tasks_created"][task_type] = created

        # Auto-scale workers based on task queue depth
        results["auto_scale"] = self.auto_scale_workers()

        return results

    def get_status(self) -> dict[str, Any]:
        """Get current status of all tasks.

        Returns:
            Dictionary with task statistics per task type.
        """
        status = {"tasks": {}, "workers": {}}

        with self.engine.connect() as conn:
            # Task statistics
            result = conn.execute(
                text("""
                    SELECT task_type, status, COUNT(*) as count
                    FROM scraper_tasks
                    GROUP BY task_type, status
                    ORDER BY task_type, status
                """)
            )

            for row in result:
                if row.task_type not in status["tasks"]:
                    status["tasks"][row.task_type] = {}
                status["tasks"][row.task_type][row.status] = row.count

            # Worker statistics
            result = conn.execute(
                text("""
                    SELECT status, COUNT(*) as count
                    FROM scraper_workers
                    GROUP BY status
                """)
            )
            status["workers"] = {row.status: row.count for row in result}

            # Active workers with recent heartbeat (within last 2 minutes)
            healthy_cutoff = datetime.now(UTC) - timedelta(minutes=2)
            result = conn.execute(
                text("""
                    SELECT COUNT(*) as count
                    FROM scraper_workers
                    WHERE status = 'active'
                    AND last_heartbeat > :cutoff
                """),
                {"cutoff": healthy_cutoff},
            )
            status["workers"]["healthy"] = result.fetchone().count

        return status

    def run(self) -> None:
        """Run the scheduler loop."""
        self._running = True
        logger.info(
            f"TaskScheduler starting, check_interval={self.check_interval}s, "
            f"orphan_cleanup_threshold={self.orphan_cleanup_threshold}s"
        )

        while self._running:
            try:
                results = self.schedule_cycle()

                # Log results
                cleanup = results["cleanup"]
                stuck_tasks = results["stuck_tasks"]
                tasks_created = results["tasks_created"]

                if cleanup["workers_cleaned"] > 0 or cleanup["tasks_reset"] > 0:
                    logger.info(f"Cleanup: {cleanup}")

                if stuck_tasks["tasks_reset"] > 0:
                    logger.info(f"Reset {stuck_tasks['tasks_reset']} stuck tasks")

                total_created = sum(tasks_created.values())
                if total_created > 0:
                    logger.info(f"Scheduled {total_created} tasks: {tasks_created}")

            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)

            # Wait for next cycle
            for _ in range(self.check_interval):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("TaskScheduler stopped")

    def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False


def main() -> None:
    """Main entry point for task scheduler."""
    parser = argparse.ArgumentParser(description="Scraper Task Scheduler")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to configuration file",
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=None,
        help="Seconds between scheduler checks (overrides config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current status and exit",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Run cleanup only and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load config using YAMLConfig (handles .env overrides for database)
    from src.utils.yaml_config import YAMLConfig

    yaml_config = YAMLConfig(args.config)
    config = yaml_config.config

    # Get database URL (YAMLConfig handles DB_HOST env override)
    db_config = yaml_config.get_database_config()
    database_url = db_config.get("url", "")

    if not database_url:
        logger.error("No database URL configured")
        sys.exit(1)

    # Create scheduler
    scheduler = TaskScheduler(
        config=config,
        database_url=database_url,
        check_interval=args.check_interval,
    )

    # Handle signals
    def signal_handler(signum, frame):
        logger.info("Received shutdown signal")
        scheduler.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.status:
        # Show status only
        status = scheduler.get_status()
        print("\n=== Task Status ===")
        for task_type, stats in status["tasks"].items():
            print(f"\n{task_type}:")
            for s, count in stats.items():
                print(f"  {s}: {count}")
        print("\n=== Workers ===")
        for s, count in status["workers"].items():
            print(f"  {s}: {count}")

    elif args.cleanup:
        # Run cleanup only
        results = scheduler.cleanup_orphan_tasks()
        print(f"Cleanup results: {results}")

    elif args.once:
        # Run once
        results = scheduler.schedule_cycle()
        print(f"Cycle results: {results}")
        status = scheduler.get_status()
        print(f"Current status: {status}")

    else:
        # Run loop
        scheduler.run()


if __name__ == "__main__":
    main()
