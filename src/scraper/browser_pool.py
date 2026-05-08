"""
Browser pool management for DrissionPage instances.

Manages a pool of browser instances with thread-safe acquisition/release.
Includes health checking and automatic recycling of crashed browsers.
"""

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("scraper.browser_pool")


@dataclass
class BrowserInstance:
    """Wrapper for a browser instance with metadata.

    Attributes:
        browser: The DrissionPage browser instance.
        id: Unique identifier for this instance.
        created_at: When this browser was created.
        last_used_at: When this browser was last used.
        in_use: Whether the browser is currently being used.
        tasks_processed: Number of tasks processed (for stats only).
        healthy: Whether the browser is known to be healthy.
    """

    browser: Any
    id: int
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    in_use: bool = False
    tasks_processed: int = 0
    healthy: bool = True


class BrowserPool:
    """Pool of DrissionPage browser instances.

    Features:
        - Thread-safe acquisition and release
        - Health checking to detect crashed browsers
        - Automatic recycling of unhealthy browsers
        - Slot-based release to prevent leaks

    Usage:
        pool = BrowserPool(size=1)
        pool.initialize()

        browser = pool.acquire()
        try:
            # use browser
            pass
        finally:
            pool.release(browser)

        pool.shutdown()
    """

    def __init__(
        self,
        size: int = 1,
        drission_options: dict[str, Any] | None = None,
        max_tasks_per_browser: int = 50,
        **kwargs: Any,
    ) -> None:
        """Initialize the browser pool.

        Args:
            size: Number of browser instances in the pool.
            drission_options: DrissionPage configuration options.
            max_tasks_per_browser: Recycle browser after this many tasks (0 = never).
            **kwargs: Ignored (for backwards compatibility with old configs).
        """
        self.size = size
        self.drission_options = drission_options or {}
        self.max_tasks_per_browser = max_tasks_per_browser

        self._pool: list[BrowserInstance] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._initialized = False
        self._shutdown = False

        # Map browser object id to instance id for reliable release
        self._browser_to_instance: dict[int, int] = {}

    def initialize(self) -> None:
        """Initialize the browser pool by creating browser instances."""
        if self._initialized:
            logger.warning("BrowserPool already initialized")
            return

        logger.info(f"Initializing browser pool with {self.size} instance(s)")

        for _ in range(self.size):
            try:
                instance = self._create_browser_instance()
                self._pool.append(instance)
                self._browser_to_instance[id(instance.browser)] = instance.id
            except Exception as e:
                logger.error(f"Error creating browser instance: {e}")

        self._initialized = True
        logger.info(f"Browser pool initialized with {len(self._pool)} browser(s)")

    def _find_browser_path(self) -> str | None:
        """Find available browser executable path.

        Checks common browser locations in order of preference.

        Returns:
            Path to browser executable, or None to use DrissionPage default.
        """
        # Browser paths in order of preference
        browser_candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]

        # First check config override
        config_path = self.drission_options.get("browser_path")
        if config_path and os.path.isfile(config_path):
            return config_path

        # Check candidates
        for path in browser_candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path

        # Fallback to shutil.which
        for name in [
            "google-chrome",
            "google-chrome-stable",
            "chromium-browser",
            "chromium",
        ]:
            path = shutil.which(name)
            if path:
                return path

        logger.warning("No browser found, using DrissionPage default")
        return None

    def _configure_chromium_options(self) -> Any:
        """Configure and return ChromiumOptions with standard settings.

        Centralizes browser configuration to avoid duplication between
        _create_browser_instance() and _recycle_browser().

        Returns:
            Configured ChromiumOptions instance.
        """
        import platform

        from DrissionPage import ChromiumOptions

        co = ChromiumOptions()

        # Auto-detect browser path (google-chrome preferred, fallback to chromium)
        browser_path = self._find_browser_path()
        if browser_path:
            co.set_browser_path(browser_path)
            logger.debug(f"Using browser: {browser_path}")

        # Standard browser arguments
        # Only use --no-sandbox on Linux (not needed on macOS and can cause issues)
        if platform.system() == "Linux":
            co.set_argument("--no-sandbox")
            co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--window-size=1920,1080")
        co.set_argument("--disable-blink-features=AutomationControlled")

        # Headless mode from config
        if self.drission_options.get("headless", False):
            co.set_argument("--headless")

        # Additional custom arguments from config
        for arg in self.drission_options.get("arguments", []):
            co.set_argument(arg)

        # Auto-assign port to avoid conflicts
        co.auto_port()

        return co

    def _create_browser_instance(self) -> BrowserInstance:
        """Create a new browser instance with configured options.

        Returns:
            A new BrowserInstance wrapper.
        """
        from DrissionPage import ChromiumPage

        co = self._configure_chromium_options()

        with self._lock:
            instance_id = self._next_id
            self._next_id += 1

        # Create browser
        browser = ChromiumPage(co)

        # Set page load timeout (default is often too short for slow sites like FR24)
        page_load_timeout = self.drission_options.get("page_load_timeout", 120)
        browser.set.timeouts(page_load=page_load_timeout)
        logger.debug(f"Set page load timeout to {page_load_timeout}s")

        logger.info(f"Created browser instance {instance_id}")

        return BrowserInstance(
            browser=browser,
            id=instance_id,
        )

    def _check_browser_health(self, instance: BrowserInstance) -> bool:
        """Check if a browser instance is healthy.

        Args:
            instance: The browser instance to check.

        Returns:
            True if the browser is healthy, False otherwise.
        """
        try:
            # Try to access a basic property - if browser crashed, this will fail
            _ = instance.browser.title
            return True
        except Exception as e:
            logger.warning(f"Browser {instance.id} health check failed: {e}")
            return False

    def _recycle_browser(self, instance: BrowserInstance) -> bool:
        """Recycle a browser instance by closing and recreating it.

        Args:
            instance: The browser instance to recycle.

        Returns:
            True if recycling was successful, False otherwise.
        """
        from DrissionPage import ChromiumPage

        logger.info(f"Recycling browser {instance.id}")

        # Remove old mapping
        old_browser_id = id(instance.browser)
        self._browser_to_instance.pop(old_browser_id, None)

        # Try to close the old browser
        try:
            instance.browser.quit()
        except Exception as e:
            logger.warning(f"Error closing browser {instance.id} during recycle: {e}")

        # Create a new browser using centralized configuration
        try:
            co = self._configure_chromium_options()
            new_browser = ChromiumPage(co)

            page_load_timeout = self.drission_options.get("page_load_timeout", 120)
            new_browser.set.timeouts(page_load=page_load_timeout)

            # Update the instance
            instance.browser = new_browser
            instance.created_at = time.time()
            instance.tasks_processed = 0
            instance.healthy = True

            # Add new mapping
            self._browser_to_instance[id(new_browser)] = instance.id

            logger.info(f"Browser {instance.id} recycled successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to recycle browser {instance.id}: {e}")
            instance.healthy = False
            return False

    def acquire(self, timeout: float = 30.0) -> Any:
        """Acquire a browser from the pool.

        Includes health check and automatic recycling of unhealthy browsers.

        Args:
            timeout: Maximum time to wait for an available browser.

        Returns:
            A browser instance.

        Raises:
            TimeoutError: If no browser becomes available within timeout.
            RuntimeError: If pool is not initialized or is shut down.
        """
        if not self._initialized:
            raise RuntimeError("BrowserPool not initialized")
        if self._shutdown:
            raise RuntimeError("BrowserPool has been shut down")

        start_time = time.time()

        while time.time() - start_time < timeout:
            with self._lock:
                # Find an available browser
                for instance in self._pool:
                    if not instance.in_use:
                        # Check if browser needs recycling due to task count
                        if (
                            self.max_tasks_per_browser > 0
                            and instance.tasks_processed >= self.max_tasks_per_browser
                        ):
                            logger.info(
                                f"Browser {instance.id} reached max tasks "
                                f"({instance.tasks_processed}), recycling"
                            )
                            if not self._recycle_browser(instance):
                                continue  # Try next browser if recycle failed

                        # Health check
                        if not self._check_browser_health(instance):
                            logger.warning(f"Browser {instance.id} unhealthy, recycling")
                            if not self._recycle_browser(instance):
                                continue  # Try next browser if recycle failed

                        instance.in_use = True
                        instance.last_used_at = time.time()
                        logger.debug(f"Acquired browser {instance.id}")
                        return instance.browser

            # No browser available, wait a bit
            time.sleep(0.5)

        raise TimeoutError(f"No browser available within {timeout}s")

    def release(self, browser: Any) -> None:
        """Release a browser back to the pool.

        Uses object id mapping to reliably find the browser slot,
        preventing slot leaks when browser object reference changes.

        Args:
            browser: The browser instance to release.
        """
        browser_obj_id = id(browser)

        with self._lock:
            # Try to find by object id mapping first
            instance_id = self._browser_to_instance.get(browser_obj_id)

            if instance_id is not None:
                for instance in self._pool:
                    if instance.id == instance_id:
                        instance.in_use = False
                        instance.tasks_processed += 1
                        instance.last_used_at = time.time()
                        logger.debug(
                            f"Released browser {instance.id} "
                            f"(total tasks: {instance.tasks_processed})"
                        )
                        return

            # Fallback: try to find by object reference (for backwards compatibility)
            for instance in self._pool:
                if instance.browser is browser:
                    instance.in_use = False
                    instance.tasks_processed += 1
                    instance.last_used_at = time.time()
                    # Update mapping
                    self._browser_to_instance[browser_obj_id] = instance.id
                    logger.debug(
                        f"Released browser {instance.id} via fallback "
                        f"(total tasks: {instance.tasks_processed})"
                    )
                    return

            # If we still can't find it, force release the first in-use slot
            # This prevents permanent slot leaks
            for instance in self._pool:
                if instance.in_use:
                    logger.warning(f"Browser not found in pool, force-releasing slot {instance.id}")
                    instance.in_use = False
                    instance.healthy = False  # Mark as unhealthy for recycling on next acquire
                    return

        logger.error("Release failed: no matching browser and no in-use slots")

    def shutdown(self) -> None:
        """Shutdown the browser pool and close all browsers."""
        logger.info("Shutting down browser pool")
        self._shutdown = True

        with self._lock:
            for instance in self._pool:
                try:
                    instance.browser.quit()
                    logger.debug(f"Closed browser {instance.id}")
                except Exception as e:
                    logger.warning(f"Error closing browser {instance.id}: {e}")

            self._pool.clear()
            self._browser_to_instance.clear()

        self._initialized = False
        logger.info("Browser pool shut down")

    def get_stats(self) -> dict[str, Any]:
        """Get pool statistics.

        Returns:
            Dictionary with pool statistics.
        """
        with self._lock:
            available = sum(1 for i in self._pool if not i.in_use)
            total_processed = sum(i.tasks_processed for i in self._pool)
            unhealthy = sum(1 for i in self._pool if not i.healthy)

            return {
                "pool_size": self.size,
                "active_browsers": len(self._pool),
                "available": available,
                "in_use": len(self._pool) - available,
                "unhealthy": unhealthy,
                "total_tasks_processed": total_processed,
            }

    @property
    def available_count(self) -> int:
        """Get count of available browsers."""
        with self._lock:
            return sum(1 for i in self._pool if not i.in_use)

    def __enter__(self) -> "BrowserPool":
        """Context manager entry."""
        self.initialize()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.shutdown()
