# src/scraper/

Flight-matrix scraper glue. The underlying framework (base class,
`BrowserPool`, `Worker`, browser lifecycle, login/alert/Cloudflare handling)
lives in the `resilient_scraper` submodule at `lib/resilient-scraper/`.

This package owns the flight-matrix-specific parts:

- `task_queue.py` — sync Postgres + SQLite-dialect-aware queue used by CLI
  helpers (`seed-tasks.py`, `scraper_main --status/--populate`).
- `async_task_queue.py` / `cli_task_queue.py` / `local_task_queue.py` —
  async adapters satisfying `resilient_scraper.queue.TaskQueue`; these are
  what the submodule Worker actually consumes.
- `sinks/` — flight-matrix DB writes. Bound onto each aviation scraper at
  registration time so the scraper stays persistence-agnostic. `JetPhotosSink`
  additionally owns *all* storage for that scraper: the thumbnails it derives
  (`src.media.thumbnails`) and, via the `upload_callback` config key, the images
  and saved page HTML the scraper itself writes. All of it goes through
  `ObjectStorage`, so it behaves the same on `aws`, `gcp` and `local`. The
  submodule keeps a boto3 fallback for callers that inject no callback; the
  other scrapers' uploads (`airport_data`, `planespotters`, `xiaohongshu`,
  `ebay`) still take it and so remain AWS-only.
- `sources/` — domain-table pollers (aircraft_static_info etc.) used by
  `LocalTaskQueue` for local-mode scraping.
- `task_scheduler.py` / `xiaohongshu_cycle_scheduler.py` — standalone
  scheduler daemons that shape task workflows.

## Critical: how to test scrapers locally

**Do not instantiate `BrowserPool` with `headless=True` when testing a
Cloudflare-protected scraper.** The anti-anti-scraping strategy relies on
DrissionPage running in a real (non-headless) browser window backed by an
Xvfb virtual display. Forcing headless mode makes Cloudflare reject the
request and produces `CloudflareBlockedError` / a `"Just a moment..."` page
that looks like a real bug but is a test-harness mistake.

Production config (see `config/scraper/base.yaml`):
```
drission_page.headless: false
xvfb.auto_start: true
xvfb.display_base: 55
```

To test a scraper locally:

```bash
# 1. Start Xvfb once (prod uses a systemd unit; dev just starts it ad-hoc)
Xvfb :55 -screen 0 1920x1080x24 > /tmp/xvfb.log 2>&1 &

# 2. Run the submodule Worker against a single CLI-provided task
DISPLAY=:55 uv run python -m src.scraper_main \
    --local --task N703PA --scrapers jetphotos
```

`scripts/test-scrapers.sh` wraps this for the three core sources.

## Which scrapers need what

| Scraper                   | cloudflare_protected | Needs login | Typical time |
|---------------------------|---------------------:|:-----------:|-------------:|
| `airport_data`            | no                   | no          | ~10 s        |
| `jetphotos`               | yes                  | no          | ~30 s / tail |
| `fr24_airport`            | yes                  | no          | ~60 s / IATA |
| `fr24_map`                | yes                  | no          | ~30 s        |
| `fr24_aircraft`           | yes                  | no          | ~20 s / hex  |
| `xiaohongshu*`            | yes                  | yes (QR)    | depends      |
| `planespotters`           | yes                  | no          | ~20 s / tail |

All concrete scrapers live under `lib/resilient-scraper/src/resilient_scraper/scrapers/`.

`cloudflare_protected: True` scrapers call `ResilientScraper.handle_cloudflare()`
which inspects the page title for "Just a moment…", "Ray ID:", etc. and waits
up to 180 s. If the wait expires, `CloudflareBlockedError` is raised. Check
Xvfb / headless first before assuming the site is actually blocking you.

## Task flow

```
TaskScheduler ── writes tasks ─▶ scraper_tasks
                                     │
                                     ▼
AsyncTaskQueue ◀── submodule Worker ──▶ ResilientScraper[T]
 (main-repo sync       │                    │
  TaskQueue wrapped    │                    ▼
  for async await)     ▼              BrowserPool
                   Sink.on_success ◀── result returned
                   (main-repo writes to aircraft_static_info,
                   aircraft_images, flight_schedules, etc.)
```

Key invariants:

- Each `ResilientScraper` subclass declares `task_type` (string key used by the
  task queue) and `requires_browser` (gates whether the worker hands it one).
  Don't change `task_type` — it's a persisted contract.
- `scrape(task, browser)` must be idempotent. Retries happen.
- Raise `NoDataFoundError`, `PageLoadError`, `CloudflareBlockedError`, or
  generic `ScraperError(retryable=…)` — do not swallow exceptions. The worker
  decides whether to retry based on the exception class.

## Extending

For a new aviation source, drop a new package under
`lib/resilient-scraper/src/resilient_scraper/scrapers/aviation/<name>/`:

```python
from resilient_scraper.scraper import ResilientScraper
from resilient_scraper.models import ScraperTask


class MyScraper(ResilientScraper[MyResult]):
    task_type = "my_source"
    requires_browser = True
    cloudflare_protected = False  # flip to True if applicable

    def validate_task(self, task: ScraperTask) -> bool: ...
    def build_url(self, task: ScraperTask) -> str: ...
    def scrape(self, task, browser): ...
```

Register it in `src/scraper_main.py::_build_scraper_configs()`, add a sink in
`src/scraper/sinks/` if it needs DB writes, and wire the sink into
`_build_sinks_and_augment_configs()`.
