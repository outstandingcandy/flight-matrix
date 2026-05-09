#!/usr/bin/env bash
#
# Quick smoke test for the project's core scrapers, using the PRODUCTION
# config (non-headless DrissionPage under an Xvfb virtual display).
#
# Why this script exists: it's really easy to write a one-off Python
# snippet that forces `headless=True`, get Cloudflare blocks, and
# mis-conclude the scraper is broken. This script uses the same
# parameters that `scraper_main.py` uses in production, so a green run
# here means the code path really is fine.
#
# Usage:
#   ./scripts/test-scrapers.sh            # run all three
#   ./scripts/test-scrapers.sh airport    # just airport-data.com
#   ./scripts/test-scrapers.sh jetphotos  # just JetPhotos
#   ./scripts/test-scrapers.sh fr24       # just FR24 arrivals

set -euo pipefail

if [[ -t 1 ]]; then
    GREEN=$'\033[32m' RED=$'\033[31m' BLUE=$'\033[34m' BOLD=$'\033[1m' RESET=$'\033[0m'
else
    GREEN='' RED='' BLUE='' BOLD='' RESET=''
fi

step() { printf "${BLUE}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
ok()   { printf "    ${GREEN}✓${RESET} %s\n" "$*"; }
die()  { printf "    ${RED}✗${RESET} %s\n" "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

WHICH="${1:-all}"
case "$WHICH" in
    all|airport|jetphotos|fr24) ;;
    *) die "unknown arg: $WHICH (use: all | airport | jetphotos | fr24)" ;;
esac

# ---- Prereqs --------------------------------------------------------------
step "Checking prerequisites"
for tool in Xvfb python3; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found (apt install xvfb chromium-browser)"
done
command -v chromium-browser >/dev/null 2>&1 || command -v google-chrome >/dev/null 2>&1 \
    || die "no chromium/chrome browser found"
[[ -x .venv/bin/python3 ]] || die "run ./scripts/quickstart.sh first (no .venv)"
ok "prereqs ok"

# ---- Xvfb -----------------------------------------------------------------
DISPLAY_NUM=55
if ! [[ -e /tmp/.X11-unix/X${DISPLAY_NUM} ]]; then
    step "Starting Xvfb :${DISPLAY_NUM}"
    Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 > /tmp/xvfb.log 2>&1 &
    sleep 2
    ok "Xvfb started (PID $!)"
else
    ok "Xvfb :${DISPLAY_NUM} already running"
fi
export DISPLAY=":${DISPLAY_NUM}"

# ---- Runner ---------------------------------------------------------------
run_test() {
    local name="$1" script="$2"
    step "Running $name"
    if .venv/bin/python3 -c "$script"; then
        ok "$name passed"
        return 0
    else
        printf "    ${RED}✗${RESET} $name FAILED\n" >&2
        return 1
    fi
}

FAILURES=0

# airport-data.com (no Cloudflare)
if [[ $WHICH == "all" || $WHICH == "airport" ]]; then
    run_test "airport-data.com" '
import sys, time
sys.path.insert(0, ".")
from src.scraper.browser_pool import BrowserPool
from src.scraper.scrapers.airport_data import AirportDataScraper
from src.scraper.models import ScraperTask
scraper = AirportDataScraper({"sync_to_database": False, "s3_upload": False})
scraper.setup()
pool = BrowserPool(pool_size=1, drission_options={"headless": False})
pool.initialize()
browser = pool.acquire(timeout=60)
try:
    t0 = time.time()
    result = scraper.scrape(ScraperTask(task_type="airport_data", task_key="aircraft:N703PA"), browser=browser)
    elapsed = time.time() - t0
    print(f"    {elapsed:.1f}s  aircraft_count={result.aircraft_count}")
    assert result.aircraft_count == 1, "expected 1 aircraft"
    print(f"    manufacturer={result.aircraft[0].manufacturer} model={result.aircraft[0].model}")
finally:
    pool.release(browser); pool.shutdown(); scraper.teardown()
' || FAILURES=$((FAILURES + 1))
fi

# JetPhotos (Cloudflare-protected)
if [[ $WHICH == "all" || $WHICH == "jetphotos" ]]; then
    run_test "JetPhotos (cloudflare-protected)" '
import sys, time
sys.path.insert(0, ".")
from src.scraper.browser_pool import BrowserPool
from src.scraper.scrapers.jetphotos import JetPhotosScraper
from src.scraper.models import ScraperTask
scraper = JetPhotosScraper({"max_pages": 1, "max_images_per_aircraft": 1,
                            "download_all_images": False, "images_dir": "/tmp/jp-test",
                            "parallel_workers": 1, "delay_min": 0.5, "delay_max": 1.0,
                            "s3_upload": False, "sync_to_database": False})
scraper.setup()
pool = BrowserPool(pool_size=1, drission_options={"headless": False})
pool.initialize()
browser = pool.acquire(timeout=60)
try:
    t0 = time.time()
    result = scraper.scrape(ScraperTask(task_type="jetphotos", task_key="N703PA"), browser=browser)
    elapsed = time.time() - t0
    imgs = getattr(result, "images", []) or []
    print(f"    {elapsed:.1f}s  images={len(imgs)}")
    assert len(imgs) > 0, "expected at least one photo"
finally:
    pool.release(browser); pool.shutdown(); scraper.teardown()
' || FAILURES=$((FAILURES + 1))
fi

# FR24 Airport Arrivals (Cloudflare-protected)
if [[ $WHICH == "all" || $WHICH == "fr24" ]]; then
    run_test "FR24 Airport Arrivals (cloudflare-protected)" '
import sys, time
sys.path.insert(0, ".")
from src.scraper.browser_pool import BrowserPool
from src.scraper.scrapers.fr24_airport import FR24AirportArrivalsScraper
from src.scraper.models import ScraperTask
scraper = FR24AirportArrivalsScraper({"max_load_more_clicks": 0, "sync_to_database": False})
scraper.setup()
pool = BrowserPool(pool_size=1, drission_options={"headless": False})
pool.initialize()
browser = pool.acquire(timeout=60)
try:
    t0 = time.time()
    result = scraper.scrape(ScraperTask(task_type="fr24_arrivals", task_key="JFK"), browser=browser)
    elapsed = time.time() - t0
    flights = getattr(result, "flights", []) or []
    print(f"    {elapsed:.1f}s  airport={result.airport_name}  flights={len(flights)}")
    assert len(flights) >= 10, f"expected a lot of JFK flights, got {len(flights)}"
finally:
    pool.release(browser); pool.shutdown(); scraper.teardown()
' || FAILURES=$((FAILURES + 1))
fi

# ---- Result ---------------------------------------------------------------
echo
if (( FAILURES == 0 )); then
    printf "${BOLD}${GREEN}All scraper smokes passed.${RESET}\n"
    exit 0
else
    printf "${BOLD}${RED}$FAILURES scraper smoke(s) failed.${RESET}\n" >&2
    echo "If this is JetPhotos or FR24: verify Xvfb is actually running under" >&2
    echo "DISPLAY=:${DISPLAY_NUM} and DrissionPage is using a real (non-headless)" >&2
    echo "browser. See src/scraper/CLAUDE.md for the expected config." >&2
    exit 1
fi
