# src/scraper/

Distributed web-scraping framework. Browser pool, task queue, worker
lifecycle. Concrete sources live in `scrapers/` and `sources/`.

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

# 2. Run with DISPLAY=:55 and headless=False
DISPLAY=:55 python3 -c "
from src.scraper.browser_pool import BrowserPool
from src.scraper.scrapers.jetphotos import JetPhotosScraper
from src.scraper.models import ScraperTask

pool = BrowserPool(pool_size=1, drission_options={'headless': False})
pool.initialize()
browser = pool.acquire(timeout=60)
scraper = JetPhotosScraper({'max_pages': 1, 'max_images_per_aircraft': 1,
                            's3_upload': False, 'sync_to_database': False})
scraper.setup()
result = scraper.scrape(ScraperTask(task_type='jetphotos', task_key='N703PA'),
                        browser=browser)
print(result)
pool.release(browser); pool.shutdown(); scraper.teardown()
"
```

`scripts/test-scrapers.sh` wraps this for the three core sources.

## Which scrapers need what

| Scraper                   | cloudflare_protected | Needs login | Typical time |
|---------------------------|---------------------:|:-----------:|-------------:|
| `airport-data` (this pkg) | no                   | no          | ~10 s        |
| `jetphotos` (this pkg)    | yes                  | no          | ~30 s / tail |
| `fr24_airport`            | yes                  | no          | ~60 s / IATA |
| `fr24_map`                | yes                  | no          | ~30 s        |
| `fr24_aircraft`           | yes                  | no          | ~20 s / hex  |
| `xiaohongshu*` (submodule)| yes                  | yes (QR)    | depends      |
| `planespotters` (submodule)| yes                 | no          | ~20 s / tail |

`cloudflare_protected: True` scrapers call `BaseScraper.handle_cloudflare()`
which inspects the page title for "Just a moment…", "Ray ID:", etc. and
waits up to 180 s. If the wait expires, `CloudflareBlockedError` is
raised. Check Xvfb / headless first before assuming the site is actually
blocking you.

## Task flow

```
TaskScheduler ── produces tasks ──▶ TaskQueue (Postgres, SKIP LOCKED)
                                        │
                                        ▼
                               ScraperWorker ── dispatches ──▶ BaseScraper[T]
                                        │                         │
                                        ▼                         ▼
                                  BrowserPool ◀─── acquires ─── scrape(task)
```

Key invariants:

- Each `BaseScraper` subclass declares `task_type` (string key used by the
  task queue) and `requires_browser` (gates whether the worker hands it
  one). Don't change `task_type` — it's a persisted contract.
- `scrape(task, browser)` must be idempotent. Retries happen.
- Raise `NoDataFoundError`, `PageLoadError`, `CloudflareBlockedError`, or
  generic `ScraperError(retryable=…)` — do not swallow exceptions. The
  worker decides whether to retry based on the exception class.

## Extending

New source = new file under `scrapers/`:

```python
from src.scraper.base import BaseScraper
from src.scraper.models import ScraperTask

class MyScraper(BaseScraper[MyResult]):
    task_type = "my_source"
    requires_browser = True
    cloudflare_protected = False  # flip to True if applicable

    def validate_task(self, task: ScraperTask) -> bool: ...
    def build_url(self, task: ScraperTask) -> str: ...
    def scrape(self, task, browser): ...
```

Register it in `src/scraper_main.py::main()` when the service starts.
