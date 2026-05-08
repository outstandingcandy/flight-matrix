#!/usr/bin/env python3
"""
Scrape all manufacturers from Airport-data.com.

Usage:
    # Step 1: Scrape all index pages to get manufacturer list
    uv run python scripts/scrape_all_airport_data.py --scrape-index

    # Step 2: Scrape all manufacturers
    uv run python scripts/scrape_all_airport_data.py

    # Resume from a specific manufacturer index
    uv run python scripts/scrape_all_airport_data.py --start-index 50

    # Scrape specific manufacturers
    uv run python scripts/scrape_all_airport_data.py --manufacturers Airbus Boeing

    # Scrape a specific index page (09, A-Z)
    uv run python scripts/scrape_all_airport_data.py --index A
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path


# Index letters for airport-data.com: 09 for numeric, then A-Z
INDEX_LETTERS = ["09"] + [chr(i) for i in range(ord("A"), ord("Z") + 1)]


def scrape_index(letter: str) -> bool:
    """Scrape a single index page."""
    cmd = [
        "uv", "run", "python", "src/scraper_main.py",
        "--local", "--scrapers", "airport_data",
        "--task", letter,
    ]

    print(f"\n{'='*60}")
    print(f"Scraping index: {letter}")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*60}\n")

    try:
        result = subprocess.run(cmd, timeout=600)  # 10 minute timeout
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT: index {letter} took too long")
        return False
    except KeyboardInterrupt:
        print(f"\nInterrupted during index {letter}")
        raise


def scrape_all_index_pages(delay: int = 30) -> None:
    """Scrape all index pages (09, A-Z)."""
    print(f"Scraping {len(INDEX_LETTERS)} index pages...")

    completed = []
    failed = []

    try:
        for i, letter in enumerate(INDEX_LETTERS):
            print(f"\n[{i + 1}/{len(INDEX_LETTERS)}] Index: {letter}")

            success = scrape_index(letter)

            if success:
                completed.append(letter)
            else:
                failed.append(letter)

            # Delay between index pages
            if i < len(INDEX_LETTERS) - 1:
                print(f"Waiting {delay}s before next index...")
                time.sleep(delay)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")

    print(f"\n{'='*60}")
    print("INDEX SCRAPING SUMMARY")
    print(f"{'='*60}")
    print(f"Completed: {len(completed)}")
    print(f"Failed: {len(failed)}")
    if failed:
        print(f"Failed indices: {', '.join(failed)}")


def get_all_manufacturers() -> list[str]:
    """Extract all manufacturer names from saved HTML files."""
    html_dir = Path("data/airport_data_screenshots/html")

    if not html_dir.exists():
        print("ERROR: No HTML directory found.")
        print("Run this first: uv run python scripts/scrape_all_airport_data.py --scrape-index")
        sys.exit(1)

    index_files = list(html_dir.glob("manufacturers/index_*_*.html"))

    if not index_files:
        print("ERROR: No index HTML files found.")
        print("Run this first: uv run python scripts/scrape_all_airport_data.py --scrape-index")
        sys.exit(1)

    manufacturers: set[str] = set()

    for html_file in index_files:
        try:
            html = html_file.read_text(encoding="utf-8")
            # Extract manufacturer slugs from links: /manuf/ManufacturerName.html
            matches = re.findall(r'/manuf/([^.]+)\.html', html)
            for m in matches:
                # Skip index letters
                if m not in INDEX_LETTERS:
                    manufacturers.add(m)
        except Exception as e:
            print(f"Warning: Could not read {html_file}: {e}")

    # Sort alphabetically
    return sorted(manufacturers)


def scrape_manufacturer(manufacturer: str, start_page: int = 1) -> bool:
    """Scrape a single manufacturer."""
    cmd = [
        "uv", "run", "python", "src/scraper_main.py",
        "--local", "--scrapers", "airport_data",
        "--task", manufacturer,
    ]
    if start_page > 1:
        cmd.extend(["--start-page", str(start_page)])

    print(f"\n{'='*60}")
    print(f"Scraping: {manufacturer}")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*60}\n")

    try:
        result = subprocess.run(cmd, timeout=7200)  # 2 hour timeout per manufacturer
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT: {manufacturer} took too long")
        return False
    except KeyboardInterrupt:
        print(f"\nInterrupted during {manufacturer}")
        raise


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Scrape all manufacturers from airport-data.com"
    )
    parser.add_argument(
        "--scrape-index", action="store_true",
        help="Scrape all index pages (09, A-Z) to get manufacturer list"
    )
    parser.add_argument(
        "--index", type=str,
        help="Scrape a specific index page (09, A, B, ..., Z)"
    )
    parser.add_argument(
        "--start-index", type=int, default=0,
        help="Start from this manufacturer index (0-based)"
    )
    parser.add_argument(
        "--manufacturers", nargs="+",
        help="Specific manufacturers to scrape (instead of all)"
    )
    parser.add_argument(
        "--delay", type=int, default=60,
        help="Delay between manufacturers in seconds (default: 60)"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Just list all manufacturers without scraping"
    )
    args = parser.parse_args()

    # Scrape specific index page
    if args.index:
        if args.index not in INDEX_LETTERS:
            print(f"ERROR: Invalid index. Must be one of: {', '.join(INDEX_LETTERS)}")
            sys.exit(1)
        scrape_index(args.index)
        return

    # Scrape all index pages
    if args.scrape_index:
        scrape_all_index_pages(delay=args.delay)
        return

    # Get manufacturers
    if args.manufacturers:
        manufacturers = args.manufacturers
    else:
        manufacturers = get_all_manufacturers()

    print(f"Found {len(manufacturers)} manufacturers")

    if args.list:
        for i, m in enumerate(manufacturers):
            print(f"{i:4d}: {m}")
        return

    # Apply start index
    if args.start_index > 0:
        manufacturers = manufacturers[args.start_index:]
        print(f"Starting from index {args.start_index}, {len(manufacturers)} manufacturers remaining")

    # Track progress
    completed = []
    failed = []

    try:
        for i, manufacturer in enumerate(manufacturers):
            actual_index = i + args.start_index
            print(f"\n[{actual_index + 1}/{len(manufacturers) + args.start_index}] {manufacturer}")

            success = scrape_manufacturer(manufacturer)

            if success:
                completed.append(manufacturer)
            else:
                failed.append(manufacturer)

            # Delay between manufacturers
            if i < len(manufacturers) - 1:
                print(f"Waiting {args.delay}s before next manufacturer...")
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Completed: {len(completed)}")
    print(f"Failed: {len(failed)}")
    if failed:
        print(f"Failed manufacturers: {', '.join(failed[:20])}")
        if len(failed) > 20:
            print(f"  ... and {len(failed) - 20} more")


if __name__ == "__main__":
    main()
