# Scraping tasks

How to configure which data gets scraped, how often, and how to enqueue
ad-hoc jobs for development. For the scraper framework's architecture
(workers, browser pool, task queue) see
[architecture.md](architecture.md).

## Three layers

1. **Which sources are enabled** — `config/scraper/*.yaml`.
2. **How often each source runs + coverage** — per-source YAML keys
   (`min_cycle_gap`, `airport_groups`, `regions`, `top_count`, …).
3. **Which specific tasks are in the queue** — rows in the
   `scraper_tasks` table, written either by the scheduler (production)
   or by `scripts/seed-tasks.py` (development).

## 1. Source configuration

```
config/scraper/
├── base.yaml          # worker / browser pool / queue / scheduler defaults
├── fr24.yaml          # fr24_airport, fr24_map, fr24_aircraft
├── jetphotos.yaml     # jetphotos + image download
├── other.yaml         # airport_data, planespotters
└── xiaohongshu.yaml   # Xiaohongshu (needs resilient-scraper submodule)
```

Enable / disable a source with `scraper.scrapers.<name>.enabled`. Commonly
edited keys:

| Key                                           | File             | Effect                                              |
|-----------------------------------------------|------------------|-----------------------------------------------------|
| `scraper.worker.task_timeout`                 | base.yaml        | Per-task timeout (seconds)                          |
| `scraper.browser_pool.size`                   | base.yaml        | Parallel browsers per worker                        |
| `scraper.task_queue.max_attempts`             | base.yaml        | Retries before a task is marked failed              |
| `fr24_airport.airport_groups`                 | fr24.yaml        | Airport tiers + their `priority` / `min_cycle_gap`  |
| `fr24_map.global_coverage` / `regions`        | fr24.yaml        | Built-in global regions vs. custom lat/lon boxes    |
| `fr24_aircraft.top_count`                     | fr24.yaml        | How many top-interest aircraft to scrape histories  |
| `image_download.max_images_per_aircraft`      | jetphotos.yaml   | Cap per tail number                                 |
| `image_download.s3.enabled`                   | jetphotos.yaml   | Upload to S3 (disable locally)                      |

After editing, restart the worker:

```bash
./scripts/start-all.sh --restart
```

## 2. Enqueueing tasks

### A. Manual (development)

`scripts/seed-tasks.py` writes rows directly into `scraper_tasks`. The
running worker picks them up on its next poll (~5 s).

```bash
# Default demo bundle — one task per scraper type
STAGE=local python scripts/seed-tasks.py

# Single aircraft (seeds airport_data + jetphotos + fr24_aircraft)
STAGE=local python scripts/seed-tasks.py --registration N703PA

# Single airport (fr24_airport)
STAGE=local python scripts/seed-tasks.py --airport JFK

# Map region (fr24_map)
STAGE=local python scripts/seed-tasks.py --map-lat 40.6 --map-lon -73.8 --map-zoom 8
```

`task_key` format per scraper type:

| Scraper type      | `task_key`                | Payload                         |
|-------------------|---------------------------|---------------------------------|
| `airport_data`    | `aircraft:<REG>`          | (none)                          |
| `jetphotos`       | `<REG>`                   | `max_pages`, `max_images_per_aircraft` |
| `fr24_airport`    | `<IATA>`                  | `max_clicks`                    |
| `fr24_aircraft`   | `<REG>`                   | (none)                          |
| `fr24_map`        | any string                | `lat`, `lon`, `zoom` (required) |

### B. Automatic (production)

`src/scraper/task_scheduler.py` runs as its own service and writes tasks
based on the YAML: `airport_groups.min_cycle_gap` for fr24_airport,
`top_count` for fr24_aircraft, region lists for fr24_map, and so on. In
local dev the scheduler is not started by default — `seed-tasks.py`
covers the need.

## 3. Inspecting the queue

```bash
STAGE=local sqlite3 aircraft_data.db \
  "SELECT status, task_type, task_key, attempts, last_error
   FROM scraper_tasks ORDER BY id DESC LIMIT 20"
```

Statuses: `pending`, `claimed`, `processing`, `completed`, `failed`,
`skipped`. A task becomes `claimed` when a worker picks it up, then
`processing` once it starts, and finally `completed` or `failed`.

## 4. End-to-end demo

`scripts/demo.sh` wraps the whole loop — stop any running services,
bootstrap the SQLite schema, start Xvfb + scraper worker, seed tasks,
poll until complete, and print a before/after diff of rows and files on
disk.

```bash
./scripts/demo.sh                            # default demo bundle
./scripts/demo.sh --reset                    # wipe local DB first
./scripts/demo.sh --registration B-1020      # one specific aircraft
./scripts/demo.sh --airport LAX              # one specific airport
./scripts/demo.sh --wait 180                 # wait up to N seconds
./scripts/demo.sh --stop-when-done           # shut down worker at the end
```

By default services are left running so you can keep queueing more tasks
with `seed-tasks.py`.
