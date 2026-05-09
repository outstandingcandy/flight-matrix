#!/usr/bin/env bash
#
# Flight Matrix — end-to-end local demo.
#
# Spins up the full scraper pipeline against a throwaway local SQLite
# database, seeds a few tasks, waits for them to complete, and prints
# what got written to disk + DB. Good for:
#
#   - First-time contributors: proves the stack works without AWS.
#   - Regression checks: run after any scraper/config change.
#   - Demo-ing the project to someone.
#
# What it does:
#   1. Stop any running services (clean slate)
#   2. Verify .env.local exists (suggest quickstart.sh if not)
#   3. Optional: reset the local SQLite DB (--reset)
#   4. Start Xvfb + Scraper worker (Track is skipped — needs API key)
#   5. Seed tasks via scripts/seed-tasks.py
#   6. Wait for them to be processed (progress bar every 10 s)
#   7. Print the final DB row counts + downloaded files
#   8. Leave services running unless --stop-when-done
#
# Usage:
#   ./scripts/demo.sh                            # default demo
#   ./scripts/demo.sh --reset                    # wipe local DB first
#   ./scripts/demo.sh --registration B-1020      # one specific aircraft
#   ./scripts/demo.sh --airport LAX              # one specific airport
#   ./scripts/demo.sh --wait 60                  # wait N seconds (default 120)
#   ./scripts/demo.sh --stop-when-done           # shut down scraper at the end
#   ./scripts/demo.sh --help

set -euo pipefail

if [[ -t 1 ]]; then
    RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m' BLUE=$'\033[34m' CYAN=$'\033[36m'
    BOLD=$'\033[1m' DIM=$'\033[2m' RESET=$'\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' DIM='' RESET=''
fi

step() { printf "${BLUE}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
ok()   { printf "    ${GREEN}✓${RESET} %s\n" "$*"; }
warn() { printf "    ${YELLOW}!${RESET} %s\n" "$*"; }
fail() { printf "    ${RED}✗${RESET} %s\n" "$*" >&2; }
die()  { fail "$*"; exit 1; }
hr()   { printf "${DIM}%s${RESET}\n" "────────────────────────────────────────────────────────────"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# ---- Defaults --------------------------------------------------------------
RESET_DB=0
STOP_WHEN_DONE=0
WAIT_SECS=120
SEED_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --reset)             RESET_DB=1 ;;
        --stop-when-done)    STOP_WHEN_DONE=1 ;;
        --wait)              WAIT_SECS="$2"; shift ;;
        --registration)      SEED_ARGS+=(--registration "$2"); shift ;;
        --airport)           SEED_ARGS+=(--airport "$2"); shift ;;
        --map-lat)           SEED_ARGS+=(--map-lat "$2"); shift ;;
        --map-lon)           SEED_ARGS+=(--map-lon "$2"); shift ;;
        -h|--help)
            sed -n 's/^# //;s/^#//;/^Usage/,/^$/p' "$0" | head -30
            exit 0
            ;;
        *) die "unknown flag: $1 (use --help)" ;;
    esac
    shift
done

# ---- Prereq check ----------------------------------------------------------
step "Checking local dev environment"
[[ -x .venv/bin/python3 ]] \
    || die ".venv missing. Run ./scripts/quickstart.sh first."
if [[ ! -f .env.local ]]; then
    die ".env.local missing. Run ./scripts/quickstart.sh first."
fi
grep -q "^STAGE=local" .env.local \
    || warn ".env.local doesn't set STAGE=local — demo will use shell STAGE"
ok "ready"

# ---- Clean slate -----------------------------------------------------------
step "Stopping any running services"
./scripts/start-all.sh --stop 2>&1 | sed 's/^/    /'

if (( RESET_DB )); then
    step "Resetting local SQLite database"
    rm -f aircraft_data.db
    ok "deleted aircraft_data.db"
fi

# Bootstrap schema (idempotent; creates DB if missing).
STAGE=local .venv/bin/python3 -c "
from src.data.db_manager import DatabaseManager
dm = DatabaseManager('sqlite:///aircraft_data.db')
dm.ensure_multi_user_tables_exist()
dm.close()
" 2>&1 | grep -vE "INFO|dotenv" || true

# ---- Record baseline -------------------------------------------------------
before=$(STAGE=local .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('aircraft_data.db')
counts = {}
for t in ['aircraft_static_info','aircraft_images','aircraft_snapshots',
         'aircraft_realtime_positions','flight_schedules','scraper_results']:
    counts[t] = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
print(counts)
")
images_before=$(ls data/jetphotos_images/*.jpg 2>/dev/null | wc -l)

# ---- Start services --------------------------------------------------------
step "Starting Xvfb + Scraper worker (Track skipped)"
STAGE=local ./scripts/start-all.sh --no-track 2>&1 | grep -E "started|already|✗" | sed 's/^/    /'

# ---- Seed tasks ------------------------------------------------------------
step "Seeding scraper tasks"
STAGE=local .venv/bin/python3 scripts/seed-tasks.py "${SEED_ARGS[@]}" 2>&1 | sed 's/^/    /'

# ---- Wait --------------------------------------------------------------------
step "Waiting up to ${WAIT_SECS}s for tasks to complete"
elapsed=0
interval=10
while (( elapsed < WAIT_SECS )); do
    sleep "$interval"
    elapsed=$((elapsed + interval))
    summary=$(STAGE=local .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('aircraft_data.db')
rows = c.execute('SELECT status, COUNT(*) FROM scraper_tasks GROUP BY status').fetchall()
parts = [f'{s}={n}' for s, n in rows]
print(' '.join(parts) or '(no tasks)')
")
    printf "    ${DIM}[%3ds]${RESET}  %s\n" "$elapsed" "$summary"
    if [[ "$summary" != *"pending"* ]] && [[ "$summary" != *"processing"* ]] && [[ "$summary" != *"claimed"* ]]; then
        ok "all tasks finished"
        break
    fi
done

# ---- Report ----------------------------------------------------------------
hr
printf "${BOLD}Results${RESET}\n"
hr

STAGE=local .venv/bin/python3 <<PY
import ast, sqlite3
before = $before
c = sqlite3.connect('aircraft_data.db')

def show(table, desc):
    n = c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    delta = n - before.get(table, 0)
    arrow = f'  (+{delta})' if delta > 0 else ''
    print(f'  {table:32s} {n:5d}{arrow}  {desc}')

print()
print('Data tables:')
show('aircraft_static_info', '飞机静态元数据 (airport-data + jetphotos sync)')
show('aircraft_images',      '飞机图片元数据 (jetphotos)')
show('flight_schedules',     '航班时刻表 (fr24_airport)')
show('aircraft_realtime_positions', '实时位置 (fr24_map)')

print()
print('Bookkeeping:')
show('scraper_results', '爬虫执行日志')

print()
print('Task queue outcome:')
for row in c.execute('SELECT status, task_type, task_key, attempts, last_error FROM scraper_tasks ORDER BY id'):
    st, tt, tk, att, err = row
    err_str = (err or '').splitlines()[0][:60] if err else ''
    print(f'  {st:10s}  {tt:14s}  {tk:22s}  attempts={att}  {err_str}')

print()
print('Scraped rows (samples):')
samples = c.execute('''
    SELECT registration, manufacturer, model, owner
    FROM aircraft_static_info
    ORDER BY last_updated DESC
    LIMIT 3
''').fetchall()
for reg, mfg, model, owner in samples:
    print(f'  {reg:10s}  {mfg or "?"} {model or ""}  owner={owner}')
PY

hr
printf "${BOLD}Files on disk${RESET}\n"
hr

images_after=$(ls data/jetphotos_images/*.jpg 2>/dev/null | wc -l)
images_delta=$((images_after - images_before))
printf "  jetphotos images:  %d total" "$images_after"
(( images_delta > 0 )) && printf "  ${GREEN}(+%d new)${RESET}" "$images_delta"
printf "\n"

screenshots=$(ls data/airport_data_screenshots/screenshots/*.png 2>/dev/null | wc -l)
printf "  airport-data screenshots:  %d\n" "$screenshots"

if (( images_after > 0 )); then
    printf "\n  Sample JPG (most recent):\n"
    ls -lh data/jetphotos_images/*.jpg 2>/dev/null | tail -1 | sed 's/^/    /'
fi

# ---- Done ------------------------------------------------------------------
echo
if (( STOP_WHEN_DONE )); then
    step "Stopping services"
    ./scripts/start-all.sh --stop 2>&1 | sed 's/^/    /'
else
    step "Leaving services running"
    printf "  ${CYAN}./scripts/start-all.sh --status${RESET}  — see what's up\n"
    printf "  ${CYAN}./scripts/start-all.sh --tail${RESET}    — follow logs\n"
    printf "  ${CYAN}./scripts/start-all.sh --stop${RESET}    — shut down\n"
    printf "  ${CYAN}STAGE=local python scripts/seed-tasks.py --registration <REG>${RESET}\n"
    printf "                                          — queue more tasks\n"
fi
