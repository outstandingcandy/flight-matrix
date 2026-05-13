"""
Scraper worker process.

Manages the lifecycle of a scraper worker including:
- Task polling and execution
- Browser pool management
- Heartbeat reporting
- Graceful shutdown
"""

import asyncio
import logging
import os
import signal
import socket
import subprocess
import time
import uuid
from datetime import datetime
from typing import Any, Literal

from src.scraper.base import BaseScraper, NoDataFoundError, ScraperError
from src.scraper.browser_pool import BrowserPool
from src.scraper.local_task_provider import LocalTaskProvider
from src.scraper.local_task_source import LocalTaskSource
from src.scraper.models import ScraperResult, ScraperTask, TaskStatus, WorkerStatus
from src.scraper.sources.cli_source import CLITaskSource
from src.scraper.sources.fr24_airport_source import FR24AirportTaskSource
from src.scraper.sources.fr24_map_source import FR24MapTaskSource
from src.scraper.sources.jetphotos_source import JetPhotosTaskSource
from src.scraper.task_provider import TaskProvider
from src.scraper.task_queue import TaskQueue

logger = logging.getLogger("scraper.worker")


class ScraperWorker:
    """Distributed scraper worker process.

    Manages task execution with browser pooling, heartbeat reporting,
    and graceful shutdown handling.

    Usage:
        from src.scraper import ScraperWorker, JetPhotosScraper

        worker = ScraperWorker(config)
        worker.register_scraper(JetPhotosScraper)
        await worker.run()

    Configuration (from config dict):
        scraper.worker.heartbeat_interval: Heartbeat interval in seconds (default: 30)
        scraper.worker.task_timeout: Task execution timeout (default: 300)
        scraper.browser_pool.size: Browser pool size (default: 1)
        scraper.task_queue.poll_interval: Queue poll interval (default: 5)
        scraper.xvfb.auto_start: Auto-start Xvfb (default: True)
        scraper.xvfb.display_base: Base display number (default: 55)
    """

    def __init__(
        self,
        config: dict[str, Any],
        database_url: str | None = None,
        worker_id: str | None = None,
        mode: Literal["distributed", "local"] = "distributed",
        cli_task_key: str | None = None,
        cli_payload: dict[str, Any] | None = None,
        pool_size: int | None = None,
        from_queue: bool = False,
    ) -> None:
        """Initialize the scraper worker.

        Args:
            config: Configuration dictionary.
            database_url: Database URL (overrides config).
            worker_id: Unique worker identifier (auto-generated if not provided).
            mode: Operating mode - "distributed" uses TaskQueue with PostgreSQL
                  task tables, "local" uses LocalTaskProvider polling
                  aircraft_static_info directly.
            cli_task_key: Optional task key from command line (--task argument).
            cli_payload: Optional payload for CLI task (e.g., start_page).
            pool_size: Optional browser pool size (overrides config).
            from_queue: If True in local mode, pull tasks from database queue.
        """
        self.config = config
        self.scraper_config = config.get("scraper", {})
        self.mode = mode
        self.cli_task_key = cli_task_key
        self.cli_payload = cli_payload or {}
        self.from_queue = from_queue

        # Database URL
        if database_url:
            self.database_url = database_url
        else:
            db_config = config.get("database", {})
            self.database_url = db_config.get("url", "")

        # Worker identification
        self.worker_id = worker_id or self._generate_worker_id()

        # Configuration values (required)
        worker_config = self.scraper_config.get("worker")
        if not worker_config:
            raise ValueError("Missing required config section: scraper.worker")
        self.heartbeat_interval = self._require_config(
            worker_config, "heartbeat_interval", "scraper.worker"
        )
        self.task_timeout = self._require_config(worker_config, "task_timeout", "scraper.worker")

        pool_config = self.scraper_config.get("browser_pool")
        if not pool_config:
            raise ValueError("Missing required config section: scraper.browser_pool")
        self.pool_size = (
            pool_size
            if pool_size is not None
            else self._require_config(pool_config, "size", "scraper.browser_pool")
        )

        queue_config = self.scraper_config.get("task_queue")
        if not queue_config:
            raise ValueError("Missing required config section: scraper.task_queue")
        self.poll_interval = self._require_config(
            queue_config, "poll_interval", "scraper.task_queue"
        )
        self.max_attempts = self._require_config(queue_config, "max_attempts", "scraper.task_queue")
        self.stale_task_timeout = self._require_config(
            queue_config, "stale_task_timeout", "scraper.task_queue"
        )

        xvfb_config = self.scraper_config.get("xvfb")
        if not xvfb_config:
            raise ValueError("Missing required config section: scraper.xvfb")
        self.xvfb_auto_start = self._require_config(xvfb_config, "auto_start", "scraper.xvfb")
        self.xvfb_display_base = self._require_config(xvfb_config, "display_base", "scraper.xvfb")

        # Runtime state
        self._scrapers: dict[str, BaseScraper] = {}
        self._task_provider: TaskProvider | None = None
        self._browser_pool: BrowserPool | None = None
        self._xvfb_process: subprocess.Popen | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._tasks_processed = 0
        self._start_time: datetime | None = None
        self._current_task_ids: dict[str, int] = {}  # task_type -> task_id for parallel processing
        self._task_heartbeat_interval = 30  # Update task heartbeat every 30 seconds

        logger.info(f"ScraperWorker initialized: {self.worker_id}")

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

    def _generate_worker_id(self) -> str:
        """Generate a unique worker ID.

        Returns:
            Unique worker identifier.
        """
        hostname = socket.gethostname()
        short_uuid = str(uuid.uuid4())[:8]
        return f"worker-{hostname}-{short_uuid}"

    def register_scraper(
        self,
        scraper_class: type[BaseScraper],
        config: dict[str, Any] | None = None,
    ) -> None:
        """Register a scraper implementation.

        Args:
            scraper_class: The scraper class to register.
            config: Optional scraper-specific configuration.
        """
        # Merge with config from yaml
        scraper_type = scraper_class.task_type
        yaml_config = self.scraper_config.get("scrapers", {}).get(scraper_type, {})
        merged_config = {**yaml_config, **(config or {})}

        # Add database_url to config for scrapers that need it
        if self.database_url:
            merged_config["database_url"] = self.database_url

        # Wire up flight-matrix DB sinks for aviation scrapers. Sinks own all
        # application-table writes; the submodule scraper only produces
        # structured Pydantic results.
        sink = self._build_sink_for(scraper_type, merged_config)

        scraper = scraper_class(merged_config)
        if sink is not None:
            from src.scraper.sinks import bind_sink

            bind_sink(scraper, sink)
        self._scrapers[scraper_type] = scraper
        logger.info(f"Registered scraper: {scraper_type}")

    def _build_sink_for(
        self, scraper_type: str, merged_config: dict[str, Any]
    ) -> Any:
        """Instantiate the flight-matrix sink for this scraper type.

        Also injects scraper-specific callbacks (``persist_*_callback``,
        ``add_task_callback``) into ``merged_config`` so the scraper can call
        them during its run.
        """
        if not self.database_url:
            return None

        db_url = self.database_url

        if scraper_type == "fr24_map":
            from src.scraper.sinks.fr24_map_sink import FR24MapSink

            return FR24MapSink(db_url)

        if scraper_type == "fr24_aircraft":
            from src.scraper.sinks.fr24_aircraft_sink import FR24AircraftSink

            return FR24AircraftSink(db_url)

        if scraper_type in ("fr24_airport", "fr24_arrivals", "fr24_departures"):
            from src.scraper.sinks.fr24_airport_sink import FR24AirportSink

            hint = (
                "arrival"
                if scraper_type == "fr24_arrivals"
                else "departure"
                if scraper_type == "fr24_departures"
                else ""
            )
            return FR24AirportSink(db_url, flight_type_hint=hint)

        if scraper_type == "airport_data":
            from src.scraper.sinks.airport_data_sink import AirportDataSink
            from src.scraper.task_queue import TaskQueue

            task_queue = TaskQueue(db_url) if db_url else None
            sink = AirportDataSink(db_url, task_queue=task_queue)
            merged_config.setdefault("persist_aircraft_callback", sink.persist_aircraft)
            merged_config.setdefault("add_task_callback", sink.add_tasks)
            return sink

        if scraper_type == "jetphotos":
            from src.scraper.sinks.jetphotos_sink import JetPhotosSink

            sink = JetPhotosSink(db_url)
            merged_config.setdefault("persist_images_callback", sink.persist_images)
            return sink

        return None

    def _start_xvfb(self) -> None:
        """Start Xvfb if configured and not already running."""
        if not self.xvfb_auto_start:
            return

        # Check if DISPLAY is already set
        current_display = os.environ.get("DISPLAY")
        if current_display:
            logger.info(f"DISPLAY already set: {current_display}")
            return

        # Try to start Xvfb
        display = f":{self.xvfb_display_base}"
        try:
            # Check if Xvfb is already running on this display
            result = subprocess.run(
                ["xdpyinfo", "-display", display],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info(f"Xvfb already running on {display}")
                os.environ["DISPLAY"] = display
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Start Xvfb
        try:
            self._xvfb_process = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)  # Wait for Xvfb to start
            os.environ["DISPLAY"] = display
            logger.info(f"Started Xvfb on {display}")
        except FileNotFoundError:
            logger.warning("Xvfb not found, continuing without virtual display")
        except Exception as e:
            logger.warning(f"Failed to start Xvfb: {e}")

    def _stop_xvfb(self) -> None:
        """Stop the Xvfb process if we started it."""
        if self._xvfb_process:
            try:
                self._xvfb_process.terminate()
                self._xvfb_process.wait(timeout=5)
                logger.info("Stopped Xvfb")
            except Exception as e:
                logger.warning(f"Error stopping Xvfb: {e}")
            finally:
                self._xvfb_process = None

    def _create_task_source(self, task_type: str) -> LocalTaskSource | None:
        """Create a local task source for a given task type.

        Args:
            task_type: The task type (e.g., 'jetphotos', 'fr24_airport').

        Returns:
            A LocalTaskSource implementation or None if not supported.
        """
        # If from_queue is True, use QueueTaskSource to pull from database
        if self.from_queue:
            from src.scraper.sources.queue_source import QueueTaskSource

            logger.info(f"Using QueueTaskSource for {task_type}")
            return QueueTaskSource(
                task_type=task_type,
                database_url=self.database_url,
                limit=10,
            )

        # Handle fr24_map specially - needs to parse coordinates from task_key
        if task_type == "fr24_map":
            if self.cli_task_key:
                # Parse coordinates from task_key (format: "lat,lon,zoom" or "lat,lon")
                parts = self.cli_task_key.split(",")
                payload: dict[str, Any] = {}
                if len(parts) >= 2:
                    try:
                        payload["lat"] = float(parts[0])
                        payload["lon"] = float(parts[1])
                        if len(parts) >= 3:
                            payload["zoom"] = int(parts[2])
                    except ValueError:
                        pass
                return CLITaskSource(
                    task_type=task_type,
                    task_key=self.cli_task_key,
                    payload=payload,
                )
            # No CLI task key - use FR24MapTaskSource for global coverage
            return FR24MapTaskSource(
                config=self.config,
                database_url=self.database_url,
            )

        # If CLI task key is provided, use CLITaskSource for other task types
        if self.cli_task_key:
            return CLITaskSource(
                task_type=task_type,
                task_key=self.cli_task_key,
                payload=self.cli_payload,
            )

        if task_type == "jetphotos":
            return JetPhotosTaskSource(
                database_url=self.database_url,
                config=self.config,
            )
        elif task_type in ("fr24_arrivals", "fr24_departures", "fr24_airport"):
            return FR24AirportTaskSource(
                task_type=task_type,
                config=self.config,
                database_url=self.database_url,
            )
        else:
            logger.warning(f"No local task source available for task type: {task_type}")
            return None

    async def _setup(self) -> None:
        """Perform worker setup operations."""
        logger.info(f"Setting up worker in {self.mode} mode...")

        # Start Xvfb if needed
        self._start_xvfb()

        # Initialize task provider based on mode
        if self.mode == "local":
            # Create generic LocalTaskProvider
            self._task_provider = LocalTaskProvider()

            # Register task sources for each registered scraper
            for task_type in self._scrapers:
                source = self._create_task_source(task_type)
                if source:
                    self._task_provider.register_source(source)

            logger.info(f"Using LocalTaskProvider with sources: {list(self._scrapers.keys())}")
        else:
            # Distributed mode - use TaskQueue
            self._task_provider = TaskQueue(self.database_url)
            self._task_provider.ensure_tables_exist()
            logger.info("Using TaskQueue (distributed mode)")

        # Initialize browser pool
        drission_config = self.scraper_config.get("drission_page", {})
        self._browser_pool = BrowserPool(
            size=self.pool_size,
            drission_options=drission_config,
        )
        self._browser_pool.initialize()

        # Register worker
        metadata = {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "scrapers": list(self._scrapers.keys()),
            "pool_size": self.pool_size,
            "mode": self.mode,
        }
        self._task_provider.register_worker(self.worker_id, metadata)

        # Setup scrapers
        for scraper in self._scrapers.values():
            scraper.setup()

        self._start_time = datetime.utcnow()
        logger.info("Worker setup complete")

    async def _teardown(self) -> None:
        """Perform worker teardown operations."""
        logger.info("Tearing down worker...")

        # Teardown scrapers
        for scraper in self._scrapers.values():
            try:
                scraper.teardown()
            except Exception as e:
                logger.warning(f"Error tearing down scraper: {e}")

        # Deactivate worker in database
        if self._task_provider:
            self._task_provider.deactivate_worker(self.worker_id)

        # Shutdown browser pool
        if self._browser_pool:
            self._browser_pool.shutdown()

        # Stop Xvfb
        self._stop_xvfb()

        logger.info("Worker teardown complete")

    async def _heartbeat_loop(self) -> None:
        """Background task for periodic heartbeat updates."""
        heartbeat_failures = 0
        while self._running:
            try:
                if self._task_provider:
                    # Get current task ID (first one if processing multiple tasks)
                    current_task_id = None
                    if self._current_task_ids:
                        current_task_id = next(iter(self._current_task_ids.values()), None)

                    self._task_provider.update_worker_heartbeat(
                        self.worker_id,
                        status=WorkerStatus.ACTIVE,
                        current_task_id=current_task_id,
                    )
                    heartbeat_failures = 0  # Reset on success
            except Exception as e:
                heartbeat_failures += 1
                logger.warning(f"Heartbeat error (attempt {heartbeat_failures}): {e}")
                # If heartbeat keeps failing, log more details
                if heartbeat_failures >= 3:
                    logger.error(
                        f"Heartbeat failed {heartbeat_failures} times consecutively. "
                        f"Worker: {self.worker_id}"
                    )

            await asyncio.sleep(self.heartbeat_interval)

    async def _task_heartbeat_loop(self) -> None:
        """Background task for updating current task heartbeats (supports parallel tasks)."""
        while self._running:
            try:
                if self._task_provider and self._current_task_ids:
                    for task_type, task_id in list(self._current_task_ids.items()):
                        try:
                            self._task_provider.update_task_heartbeat(task_id)
                        except Exception as e:
                            logger.warning(f"Task heartbeat error for {task_type}: {e}")
            except Exception as e:
                logger.warning(f"Task heartbeat loop error: {e}")

            await asyncio.sleep(self._task_heartbeat_interval)

    async def _process_task(self, task: ScraperTask) -> ScraperResult:
        """Process a single task.

        Args:
            task: The task to process.

        Returns:
            The scraper result.
        """
        scraper = self._scrapers.get(task.task_type)
        if not scraper:
            raise ValueError(f"No scraper registered for type: {task.task_type}")

        # Validate task
        if not scraper.validate_task(task):
            raise ValueError(f"Task validation failed: {task.task_key}")

        # Update status to processing
        if self._task_provider and task.id:
            self._task_provider.update_task_status(task.id, TaskStatus.PROCESSING)

        browser = None
        try:
            # Acquire browser if needed (inside try block for proper cleanup)
            if scraper.requires_browser and self._browser_pool:
                browser = self._browser_pool.acquire(timeout=60)

            # Execute scrape in thread pool to avoid blocking heartbeat
            start_time = time.time()
            result = await asyncio.to_thread(scraper.scrape, task, browser)
            duration = time.time() - start_time

            result.duration_seconds = duration
            await asyncio.to_thread(scraper.on_success, task, result)

            return result

        except Exception as e:
            # Only call on_failure if we got past the browser acquisition
            if browser is not None:
                await asyncio.to_thread(scraper.on_failure, task, e)
            raise

        finally:
            # Release browser if acquired
            if browser and self._browser_pool:
                self._browser_pool.release(browser)

            # Apply delay in thread pool (only if we had a browser, meaning we actually tried to scrape)
            if browser is not None:
                await asyncio.to_thread(scraper.wait_delay)

    async def _work_loop(self) -> None:
        """Main work loop - launches parallel workers for each task type."""
        task_types = list(self._scrapers.keys())

        if not task_types:
            logger.warning("No scrapers registered, work loop exiting")
            return

        # Build max_concurrent_by_type from scraper config
        scrapers_config = self.scraper_config.get("scrapers", {})
        max_concurrent_by_type: dict[str, int] = {}
        for task_type in task_types:
            scraper_cfg = scrapers_config.get(task_type, {})
            max_concurrent = scraper_cfg.get("max_concurrent_workers", -1)
            max_concurrent_by_type[task_type] = max_concurrent
            if max_concurrent != -1:
                logger.info(f"Task type {task_type} limited to {max_concurrent} concurrent workers")

        # Create parallel workers for each task type
        logger.info(f"Starting parallel workers for task types: {task_types}")
        type_workers = []
        for task_type in task_types:
            worker_coro = self._type_work_loop(task_type, max_concurrent_by_type)
            type_workers.append(asyncio.create_task(worker_coro))

        # Wait for all type workers (they run until shutdown)
        await asyncio.gather(*type_workers, return_exceptions=True)

    async def _type_work_loop(
        self,
        task_type: str,
        max_concurrent_by_type: dict[str, int],
    ) -> None:
        """Work loop for a single task type.

        Each task type has its own loop, allowing parallel processing
        of different task types while ensuring only one task per type
        is processed at a time.

        Args:
            task_type: The task type to process.
            max_concurrent_by_type: Concurrency limits by task type.
        """
        logger.info(f"[{task_type}] Type worker started")

        while self._running:
            try:
                # Check for shutdown
                if self._shutdown_event.is_set():
                    break

                # Claim tasks for this type only
                if not self._task_provider:
                    await asyncio.sleep(self.poll_interval)
                    continue

                tasks = self._task_provider.claim_tasks(
                    worker_id=self.worker_id,
                    task_types=[task_type],
                    limit=1,
                    stale_timeout_minutes=self.stale_task_timeout,
                    max_concurrent_by_type=max_concurrent_by_type,
                )

                if not tasks:
                    await asyncio.sleep(self.poll_interval)
                    continue

                task = tasks[0]

                if self._shutdown_event.is_set():
                    # Return task to queue on shutdown
                    self._task_provider.fail_task(
                        task.id,
                        "Worker shutting down",
                        retry=True,
                    )
                    break

                # Set current task for heartbeat updates
                self._current_task_ids[task_type] = task.id

                try:
                    start_time = time.time()
                    # Use per-scraper timeout if set, otherwise fall back to worker default
                    scraper = self._scrapers.get(task_type)
                    effective_timeout = (
                        scraper.task_timeout
                        if scraper and scraper.task_timeout > 0
                        else self.task_timeout
                    )
                    result = await asyncio.wait_for(
                        self._process_task(task),
                        timeout=effective_timeout,
                    )

                    # Complete task
                    duration = time.time() - start_time
                    self._task_provider.complete_task(
                        task.id,
                        result=result.model_dump(mode="json"),
                        worker_id=self.worker_id,
                        duration_seconds=duration,
                    )
                    self._task_provider.increment_worker_completed(self.worker_id)
                    self._tasks_processed += 1

                except TimeoutError:
                    logger.error(
                        f"[{task_type}] Task {task.task_key} timed out after {effective_timeout}s"
                    )
                    self._task_provider.fail_task(
                        task.id,
                        f"Task timed out after {effective_timeout}s",
                        worker_id=self.worker_id,
                        retry=True,
                    )

                except NoDataFoundError as e:
                    # Not a failure - target simply has no data
                    duration = time.time() - start_time
                    logger.info(f"[{task_type}] Task {task.task_key}: no data found")
                    self._task_provider.complete_task_no_data(
                        task.id,
                        reason=str(e),
                        worker_id=self.worker_id,
                        duration_seconds=duration,
                    )
                    self._task_provider.increment_worker_completed(self.worker_id)
                    self._tasks_processed += 1

                except ScraperError as e:
                    logger.error(f"[{task_type}] Scraper error: {e}")
                    self._task_provider.fail_task(
                        task.id,
                        str(e),
                        worker_id=self.worker_id,
                        retry=e.retryable,
                    )

                except Exception as e:
                    logger.error(f"[{task_type}] Unexpected error: {e}")
                    self._task_provider.fail_task(
                        task.id,
                        str(e),
                        worker_id=self.worker_id,
                        retry=True,
                    )

                finally:
                    # Clear current task after processing
                    self._current_task_ids.pop(task_type, None)

                # Exit if running CLI task (single task mode)
                if self.cli_task_key:
                    logger.info(f"[{task_type}] CLI task completed, initiating shutdown")
                    self._shutdown_event.set()
                    break

            except Exception as e:
                logger.error(f"[{task_type}] Work loop error: {e}")
                await asyncio.sleep(self.poll_interval)

        logger.info(f"[{task_type}] Type worker stopped")

    async def run(self) -> None:
        """Run the worker until shutdown."""
        logger.info(f"Starting worker {self.worker_id}")

        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._signal_handler)

        try:
            await self._setup()
            self._running = True

            # Start heartbeat, task heartbeat, and work loops
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            task_heartbeat_task = asyncio.create_task(self._task_heartbeat_loop())
            work_task = asyncio.create_task(self._work_loop())

            # Wait for shutdown or completion
            await self._shutdown_event.wait()

            # Cancel tasks
            heartbeat_task.cancel()
            task_heartbeat_task.cancel()
            work_task.cancel()

            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            try:
                await task_heartbeat_task
            except asyncio.CancelledError:
                pass

            try:
                await work_task
            except asyncio.CancelledError:
                pass

        finally:
            self._running = False
            await self._teardown()

        logger.info(f"Worker {self.worker_id} stopped")

    def _signal_handler(self) -> None:
        """Handle shutdown signals."""
        logger.info("Received shutdown signal")
        self._shutdown_event.set()

    def stop(self) -> None:
        """Request worker shutdown."""
        logger.info("Stop requested")
        self._shutdown_event.set()

    def get_status(self) -> dict[str, Any]:
        """Get worker status.

        Returns:
            Dictionary with worker status information.
        """
        uptime_seconds = 0
        if self._start_time:
            uptime_seconds = (datetime.utcnow() - self._start_time).total_seconds()

        browser_stats = {}
        if self._browser_pool:
            browser_stats = self._browser_pool.get_stats()

        queue_stats = {}
        if self._task_provider:
            queue_stats = self._task_provider.get_queue_stats()

        return {
            "worker_id": self.worker_id,
            "running": self._running,
            "uptime_seconds": uptime_seconds,
            "tasks_processed": self._tasks_processed,
            "registered_scrapers": list(self._scrapers.keys()),
            "browser_pool": browser_stats,
            "queue": queue_stats,
        }


async def run_worker(
    config: dict[str, Any],
    scrapers: list[type[BaseScraper]] | None = None,
    database_url: str | None = None,
    mode: Literal["distributed", "local"] = "distributed",
) -> None:
    """Convenience function to run a worker with scrapers.

    Args:
        config: Configuration dictionary.
        scrapers: List of scraper classes to register.
        database_url: Optional database URL override.
        mode: Operating mode - "distributed" or "local".
    """
    worker = ScraperWorker(config, database_url=database_url, mode=mode)

    # Register scrapers
    if scrapers:
        for scraper_class in scrapers:
            worker.register_scraper(scraper_class)

    await worker.run()
