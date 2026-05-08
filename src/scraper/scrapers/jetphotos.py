"""
JetPhotos scraper implementation.

Downloads aircraft photos from JetPhotos.com with support for:
- High-resolution image downloads
- Cloudflare bypass handling
- S3 upload integration
- Duplicate detection
- Automatic sync to aircraft_static_info table
"""

import hashlib
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from src.scraper.base import (
    BaseScraper,
    CloudflareBlockedError,
    NoDataFoundError,
    PageLoadError,
    ScraperError,
)
from src.scraper.extractors import JetPhotosExtractor
from src.scraper.models import ImageMetadata, JetPhotosResult, ScraperTask

logger = logging.getLogger("scraper.jetphotos")


class JetPhotosScraper(BaseScraper[JetPhotosResult]):
    """Scraper for downloading aircraft images from JetPhotos.com.

    Configuration options (in scraper config):
        max_images_per_aircraft: Maximum images to download per aircraft (default: 3)
        s3_upload: Whether to upload images to S3 (default: False)
        s3_bucket: S3 bucket name (required if s3_upload is True)
        s3_prefix: S3 key prefix (default: "data/jetphotos_images")
        images_dir: Local directory for images (default: "data/jetphotos_images")
        delete_local_after_upload: Delete local files after S3 upload (default: False)

    Task payload options:
        max_images: Override max_images_per_aircraft for this task
        existing_images: List of existing image paths to skip
    """

    task_type = "jetphotos"
    default_delay = (5.0, 10.0)  # Reduced delay for faster throughput
    requires_browser = True
    cloudflare_protected = True
    task_timeout = 600  # 10 minutes — multi-page image downloads

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the JetPhotos scraper.

        Args:
            config: Scraper configuration dictionary.
        """
        super().__init__(config)

        self.max_images = self.config.get("max_images_per_aircraft", 3)
        self.images_dir = self.config.get("images_dir", "data/jetphotos_images")

        # Pagination and download settings
        self.collect_all_metadata = self.config.get("collect_all_metadata", True)
        self.download_all_images = self.config.get("download_all_images", True)
        self.max_pages = self.config.get("max_pages", 50)  # Safety limit
        self.page_delay = self.config.get("page_delay", 3.0)  # Delay between pages

        # S3 configuration
        self.s3_enabled = self.config.get("s3_upload", False)
        self.s3_bucket = self.config.get("s3_bucket", "")
        self.s3_prefix = self.config.get("s3_prefix", "data/jetphotos_images")
        self.delete_local_after_upload = self.config.get("delete_local_after_upload", False)

        # S3 client
        self.s3_client: boto3.client | None = None

        # Database configuration for syncing to aircraft_static_info
        self.database_url = self.config.get("database_url", "")
        self.db_engine = None
        self.sync_to_static_info = self.config.get("sync_to_static_info", True)

        # Initialize extractor for metadata extraction
        self.extractor = JetPhotosExtractor()

    def setup(self) -> None:
        """Setup scraper resources."""
        super().setup()

        # Ensure images directory exists
        os.makedirs(self.images_dir, exist_ok=True)

        # Initialize S3 client if enabled
        if self.s3_enabled and self.s3_bucket:
            try:
                self.s3_client = boto3.client("s3")
                logger.info(f"S3 upload enabled: s3://{self.s3_bucket}/{self.s3_prefix}")
            except Exception as e:
                logger.error(f"Failed to initialize S3 client: {e}")
                self.s3_enabled = False

        # Initialize database engine for syncing to aircraft_static_info
        if self.sync_to_static_info and self.database_url:
            try:
                self.db_engine = create_engine(self.database_url, echo=False, pool_pre_ping=True)
                logger.info("Database sync enabled for aircraft_static_info")
            except Exception as e:
                logger.error(f"Failed to initialize database engine: {e}")
                self.sync_to_static_info = False

    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that the task can be processed.

        Args:
            task: The task to validate.

        Returns:
            True if valid, False otherwise.
        """
        if not task.task_key:
            return False

        # Validate registration format
        registration = task.task_key.strip().upper()
        if len(registration) < 2 or len(registration) > 15:
            return False

        # Check for invalid values
        invalid_values = {"", "UNKNOWN", "N/A", "NA", "NONE", "NULL", "TEST"}
        if registration in invalid_values:
            return False

        return True

    def build_url(self, task: ScraperTask, page: int = 1) -> str:
        """Build the JetPhotos URL for a registration.

        Args:
            task: The task with registration as task_key.
            page: Page number (1-based).

        Returns:
            JetPhotos registration page URL.
        """
        registration = task.task_key.strip().upper()
        # JetPhotos uses '-' instead of '+' for German military registrations
        # e.g., 10+01 should be searched as 10-01
        jetphotos_reg = registration.replace("+", "-")
        if page <= 1:
            return f"https://www.jetphotos.com/registration/{jetphotos_reg}"
        # JetPhotos uses showphotos.php for pagination
        return (
            f"https://www.jetphotos.com/showphotos.php?"
            f"keywords-type=reg&keywords={jetphotos_reg}&"
            f"sort-order=0&search-type=Advanced&keywords-contain=0&page={page}"
        )

    def _extract_pagination_info(self, html: str) -> tuple[int, int]:
        """Extract pagination information from the page.

        Args:
            html: HTML content of the page.

        Returns:
            Tuple of (current_page, total_pages).
        """
        # Pattern 1: JetPhotos showphotos.php pagination - &page=N format
        # Note: & may be encoded as &amp; in HTML
        # Example: /showphotos.php?...&amp;page=2 or &page=2
        page_links = re.findall(r"(?:&amp;|&|\?)page=(\d+)", html)
        if page_links:
            max_page = max(int(p) for p in page_links)
            # Find current page from active pager
            current_match = re.search(r"paging__pager--active[^>]*>\s*(\d+)\s*<", html)
            current = int(current_match.group(1)) if current_match else 1
            logger.debug(f"Pagination detected: current={current}, total={max_page}")
            return current, max_page

        # Pattern 2: Look for pagination links like /registration/XX/3
        page_links = re.findall(r'/registration/[^/]+/(\d+)"', html)
        if page_links:
            max_page = max(int(p) for p in page_links)
            current_match = re.search(
                r'class="[^"]*(?:active|current)[^"]*"[^>]*>\s*(\d+)\s*<', html
            )
            current = int(current_match.group(1)) if current_match else 1
            return current, max_page

        # Pattern 3: Look for "next" link to determine if more pages exist
        has_next = bool(re.search(r'class="[^"]*next[^"]*"', html, re.IGNORECASE))
        return 1, 2 if has_next else 1

    def _collect_all_photo_links(
        self,
        browser: Any,
        task: ScraperTask,
        registration: str,
    ) -> list[str]:
        """Collect photo links from all pages.

        Args:
            browser: DrissionPage browser instance.
            task: The scraper task.
            registration: Aircraft registration.

        Returns:
            List of all photo page links (deduplicated).
        """
        all_links: list[str] = []
        seen_links: set[str] = set()
        current_page = 1

        while current_page <= self.max_pages:
            url = self.build_url(task, current_page)

            if current_page == 1:
                # First page already loaded by caller
                html = browser.html
            else:
                logger.info(f"[{registration}] Loading page {current_page}: {url}")
                browser.get(url)
                time.sleep(self.page_delay)

                # Check if page loaded correctly
                html = browser.html
                title = browser.title or ""
                if registration.lower() not in title.lower():
                    logger.warning(f"[{registration}] Page {current_page} load failed, stopping")
                    break

            # Extract photo links from current page
            photo_links = re.findall(r'href="(/photo/\d+)"', html)
            new_count = 0
            for link in photo_links:
                if link not in seen_links:
                    seen_links.add(link)
                    all_links.append(link)
                    new_count += 1

            logger.info(
                f"[{registration}] Page {current_page}: found {len(photo_links)} links, "
                f"{new_count} new (total: {len(all_links)})"
            )

            # Check pagination
            _, total_pages = self._extract_pagination_info(html)
            logger.debug(f"[{registration}] Page {current_page}, total_pages={total_pages}")

            # Also check for "next page" link as backup
            # JetPhotos uses showphotos.php?...&page=N format (& may be &amp; in HTML)
            next_page_exists = bool(
                re.search(r"(?:&amp;|&|\?)page=\d+", html)
                or re.search(r'class="[^"]*next[^"]*"', html, re.IGNORECASE)
            )

            # Determine if we should continue
            if current_page >= total_pages and not next_page_exists:
                logger.info(f"[{registration}] Reached last page ({current_page}/{total_pages})")
                break

            if new_count == 0:
                # No new links found, might be at the end
                logger.info(f"[{registration}] No new links on page {current_page}, stopping")
                break

            current_page += 1
            time.sleep(self.page_delay)

        return all_links

    def scrape(self, task: ScraperTask, browser: Any | None = None) -> JetPhotosResult:
        """Download images and collect metadata for an aircraft registration.

        Supports pagination to collect all photos across multiple pages.

        Args:
            task: Task with registration as task_key.
            browser: DrissionPage browser instance.

        Returns:
            JetPhotosResult with downloaded image paths and metadata.

        Raises:
            ScraperError: If scraping fails.
        """
        if browser is None:
            raise ScraperError(
                "Browser required for JetPhotos scraper",
                task_key=task.task_key,
                retryable=False,
            )

        registration = task.task_key.strip().upper()
        max_images = task.payload.get("max_images", self.max_images)
        existing_images = task.payload.get("existing_images", [])
        collect_all = task.payload.get("collect_all_metadata", self.collect_all_metadata)
        download_all = task.payload.get("download_all_images", self.download_all_images)

        # Check how many images we need to download
        # If download_all is True, we'll download all images (images_needed = infinity)
        images_needed = float("inf") if download_all else max_images - len(existing_images)

        # Visit first page
        url = self.build_url(task)
        logger.info(f"[{registration}] Visiting: {url}")
        browser.get(url)
        time.sleep(8)

        # Handle Cloudflare challenge if present (wait up to 180s)
        if not self.handle_cloudflare(browser, max_wait=180):
            logger.warning(f"[{registration}] Cloudflare challenge failed")
            raise CloudflareBlockedError(task_key=task.task_key)

        # Verify page loaded correctly
        title = browser.title or ""
        if registration.lower() not in title.lower():
            logger.warning(f"[{registration}] Page load failed: {title}")
            raise PageLoadError(url, task_key=task.task_key)

        # Collect photo links - either from all pages or just first page
        if collect_all:
            photo_links = self._collect_all_photo_links(browser, task, registration)
        else:
            html = browser.html
            photo_links = re.findall(r'href="(/photo/\d+)"', html)
            photo_links = list(dict.fromkeys(photo_links))

        if not photo_links:
            logger.warning(f"[{registration}] No photo links found")
            raise NoDataFoundError(task_key=task.task_key)

        logger.info(f"[{registration}] Total photos found: {len(photo_links)}")

        # Process photos - collect metadata for all, but only download up to max_images
        downloaded_paths: list[str] = []
        images_metadata: list[ImageMetadata] = []
        all_collected = list(existing_images)

        for i, link in enumerate(photo_links):
            photo_url = f"https://www.jetphotos.com{link}"
            should_download = len(downloaded_paths) < images_needed

            # If not collecting all metadata and we've downloaded enough, stop
            if not collect_all and not should_download:
                logger.info(
                    f"[{registration}] Reached download limit ({len(downloaded_paths)} images), stopping"
                )
                break

            logger.info(
                f"[{registration}][{i + 1}/{len(photo_links)}] "
                f"{'Downloading' if should_download else 'Collecting metadata'}: {photo_url}"
            )

            try:
                if should_download:
                    # Download image and get metadata
                    result_path, metadata = self._download_from_photo_page(
                        browser, photo_url, registration
                    )
                    if result_path:
                        if not self._is_duplicate(result_path, all_collected):
                            final_path = self._handle_upload(result_path)
                            downloaded_paths.append(final_path)
                            all_collected.append(result_path)

                            img_metadata = ImageMetadata(
                                image_path=final_path,
                                source_url=metadata.get("source_url"),
                                jetphotos_id=metadata.get("jetphotos_id"),
                                photographer=metadata.get("photographer"),
                                photo_date=metadata.get("photo_date"),
                                upload_date=metadata.get("upload_date"),
                                location=metadata.get("location"),
                                airport_icao=metadata.get("airport_icao"),
                                airport_name=metadata.get("airport_name"),
                                file_size_bytes=metadata.get("file_size_bytes"),
                                notes=metadata.get("notes"),
                                camera=metadata.get("camera"),
                                views=metadata.get("views"),
                                likes=metadata.get("likes"),
                                badges=metadata.get("badges"),
                                html_s3_path=metadata.get("html_s3_path"),
                            )
                            images_metadata.append(img_metadata)

                            notes_preview = (
                                metadata.get("notes", "")[:50] + "..."
                                if metadata.get("notes") and len(metadata.get("notes", "")) > 50
                                else metadata.get("notes", "")
                            )
                            logger.info(
                                f"[{registration}] Downloaded: {final_path} "
                                f"(photographer: {metadata.get('photographer')}, "
                                f"notes: {notes_preview or 'N/A'})"
                            )
                        else:
                            logger.warning(f"[{registration}] Duplicate image, removing")
                            try:
                                os.remove(result_path)
                            except OSError:
                                pass
                else:
                    # Only collect metadata without downloading image (when collect_all=True)
                    metadata = self._collect_metadata_only(browser, photo_url)
                    if metadata:
                        img_metadata = ImageMetadata(
                            image_path="",  # No image downloaded
                            source_url=metadata.get("source_url"),
                            jetphotos_id=metadata.get("jetphotos_id"),
                            photographer=metadata.get("photographer"),
                            photo_date=metadata.get("photo_date"),
                            upload_date=metadata.get("upload_date"),
                            location=metadata.get("location"),
                            airport_icao=metadata.get("airport_icao"),
                            airport_name=metadata.get("airport_name"),
                            notes=metadata.get("notes"),
                            camera=metadata.get("camera"),
                            views=metadata.get("views"),
                            likes=metadata.get("likes"),
                            badges=metadata.get("badges"),
                            html_s3_path=metadata.get("html_s3_path"),
                        )
                        images_metadata.append(img_metadata)

                        logger.debug(
                            f"[{registration}] Metadata collected: {metadata.get('jetphotos_id')} "
                            f"(photographer: {metadata.get('photographer')})"
                        )

            except Exception as e:
                logger.warning(f"[{registration}] Error processing {photo_url}: {e}")

            # Delay between requests
            if i < len(photo_links) - 1:
                time.sleep(2)

        # Combine existing and new paths
        all_paths = [self._get_relative_path(p) for p in existing_images] + downloaded_paths

        logger.info(
            f"[{registration}] Complete: {len(downloaded_paths)} images downloaded, "
            f"{len(images_metadata)} metadata records collected"
        )

        # If download_all is True, return all paths; otherwise limit to max_images
        result_paths = all_paths if download_all else all_paths[:max_images]

        return JetPhotosResult(
            success=len(downloaded_paths) > 0 or len(images_metadata) > 0,
            task_key=task.task_key,
            task_type=self.task_type,
            registration=registration,
            image_paths=result_paths,
            image_count=len(all_paths),
            s3_uploaded=self.s3_enabled and len(downloaded_paths) > 0,
            images_metadata=images_metadata,
        )

    def _collect_metadata_only(
        self,
        browser: Any,
        photo_url: str,
    ) -> dict[str, Any] | None:
        """Collect metadata from a photo page without downloading the image.

        Args:
            browser: DrissionPage browser instance.
            photo_url: URL of the photo detail page.

        Returns:
            Metadata dictionary or None if failed.
        """
        try:
            browser.get(photo_url)
            time.sleep(5)

            # Handle Cloudflare challenge on metadata page
            if not self.handle_cloudflare(browser, max_wait=60):
                logger.warning(f"Cloudflare blocked metadata page: {photo_url}")
                return None

            html = browser.html
            metadata = self.extractor.extract(html, {"source_url": photo_url})

            # Upload HTML to S3 for debugging/re-extraction
            if metadata and metadata.get("jetphotos_id"):
                html_s3_path = self._upload_html_to_s3(html, metadata["jetphotos_id"])
                if html_s3_path:
                    metadata["html_s3_path"] = html_s3_path

            return metadata
        except Exception as e:
            logger.warning(f"Error collecting metadata from {photo_url}: {e}")
            return None

    def _upload_html_to_s3(self, html: str, jetphotos_id: str) -> str | None:
        """Upload original HTML to S3 for debugging and future re-extraction.

        Args:
            html: HTML content of the photo page.
            jetphotos_id: JetPhotos photo ID.

        Returns:
            S3 key if successful, None otherwise.
        """
        if not self.s3_client or not self.s3_bucket or not jetphotos_id:
            return None

        s3_key = f"{self.s3_prefix}/html/{jetphotos_id}.html"
        try:
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=s3_key,
                Body=html.encode("utf-8"),
                ContentType="text/html; charset=utf-8",
                CacheControl="public, max-age=2592000",  # 30 days
            )
            logger.debug(f"Uploaded HTML to S3: s3://{self.s3_bucket}/{s3_key}")
            return s3_key
        except ClientError as e:
            logger.warning(f"Failed to upload HTML to S3: {e}")
            return None

    def _download_from_photo_page(
        self,
        browser: Any,
        photo_url: str,
        registration: str,
    ) -> tuple[str | None, dict[str, Any]]:
        """Download high-resolution image from a photo detail page.

        Args:
            browser: DrissionPage browser instance.
            photo_url: URL of the photo detail page.
            registration: Aircraft registration for filename.

        Returns:
            Tuple of (local file path, metadata dict). Path is None if failed.
        """
        # Extract photo ID from URL first
        photo_id_match = re.search(r"/photo/(\d+)", photo_url)
        photo_id = photo_id_match.group(1) if photo_id_match else None

        logger.debug(f"Visiting photo page: {photo_url} (ID: {photo_id})")
        browser.get(photo_url)
        time.sleep(8)

        # Handle Cloudflare challenge on photo page
        if not self.handle_cloudflare(browser, max_wait=90):
            logger.warning(f"Cloudflare blocked photo page: {photo_url}")
            return None, {"source_url": photo_url, "jetphotos_id": photo_id}

        html = browser.html

        # CRITICAL: Verify the page loaded correctly by checking photo ID is in the page
        # This prevents extracting metadata from a cached/stale page
        if photo_id:
            max_retries = 3
            for retry in range(max_retries):
                # Check if the photo ID appears in the page (in URL references or image URLs)
                if f"/photo/{photo_id}" in html or f"/{photo_id}" in html:
                    break
                logger.warning(
                    f"Photo page {photo_id} not loaded correctly, retry {retry + 1}/{max_retries}"
                )
                browser.get(photo_url)
                time.sleep(10)
                html = browser.html
            else:
                logger.error(f"Failed to load photo page {photo_id} after {max_retries} retries")
                return None, {"source_url": photo_url, "jetphotos_id": photo_id}

        # Extract metadata from the page using extractor
        metadata = self.extractor.extract(html, {"source_url": photo_url})

        # Upload HTML to S3 for debugging/re-extraction
        if photo_id:
            html_s3_path = self._upload_html_to_s3(html, photo_id)
            if html_s3_path:
                metadata["html_s3_path"] = html_s3_path

        # Strategy 1: Find images containing photo ID
        primary_images = []
        if photo_id:
            id_pattern = rf"(//cdn\.jetphotos\.com/(?:full|400)/\d+/{photo_id}[^\"\'>\s]*\.jpg)"
            primary_images = re.findall(id_pattern, html)

        # Strategy 2: Find main photo area images
        if not primary_images:
            main_img_pattern = r'class="[^"]*(?:large-photo|photo-large|main-photo|photo-img)[^"]*"[^>]*src="(//cdn\.jetphotos\.com/[^"]+\.jpg)"'
            primary_images = re.findall(main_img_pattern, html, re.IGNORECASE)

        # Strategy 3: Find full resolution images
        full_pattern = r"(//cdn\.jetphotos\.com/full/\d+/[^\"\'>\s]+\.jpg)"
        full_images = re.findall(full_pattern, html)

        # Strategy 4: Find 400px images as fallback
        thumb_pattern = r"(//cdn\.jetphotos\.com/400/\d+/[^\"\'>\s]+\.jpg)"
        thumb_images = re.findall(thumb_pattern, html)

        # Strategy 5: General pattern as last resort
        general_pattern = r'(?:src|data-src)="(//cdn\.jetphotos\.com/[^"]+\.jpg)"'
        general_images = re.findall(general_pattern, html)

        # Combine by priority
        all_images = []
        seen = set()
        for img in primary_images + full_images + thumb_images + general_images:
            if img not in seen:
                seen.add(img)
                all_images.append(img)

        if not all_images:
            logger.warning(f"No images found on {photo_url}")
            return None, metadata

        # Get cookies for download
        cookies = {}
        for c in browser.cookies():
            cookies[c["name"]] = c["value"]

        # Try to download images
        for img in all_images:
            img_url = "https:" + img if img.startswith("//") else img

            # Try full resolution first
            if "/400/" in img_url:
                full_url = img_url.replace("/400/", "/full/")
                result, file_size = self._download_image(
                    full_url, registration, cookies, browser.user_agent, photo_url
                )
                if result:
                    metadata["file_size_bytes"] = file_size
                    return result, metadata

            # Try original URL
            result, file_size = self._download_image(
                img_url, registration, cookies, browser.user_agent, photo_url
            )
            if result:
                metadata["file_size_bytes"] = file_size
                return result, metadata

        return None, metadata

    def _download_image(
        self,
        img_url: str,
        registration: str,
        cookies: dict[str, str],
        user_agent: str,
        referer: str,
    ) -> tuple[str | None, int]:
        """Download a single image.

        Args:
            img_url: Image URL.
            registration: Aircraft registration for filename.
            cookies: Cookies from browser session.
            user_agent: User agent string.
            referer: Referer URL.

        Returns:
            Tuple of (local file path, file size in bytes). Path is None if failed.
        """
        try:
            resp = requests.get(
                img_url,
                headers={
                    "User-Agent": user_agent,
                    "Referer": referer,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Sec-Fetch-Dest": "image",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "cross-site",
                },
                cookies=cookies,
                timeout=30,
            )

            if resp.status_code == 200 and len(resp.content) > 10000:
                timestamp = int(time.time())
                res_tag = "_full" if "/full/" in img_url else ""
                safe_registration = (
                    registration.replace("/", "-").replace("\\", "-").replace(":", "-")
                )
                filepath = os.path.join(
                    self.images_dir, f"{safe_registration}{res_tag}_{timestamp}.jpg"
                )

                file_size = len(resp.content)
                with open(filepath, "wb") as f:
                    f.write(resp.content)

                logger.debug(f"Saved: {filepath} ({file_size:,} bytes)")
                return filepath, file_size
            else:
                logger.debug(
                    f"Download failed: status={resp.status_code}, size={len(resp.content)}"
                )
                return None, 0

        except Exception as e:
            logger.warning(f"Download error: {e}")
            return None, 0

    def _is_duplicate(self, new_path: str, existing_paths: list[str]) -> bool:
        """Check if an image is a duplicate of existing images.

        Args:
            new_path: Path to the new image.
            existing_paths: List of existing image paths.

        Returns:
            True if duplicate, False otherwise.
        """
        if not existing_paths or not os.path.exists(new_path):
            return False

        with open(new_path, "rb") as f:
            new_hash = hashlib.md5(f.read()).hexdigest()

        for existing in existing_paths:
            if os.path.exists(existing):
                with open(existing, "rb") as f:
                    if hashlib.md5(f.read()).hexdigest() == new_hash:
                        return True

        return False

    def _handle_upload(self, local_path: str) -> str:
        """Handle S3 upload if enabled.

        Args:
            local_path: Local file path.

        Returns:
            S3 key if uploaded, relative path otherwise.
        """
        if self.s3_enabled and self.s3_client and self.s3_bucket:
            s3_key = self._upload_to_s3(local_path)
            if s3_key:
                return s3_key

        return self._get_relative_path(local_path)

    def _upload_to_s3(self, local_path: str) -> str | None:
        """Upload a file to S3.

        Args:
            local_path: Local file path.

        Returns:
            S3 key if successful, None otherwise.
        """
        if not self.s3_client or not os.path.exists(local_path):
            return None

        try:
            filename = os.path.basename(local_path)
            s3_key = f"{self.s3_prefix}/{filename}" if self.s3_prefix else filename

            content_type = "image/jpeg"
            if local_path.lower().endswith(".png"):
                content_type = "image/png"
            elif local_path.lower().endswith(".gif"):
                content_type = "image/gif"

            self.s3_client.upload_file(
                local_path,
                self.s3_bucket,
                s3_key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": "public, max-age=86400",
                },
            )

            logger.info(f"Uploaded to S3: s3://{self.s3_bucket}/{s3_key}")

            # Delete local file if configured
            if self.delete_local_after_upload:
                try:
                    os.remove(local_path)
                except OSError as e:
                    logger.warning(f"Failed to delete local file: {e}")

            return s3_key

        except ClientError as e:
            logger.error(f"S3 upload failed: {e}")
            return None

    def _get_relative_path(self, local_path: str) -> str:
        """Convert local path to relative path for database storage.

        Args:
            local_path: Local filesystem path.

        Returns:
            Relative path suitable for database storage.
        """
        if local_path.startswith("https://") or local_path.startswith("s3://"):
            parts = local_path.split("/")
            for i, part in enumerate(parts):
                if part == "data" or part == self.s3_prefix.split("/")[0]:
                    return "/".join(parts[i:])
            return os.path.basename(local_path)

        filename = os.path.basename(local_path)
        return f"{self.s3_prefix}/{filename}" if self.s3_prefix else filename

    def on_success(self, task: ScraperTask, result: JetPhotosResult) -> None:
        """Handle successful scrape completion and sync to database.

        Args:
            task: The completed task.
            result: The scrape result with image paths and metadata.
        """
        super().on_success(task, result)

        # Sync images to aircraft_static_info table
        if self.sync_to_static_info and self.db_engine and result.image_paths:
            self._sync_to_aircraft_static_info(result.registration, result.image_paths)

        # Save image metadata to aircraft_images table
        if self.db_engine and result.images_metadata:
            self._save_images_metadata(result.registration, result.images_metadata)

    def _sync_to_aircraft_static_info(self, registration: str, image_paths: list[str]) -> bool:
        """Sync downloaded images to aircraft_static_info table.

        Updates the images_downloaded flag and images_updated_at timestamp.
        The actual image paths are now stored in aircraft_images table.

        Args:
            registration: Aircraft registration.
            image_paths: List of image paths.

        Returns:
            True if sync successful, False otherwise.
        """
        if not self.db_engine or not registration or not image_paths:
            return False

        try:
            with self.db_engine.connect() as conn:
                # Update aircraft_static_info to mark images as downloaded
                result = conn.execute(
                    text("""
                        UPDATE aircraft_static_info
                        SET images_downloaded = true,
                            images_updated_at = :updated_at
                        WHERE registration = :registration
                    """),
                    {
                        "registration": registration,
                        "updated_at": datetime.now(UTC),
                    },
                )
                conn.commit()

                if result.rowcount > 0:
                    logger.info(
                        f"[{registration}] Updated images_downloaded flag in aircraft_static_info"
                    )
                    return True
                else:
                    logger.warning(f"[{registration}] Not found in aircraft_static_info table")
                    return False

        except SQLAlchemyError as e:
            logger.error(f"[{registration}] Failed to sync to aircraft_static_info: {e}")
            return False

    def _save_images_metadata(self, registration: str, images_metadata: list[ImageMetadata]) -> int:
        """Save image metadata to aircraft_images table.

        Args:
            registration: Aircraft registration.
            images_metadata: List of image metadata objects.

        Returns:
            Number of images saved successfully.
        """
        if not self.db_engine or not registration or not images_metadata:
            return 0

        saved_count = 0

        try:
            with self.db_engine.connect() as conn:
                # Get aircraft_id from aircraft_static_info
                aircraft_id_result = conn.execute(
                    text("""
                        SELECT id FROM aircraft_static_info
                        WHERE registration = :registration
                    """),
                    {"registration": registration},
                ).fetchone()
                aircraft_id = aircraft_id_result[0] if aircraft_id_result else None

                # Get current max display_order for this registration
                max_order_result = conn.execute(
                    text("""
                        SELECT COALESCE(MAX(display_order), 0)
                        FROM aircraft_images
                        WHERE registration = :registration
                    """),
                    {"registration": registration},
                ).fetchone()
                current_max_order = max_order_result[0] if max_order_result else 0

                # Check if this registration already has images (for is_primary logic)
                has_existing_images = current_max_order > 0

                for i, meta in enumerate(images_metadata):
                    # Skip if no jetphotos_id (can't dedupe without it)
                    if not meta.jetphotos_id:
                        logger.warning(f"[{registration}] Skipping image without jetphotos_id")
                        continue

                    # Check if image already exists
                    existing = conn.execute(
                        text("""
                            SELECT id FROM aircraft_images
                            WHERE jetphotos_id = :jetphotos_id
                        """),
                        {"jetphotos_id": meta.jetphotos_id},
                    ).fetchone()

                    if existing:
                        # Update metadata for existing image if we have new data
                        has_new_data = any(
                            [
                                meta.photographer,
                                meta.photo_date,
                                meta.upload_date,
                                meta.location,
                                meta.notes,
                                meta.camera,
                                meta.views is not None,
                                meta.likes is not None,
                                meta.badges,
                                meta.html_s3_path,
                            ]
                        )
                        if has_new_data:
                            conn.execute(
                                text("""
                                    UPDATE aircraft_images
                                    SET photographer = COALESCE(:photographer, photographer),
                                        photo_date = COALESCE(:photo_date, photo_date),
                                        upload_date = COALESCE(:upload_date, upload_date),
                                        location = COALESCE(:location, location),
                                        airport_icao = COALESCE(:airport_icao, airport_icao),
                                        airport_name = COALESCE(:airport_name, airport_name),
                                        notes = COALESCE(:notes, notes),
                                        camera = COALESCE(:camera, camera),
                                        views = COALESCE(:views, views),
                                        likes = COALESCE(:likes, likes),
                                        badges = COALESCE(:badges, badges),
                                        html_s3_path = COALESCE(:html_s3_path, html_s3_path),
                                        updated_at = :updated_at
                                    WHERE jetphotos_id = :jetphotos_id
                                """),
                                {
                                    "photographer": meta.photographer,
                                    "photo_date": meta.photo_date,
                                    "upload_date": meta.upload_date,
                                    "location": meta.location,
                                    "airport_icao": meta.airport_icao,
                                    "airport_name": meta.airport_name,
                                    "notes": meta.notes,
                                    "camera": meta.camera,
                                    "views": meta.views,
                                    "likes": meta.likes,
                                    "badges": meta.badges,
                                    "html_s3_path": meta.html_s3_path,
                                    "updated_at": datetime.now(UTC),
                                    "jetphotos_id": meta.jetphotos_id,
                                },
                            )
                            conn.commit()
                            saved_count += 1
                            logger.debug(
                                f"[{registration}] Updated metadata for image {meta.jetphotos_id}"
                            )
                        else:
                            logger.debug(
                                f"[{registration}] Image {meta.jetphotos_id} already exists, no new metadata"
                            )
                        continue

                    # Calculate display_order and is_primary
                    display_order = current_max_order + saved_count + 1
                    # First image is primary only if no existing images
                    is_primary = (display_order == 1) and not has_existing_images

                    # Insert new image record with all fields
                    conn.execute(
                        text("""
                            INSERT INTO aircraft_images (
                                registration, aircraft_id, image_path, source_url, source,
                                photographer, photo_date, upload_date,
                                location, airport_icao, airport_name,
                                file_size_bytes, jetphotos_id, notes,
                                camera, views, likes, badges, html_s3_path,
                                display_order, is_primary,
                                created_at, updated_at
                            ) VALUES (
                                :registration, :aircraft_id, :image_path, :source_url, :source,
                                :photographer, :photo_date, :upload_date,
                                :location, :airport_icao, :airport_name,
                                :file_size_bytes, :jetphotos_id, :notes,
                                :camera, :views, :likes, :badges, :html_s3_path,
                                :display_order, :is_primary,
                                :created_at, :updated_at
                            )
                        """),
                        {
                            "registration": registration,
                            "aircraft_id": aircraft_id,
                            "image_path": meta.image_path,
                            "source_url": meta.source_url,
                            "source": "jetphotos",
                            "photographer": meta.photographer,
                            "photo_date": meta.photo_date,
                            "upload_date": meta.upload_date,
                            "location": meta.location,
                            "airport_icao": meta.airport_icao,
                            "airport_name": meta.airport_name,
                            "file_size_bytes": meta.file_size_bytes,
                            "jetphotos_id": meta.jetphotos_id,
                            "notes": meta.notes,
                            "camera": meta.camera,
                            "views": meta.views,
                            "likes": meta.likes,
                            "badges": meta.badges,
                            "html_s3_path": meta.html_s3_path,
                            "display_order": display_order,
                            "is_primary": is_primary,
                            "created_at": datetime.now(UTC),
                            "updated_at": datetime.now(UTC),
                        },
                    )
                    saved_count += 1

                conn.commit()

                if saved_count > 0:
                    logger.info(f"[{registration}] Saved {saved_count} image(s) to aircraft_images")

        except SQLAlchemyError as e:
            logger.error(f"[{registration}] Failed to save image metadata: {e}")

        return saved_count
