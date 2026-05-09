#!/usr/bin/env python3
"""Seed a handful of scraper tasks into the local queue for dev/testing.

Usage:
    STAGE=local python scripts/seed-tasks.py
    STAGE=local python scripts/seed-tasks.py --airport JFK
    STAGE=local python scripts/seed-tasks.py --registration N703PA

The worker (started via `./scripts/start-all.sh`) will pick these up
automatically on its next poll (default ~5 s).

task_key format per scraper type:
    airport_data   aircraft:<REG>        e.g. aircraft:N703PA
    jetphotos      <REG>                 e.g. N703PA
    fr24_airport   <IATA>                e.g. JFK
    fr24_aircraft  <REG>                 e.g. D-AIXA
    fr24_map       any string            payload: lat / lon / zoom required
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registration",
        default=None,
        help="Aircraft registration (e.g. N703PA). Seeds airport_data + jetphotos.",
    )
    parser.add_argument(
        "--airport",
        default=None,
        help="IATA airport code (e.g. JFK). Seeds fr24_airport.",
    )
    parser.add_argument(
        "--map-lat",
        type=float,
        default=None,
        help="Latitude for fr24_map task (pair with --map-lon).",
    )
    parser.add_argument(
        "--map-lon",
        type=float,
        default=None,
        help="Longitude for fr24_map task.",
    )
    parser.add_argument(
        "--map-zoom",
        type=int,
        default=8,
        help="Zoom level for fr24_map (default: 8).",
    )
    parser.add_argument(
        "--db",
        default="sqlite:///aircraft_data.db",
        help="Database URL (default: local SQLite).",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.scraper.task_queue import TaskQueue

    q = TaskQueue(args.db)
    q.ensure_tables_exist()

    # Default bundle when no specific args: a small sample across all scrapers.
    seed: list[tuple[str, str, dict]] = []
    if args.registration:
        reg = args.registration.strip().upper()
        seed.append(("airport_data", f"aircraft:{reg}", {}))
        seed.append(
            (
                "jetphotos",
                reg,
                {"max_pages": 1, "max_images_per_aircraft": 1},
            )
        )
        seed.append(("fr24_aircraft", reg, {}))
    if args.airport:
        apt = args.airport.strip().upper()
        seed.append(("fr24_airport", apt, {"max_clicks": 0}))
    if args.map_lat is not None and args.map_lon is not None:
        seed.append(
            (
                "fr24_map",
                f"{args.map_lat},{args.map_lon}",
                {"lat": args.map_lat, "lon": args.map_lon, "zoom": args.map_zoom},
            )
        )
    if not seed:
        # Default demo bundle — covers every scraper type once.
        seed = [
            ("airport_data", "aircraft:N703PA", {}),
            ("jetphotos", "N703PA", {"max_pages": 1, "max_images_per_aircraft": 1}),
            ("fr24_airport", "JFK", {"max_clicks": 0}),
            ("fr24_aircraft", "D-AIXA", {}),
            ("fr24_map", "nyc", {"lat": 40.64, "lon": -73.78, "zoom": 8}),
        ]

    added = 0
    for task_type, task_key, payload in seed:
        try:
            tid = q.add_task(task_type, task_key, payload=payload)
            if tid:
                added += 1
                print(f"  queued  {task_type:<15s}  {task_key:<22s}  task_id={tid}")
            else:
                print(f"  skipped {task_type:<15s}  {task_key:<22s}  (duplicate?)")
        except Exception as e:
            print(f"  ERROR   {task_type:<15s}  {task_key:<22s}  {e}")

    print(f"\nTotal queued: {added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
