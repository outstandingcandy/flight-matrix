#!/usr/bin/env bash
#
# Flight Matrix — one-command supervisor for every data-ingestion task.
#
# What it starts (each as a detached background process):
#
#   1. Xvfb on :55              — virtual display for non-headless Chromium
#   2. Track service            — polls ADS-B Exchange → writes aircraft_snapshots
#   3. Scraper worker(s)        — consumes the Postgres task queue, runs FR24,
#                                 JetPhotos, airport-data, Xiaohongshu, etc.
#
# Logs go to ./logs/<service>.log, PIDs to ./logs/<service>.pid. Use
# `--status` to see what's up, `--stop` to shut down everything, `--restart`
# to stop + start, `--tail` to follow all logs.
#
# Usage:
#   ./scripts/start-all.sh                    # start everything
#   ./scripts/start-all.sh --status           # show running services
#   ./scripts/start-all.sh --stop             # stop everything
#   ./scripts/start-all.sh --restart          # stop + start
#   ./scripts/start-all.sh --tail             # `tail -f` all log files
#   ./scripts/start-all.sh --scrapers fr24_airport,jetphotos
#                                             # override which scrapers to run
#   ./scripts/start-all.sh --no-track         # skip the Track service
#   ./scripts/start-all.sh --no-scraper       # skip the Scraper worker
#   ./scripts/start-all.sh --foreground SVC   # run one service attached
#                                             # SVC: xvfb | track | scraper
#
# Not started by this script (by design — they have different cadences):
#   - Report service (src/report_main.py)     — run as a cron job or from
#                                                Lambda, not continuously
#   - The Flask web app                       — use `uv run python web_app.py`
#                                                directly, or `./deploy.sh webapp`
#                                                for Lambda
#
# See docs/deployment.md for production ASG-based scraper workers.

set -euo pipefail

if [[ -t 1 ]]; then
    RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m' BLUE=$'\033[34m' CYAN=$'\033[36m'
    BOLD=$'\033[1m' RESET=$'\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' RESET=''
fi

step() { printf "${BLUE}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
ok()   { printf "    ${GREEN}✓${RESET} %s\n" "$*"; }
warn() { printf "    ${YELLOW}!${RESET} %s\n" "$*"; }
fail() { printf "    ${RED}✗${RESET} %s\n" "$*" >&2; }
die()  { fail "$*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

LOGS_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOGS_DIR"

# ---- Defaults --------------------------------------------------------------
DISPLAY_NUM=55
# Default scrapers: all that don't require interactive QR login or AWS secrets,
# AND don't require the optional resilient-scraper submodule.
# Xiaohongshu needs a manual QR scan; Planespotters lives in the submodule.
DEFAULT_SCRAPERS="fr24_airport,jetphotos,airport_data,fr24_map"

# Scrapers that live in the resilient-scraper submodule.
SUBMODULE_SCRAPERS_RE="planespotters|xiaohongshu|xiaohongshu_following|xiaohongshu_search_author"

# ---- Arg parsing -----------------------------------------------------------
ACTION=start
START_TRACK=1
START_SCRAPER=1
SCRAPERS="$DEFAULT_SCRAPERS"
FOREGROUND=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)     ACTION=status ;;
        --stop)       ACTION=stop ;;
        --restart)    ACTION=restart ;;
        --tail)       ACTION=tail ;;
        --no-track)   START_TRACK=0 ;;
        --no-scraper) START_SCRAPER=0 ;;
        --scrapers)   SCRAPERS="$2"; shift ;;
        --foreground) FOREGROUND="$2"; shift ;;
        -h|--help)
            sed -n 's/^# //;s/^#//;/^Usage/,/^See /p' "$0"
            exit 0
            ;;
        *) die "unknown flag: $1 (use --help)" ;;
    esac
    shift
done

# ---- PID / log helpers -----------------------------------------------------
pidfile() { printf "%s/%s.pid" "$LOGS_DIR" "$1"; }
logfile() { printf "%s/%s.log" "$LOGS_DIR" "$1"; }

is_running() {
    local pf; pf="$(pidfile "$1")"
    [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null
}

stop_one() {
    local name="$1" pf; pf="$(pidfile "$name")"
    if ! is_running "$name"; then
        # Clean up a stale pidfile if any, but don't let a missing file
        # bubble a non-zero exit up through `set -e`.
        [[ -f "$pf" ]] && rm -f "$pf" || true
        return 0
    fi
    local pid; pid="$(cat "$pf")"
    printf "    stopping %s (PID %s)..." "$name" "$pid"
    kill -TERM "$pid" 2>/dev/null || true
    # Give it up to 10 s to exit cleanly.
    for _ in $(seq 1 20); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pf"
            printf " ${GREEN}ok${RESET}\n"
            return
        fi
        sleep 0.5
    done
    printf " ${YELLOW}still alive, SIGKILL${RESET}\n"
    kill -KILL "$pid" 2>/dev/null || true
    rm -f "$pf"
}

start_bg() {
    # start_bg <name> <cmd...>
    local name="$1"; shift
    local lf pf
    lf="$(logfile "$name")"; pf="$(pidfile "$name")"
    if is_running "$name"; then
        ok "$name already running (PID $(cat "$pf"))"
        return
    fi
    # Rotate previous log (keep one backup).
    [[ -f "$lf" ]] && mv "$lf" "${lf}.1"
    # setsid so the child survives this shell; nohup keeps it alive after SSH close.
    nohup setsid "$@" >"$lf" 2>&1 &
    echo $! > "$pf"
    sleep 1
    if is_running "$name"; then
        ok "$name started (PID $(cat "$pf"), log: $lf)"
    else
        fail "$name failed to start — last log lines:"
        tail -20 "$lf" 2>/dev/null | sed 's/^/      /' >&2
        rm -f "$pf"
        return 1
    fi
}

# ---- Prereq checks ---------------------------------------------------------
check_prereqs() {
    [[ -x "$PROJECT_ROOT/.venv/bin/python3" ]] \
        || die ".venv missing. Run ./scripts/quickstart.sh first."
    # Accept any of the stage-aware or legacy env files.
    if [[ ! -f "$PROJECT_ROOT/.env.local" ]] \
       && [[ ! -f "$PROJECT_ROOT/.env.prod" ]] \
       && [[ ! -f "$PROJECT_ROOT/.env" ]]; then
        die "no env file found (.env.local / .env.prod / .env). Run ./scripts/quickstart.sh first."
    fi
    if (( START_SCRAPER )); then
        command -v Xvfb >/dev/null 2>&1 \
            || die "Xvfb not installed (apt install xvfb). Needed for scraper."
        command -v chromium-browser >/dev/null 2>&1 \
            || command -v google-chrome >/dev/null 2>&1 \
            || die "No Chromium/Chrome found. apt install chromium-browser."

        # If any of the requested scrapers live in the submodule, verify the
        # module is installed before starting the worker.
        if [[ "$SCRAPERS" =~ $SUBMODULE_SCRAPERS_RE ]]; then
            if ! "$PROJECT_ROOT/.venv/bin/python3" -c "import resilient_scraper" 2>/dev/null; then
                fail "one of the requested scrapers needs the resilient-scraper submodule"
                fail "  requested: $SCRAPERS"
                echo
                echo "    Install with:" >&2
                echo "      git submodule update --init --recursive" >&2
                echo "      uv pip install -e ./lib/resilient-scraper" >&2
                echo "    Or, to skip those scrapers for now:" >&2
                echo "      $0 --scrapers fr24_airport,jetphotos,airport_data,fr24_map" >&2
                exit 1
            fi
        fi
    fi
}

# ---- Service runners -------------------------------------------------------
start_xvfb() {
    if [[ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
        ok "Xvfb :${DISPLAY_NUM} already listening"
        # Still record the PID if we can find it, so --stop can clean up.
        if ! is_running xvfb; then
            local existing
            existing=$(pgrep -f "Xvfb :${DISPLAY_NUM}\b" | head -1)
            [[ -n "$existing" ]] && echo "$existing" > "$(pidfile xvfb)"
        fi
        return
    fi
    start_bg xvfb Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24
}

start_track() {
    start_bg track \
        env DISPLAY=":${DISPLAY_NUM}" \
        "$PROJECT_ROOT/.venv/bin/python3" -u src/track_main.py --config config/config.yaml
}

start_scraper() {
    local worker_id="local-$(hostname -s 2>/dev/null || echo host)-$$"
    # --scrapers accepts nargs='+' (space-separated), but we take comma-separated
    # on the command line. Split on commas so each scraper becomes its own argv.
    local IFS=','
    local -a scraper_args=($SCRAPERS)
    start_bg scraper \
        env DISPLAY=":${DISPLAY_NUM}" \
        "$PROJECT_ROOT/.venv/bin/python3" -u src/scraper_main.py \
            --config config/config.yaml \
            --scrapers "${scraper_args[@]}" \
            --worker-id "$worker_id"
}

# ---- Actions ---------------------------------------------------------------
action_start() {
    check_prereqs

    step "Starting Xvfb"
    start_xvfb

    if (( START_TRACK )); then
        step "Starting Track service"
        start_track
    else
        warn "skipping Track (--no-track)"
    fi

    if (( START_SCRAPER )); then
        step "Starting Scraper worker [$SCRAPERS]"
        start_scraper
    else
        warn "skipping Scraper (--no-scraper)"
    fi

    echo
    printf "${BOLD}Running services:${RESET}\n"
    action_status
    echo
    printf "Tail all logs with: ${CYAN}%s --tail${RESET}\n" "$0"
    printf "Stop everything:    ${CYAN}%s --stop${RESET}\n" "$0"
}

action_status() {
    local any=0
    for name in xvfb track scraper; do
        if is_running "$name"; then
            any=1
            local pid; pid=$(cat "$(pidfile "$name")")
            local started; started=$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//;s/ *$//')
            printf "    ${GREEN}●${RESET} %-9s  PID %-8s  started %s\n" "$name" "$pid" "${started:-?}"
        else
            printf "    ${RED}○${RESET} %-9s  not running\n" "$name"
        fi
    done
    (( any )) || printf "    (nothing running)\n"
}

action_stop() {
    step "Stopping services"
    # Reverse order: scraper depends on Xvfb; stop it first.
    for name in scraper track xvfb; do
        stop_one "$name"
    done
    ok "all stopped"
}

action_tail() {
    local files=()
    for name in xvfb track scraper; do
        local lf; lf="$(logfile "$name")"
        [[ -f "$lf" ]] && files+=("$lf")
    done
    if (( ${#files[@]} == 0 )); then
        die "no log files found in $LOGS_DIR"
    fi
    exec tail -F "${files[@]}"
}

action_foreground() {
    check_prereqs
    case "$FOREGROUND" in
        xvfb)
            exec Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24
            ;;
        track)
            # Need Xvfb up for anything using the browser pool, but Track alone
            # doesn't use it. Still exporting DISPLAY is harmless.
            exec env DISPLAY=":${DISPLAY_NUM}" \
                "$PROJECT_ROOT/.venv/bin/python3" -u src/track_main.py \
                --config config/config.yaml
            ;;
        scraper)
            # Make sure Xvfb is up (but don't track it).
            [[ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]] \
                || die "Xvfb :${DISPLAY_NUM} not running. Start it first or run without --foreground."
            local IFS=','
            local -a scraper_args=($SCRAPERS)
            exec env DISPLAY=":${DISPLAY_NUM}" \
                "$PROJECT_ROOT/.venv/bin/python3" -u src/scraper_main.py \
                --config config/config.yaml \
                --scrapers "${scraper_args[@]}" \
                --debug
            ;;
        *)
            die "unknown --foreground service: $FOREGROUND (use: xvfb | track | scraper)"
            ;;
    esac
}

# ---- Dispatch --------------------------------------------------------------
if [[ -n $FOREGROUND ]]; then
    action_foreground
fi

case "$ACTION" in
    start)   action_start ;;
    status)  action_status ;;
    stop)    action_stop ;;
    restart) action_stop; action_start ;;
    tail)    action_tail ;;
    *)       die "unknown action: $ACTION" ;;
esac
