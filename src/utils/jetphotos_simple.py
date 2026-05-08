#!/usr/bin/env python3
"""
JetPhotos simple download script - supports high-resolution image download

Usage (requires xvfb):
    # Download by registration
    xvfb-run python src/utils/jetphotos_simple.py N12345

    # Download a specific photo page directly
    xvfb-run python src/utils/jetphotos_simple.py --url https://www.jetphotos.com/photo/11573514

Or in an environment with a display:
    python src/utils/jetphotos_simple.py N12345
"""

import argparse
import logging
import os
import re
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def download_from_photo_page(
    page, photo_url: str, output_dir: str, registration: str = None
) -> str:
    """
    Download a high-resolution image from a photo detail page.

    Args:
        page: DrissionPage browser instance
        photo_url: Photo detail page URL (e.g. https://www.jetphotos.com/photo/11573514)
        output_dir: Output directory
        registration: Aircraft registration (optional, used for file naming)

    Returns:
        Downloaded file path, or None on failure
    """
    logger.info(f"Visiting photo detail page: {photo_url}")
    page.get(photo_url)
    time.sleep(8)

    html = page.html
    title = page.title or ""

    logger.info(f"Page title: {title}")
    logger.info(f"Page length: {len(html)} chars")

    # Check if blocked by Cloudflare
    if "just a moment" in html.lower() or "cloudflare" in html.lower():
        logger.warning("Detected Cloudflare challenge page, waiting longer...")
        time.sleep(15)
        html = page.html
        title = page.title or ""
        logger.info(f"Re-fetched page, length: {len(html)} chars")

    # Extract registration from the page (if not provided)
    if not registration:
        # Try to extract from title or page content
        reg_match = re.search(r"Registration[:\s]+([A-Z0-9-]+)", html, re.IGNORECASE)
        if reg_match:
            registration = reg_match.group(1)
        else:
            # Extract photo ID from URL as fallback name
            photo_id_match = re.search(r"/photo/(\d+)", photo_url)
            registration = f"photo_{photo_id_match.group(1)}" if photo_id_match else "unknown"

    logger.info(f"Registration: {registration}")

    # Extract photo ID from URL for precise main-image matching
    photo_id_match = re.search(r"/photo/(\d+)", photo_url)
    photo_id = photo_id_match.group(1) if photo_id_match else None
    logger.info(f"Photo ID: {photo_id}")

    # Strategy 1: Find image URLs containing the photo ID (most precise)
    # JetPhotos image URLs usually contain the photo ID, e.g. /full/6/12345678_xxxxx.jpg
    primary_images = []
    if photo_id:
        # Match images containing the photo ID
        id_pattern = rf'(//cdn\.jetphotos\.com/(?:full|400)/\d+/{photo_id}[^"\'>\s]*\.jpg)'
        primary_images = re.findall(id_pattern, html)
        if primary_images:
            logger.info(f"Found main images containing photo ID: {len(primary_images)}")

    # Strategy 2: Find images in large-photo or photo-details regions
    if not primary_images:
        # Try to find the main image region
        main_img_pattern = r'class="[^"]*(?:large-photo|photo-large|main-photo|photo-img)[^"]*"[^>]*src="(//cdn\.jetphotos\.com/[^"]+\.jpg)"'
        primary_images = re.findall(main_img_pattern, html, re.IGNORECASE)
        if primary_images:
            logger.info(f"Found main images by class: {len(primary_images)}")

    # Strategy 3: Find high-res image URLs (/full/ path preferred)
    full_pattern = r'(//cdn\.jetphotos\.com/full/\d+/[^"\'>\s]+\.jpg)'
    full_images = re.findall(full_pattern, html)

    # Strategy 4: Find /400/ path images as fallback
    thumb_pattern = r'(//cdn\.jetphotos\.com/400/\d+/[^"\'>\s]+\.jpg)'
    thumb_images = re.findall(thumb_pattern, html)

    # Strategy 5: General pattern (last resort)
    general_pattern = r'(?:src|data-src)="(//cdn\.jetphotos\.com/[^"]+\.jpg)"'
    general_images = re.findall(general_pattern, html)

    # Combine by priority: main > full size > 400 size > general
    all_images = []
    seen = set()
    for img in primary_images + full_images + thumb_images + general_images:
        if img not in seen:
            seen.add(img)
            all_images.append(img)

    if not all_images:
        logger.error("No image URLs found")
        return None

    logger.info(f"Found {len(all_images)} image URLs (priority-ordered)")
    for i, img in enumerate(all_images[:3]):
        logger.info(f"  [{i + 1}] {img}")

    # Get cookies
    cookies = {}
    for c in page.cookies():
        cookies[c["name"]] = c["value"]

    # Try downloading images
    for img in all_images:
        img_url = "https:" + img if img.startswith("//") else img

        # Prefer the full size
        if "/400/" in img_url:
            full_url = img_url.replace("/400/", "/full/")
            logger.info(f"Trying high-resolution: {full_url}")
            result = _download_image(
                full_url, output_dir, registration, cookies, page.user_agent, photo_url
            )
            if result:
                return result

        # Try the original URL
        logger.info(f"Downloading: {img_url}")
        result = _download_image(
            img_url, output_dir, registration, cookies, page.user_agent, photo_url
        )
        if result:
            return result

    logger.error("All image downloads failed")
    return None


def _download_image(
    img_url: str, output_dir: str, registration: str, cookies: dict, user_agent: str, referer: str
) -> str:
    """Download a single image"""
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
            # Tag whether this is high-resolution
            res_tag = "_full" if "/full/" in img_url else ""
            # Replace special characters in the filename
            safe_registration = registration.replace("/", "-").replace("\\", "-").replace(":", "-")
            filepath = os.path.join(output_dir, f"{safe_registration}{res_tag}_{timestamp}.jpg")
            with open(filepath, "wb") as f:
                f.write(resp.content)
            logger.info(f"Saved successfully: {filepath} ({len(resp.content):,} bytes)")
            return filepath
        else:
            logger.warning(f"Download failed: status={resp.status_code}, size={len(resp.content)}")
            return None
    except Exception as e:
        logger.warning(f"Download error: {e}")
        return None


def download_jetphotos_image(
    registration: str, output_dir: str = "data/jetphotos_images", high_res: bool = True
) -> str:
    """
    Download an aircraft image from JetPhotos.

    Args:
        registration: Aircraft registration
        output_dir: Output directory
        high_res: Whether to download high-resolution image (requires clicking through to detail page)

    Returns:
        Downloaded file path, or None on failure
    """
    from DrissionPage import ChromiumOptions, ChromiumPage

    registration = registration.strip().upper()
    os.makedirs(output_dir, exist_ok=True)

    # Check if a high-resolution image already exists
    from pathlib import Path

    existing_full = list(Path(output_dir).glob(f"{registration}_full_*.jpg"))
    if existing_full:
        logger.info(f"High-resolution image already exists: {existing_full[0]}")
        return str(existing_full[0])

    existing = list(Path(output_dir).glob(f"{registration}_*.jpg"))
    if existing and not high_res:
        logger.info(f"Image already exists: {existing[0]}")
        return str(existing[0])

    logger.info(f"Starting download for {registration}...")

    # Configure browser - do not use headless to bypass Cloudflare
    co = ChromiumOptions()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--window-size=1920,1080")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.auto_port()  # Auto-assign port to avoid conflicts

    page = None
    try:
        page = ChromiumPage(co)

        url = f"https://www.jetphotos.com/registration/{registration}"
        logger.info(f"Visiting: {url}")
        page.get(url)
        time.sleep(8)

        title = page.title
        logger.info(f"Page title: {title}")

        if registration.lower() not in title.lower():
            logger.error("Page failed to load or registration not found")
            return None

        html = page.html

        if high_res:
            # Find photo detail page links
            # Pattern: <a href="/photo/11573514">
            photo_links = re.findall(r'href="(/photo/\d+)"', html)
            photo_links = list(dict.fromkeys(photo_links))  # Deduplicate, preserve order

            if photo_links:
                logger.info(f"Found {len(photo_links)} photo detail page links")

                # Click the first image to go to its detail page
                photo_url = f"https://www.jetphotos.com{photo_links[0]}"
                result = download_from_photo_page(page, photo_url, output_dir, registration)
                if result:
                    return result

                # If the first fails, try others
                for link in photo_links[1:3]:
                    photo_url = f"https://www.jetphotos.com{link}"
                    result = download_from_photo_page(page, photo_url, output_dir, registration)
                    if result:
                        return result

            logger.warning(
                "No photo detail page links found, trying to download directly from the list page..."
            )

        # Download directly from the list page (low-res fallback)
        pattern = r'(?:src|data-src)="(//cdn\.jetphotos\.com/[^"]+\.jpg)"'
        images = re.findall(pattern, html)

        if not images:
            logger.error("No images found")
            return None

        logger.info(f"Found {len(images)} images")

        cookies = {}
        for c in page.cookies():
            cookies[c["name"]] = c["value"]

        for img in images[:5]:
            img_url = "https:" + img
            result = _download_image(
                img_url, output_dir, registration, cookies, page.user_agent, url
            )
            if result:
                return result

        logger.error("All image downloads failed")
        return None

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback

        traceback.print_exc()
        return None
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                # Cleanup path — swallow any browser shutdown error.
                pass


def download_by_url(photo_url: str, output_dir: str = "data/jetphotos_images") -> str:
    """
    Download a high-resolution image directly from a photo detail page URL.

    Args:
        photo_url: Photo detail page URL (e.g. https://www.jetphotos.com/photo/11573514)
        output_dir: Output directory

    Returns:
        Downloaded file path, or None on failure
    """
    from DrissionPage import ChromiumOptions, ChromiumPage

    os.makedirs(output_dir, exist_ok=True)

    # Configure browser - do not use headless to bypass Cloudflare
    co = ChromiumOptions()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--window-size=1920,1080")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.auto_port()  # Auto-assign port to avoid conflicts

    page = None
    try:
        page = ChromiumPage(co)
        return download_from_photo_page(page, photo_url, output_dir)
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback

        traceback.print_exc()
        return None
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                # Cleanup path — swallow any browser shutdown error.
                pass


def main():
    parser = argparse.ArgumentParser(
        description="JetPhotos image download tool - supports high-resolution downloads",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Download high-resolution image by registration
    xvfb-run python jetphotos_simple.py N12345

    # Download a specific photo page directly
    xvfb-run python jetphotos_simple.py --url https://www.jetphotos.com/photo/11573514

    # Download low-resolution image (skip detail page)
    xvfb-run python jetphotos_simple.py --low-res N12345

    # Batch download
    xvfb-run python jetphotos_simple.py N12345 B-1234 JA123A
        """,
    )

    parser.add_argument("registrations", nargs="*", help="Aircraft registrations (one or more)")
    parser.add_argument("--url", help="Download a specific photo page URL directly")
    parser.add_argument(
        "-o",
        "--output",
        default="data/jetphotos_images",
        help="Directory to save images (default: data/jetphotos_images)",
    )
    parser.add_argument(
        "--low-res", action="store_true", help="Download low-resolution image (skip detail page)"
    )

    args = parser.parse_args()

    if args.url:
        # Download the specified URL directly
        result = download_by_url(args.url, args.output)
        if result:
            print(f"Success: {result}")
        else:
            print("Download failed")
            sys.exit(1)
    elif args.registrations:
        # Download by registration
        for reg in args.registrations:
            result = download_jetphotos_image(reg, args.output, high_res=not args.low_res)
            if result:
                print(f"OK {reg}: {result}")
            else:
                print(f"FAIL {reg}: Download failed")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
