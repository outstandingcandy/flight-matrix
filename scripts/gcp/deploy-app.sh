#!/bin/bash
#
# Ship the application to a provisioned GCE host and start the web service
#
# Runs after provision-existing-host.sh (shared host) or bootstrap-vm.sh
# (dedicated VM). Those two own the host: packages, swap, nginx, the TLS
# certificate, the database container, /etc/flight-matrix/env. This script owns
# only the application, so re-deploying code can never re-partition swap or
# re-issue a certificate.
#
# Code is shipped as a tar stream through `gcloud compute ssh` rather than with
# git or rsync:
#   - git clone would ship HEAD, and the deployment-target work is not committed
#     yet. What is on this disk is what needs to run.
#   - rsync would need the SSH endpoint and key plumbing that gcloud otherwise
#     handles, for a payload of about a megabyte compressed.
# The tradeoff is that extraction adds and overwrites but never deletes, so a
# file removed locally lingers on the host. Pass --clean to wipe the application
# directory first (the virtualenv and any local data are preserved).
#
# lib/resilient-scraper is included deliberately. web_app.py never imports it,
# but pyproject.toml declares it as a path dependency, so `uv sync` fails
# without it on disk.
#
# Usage:
#   ./scripts/gcp/deploy-app.sh --vm NAME [options]
#
# Options:
#   --vm NAME             Target GCE instance (required)
#   --vm-zone ZONE        Zone of the instance (default: us-west1-b)
#   --project ID          GCP project (default: gcloud config value)
#   --user NAME           OS user that owns and runs the app (default: the
#                         account gcloud ssh logs in as)
#   --app-dir PATH        Application directory (default: ~USER/flight-matrix)
#   --app-port PORT       Loopback port for gunicorn (default: 8000). Must match
#                         the upstream the provisioning script gave nginx.
#   --workers N           gunicorn worker processes (default: 2)
#   --threads N           Threads per worker (default: 4)
#   --memory-max SIZE     systemd MemoryMax for the unit (default: 2G)
#   --domain HOST         Hostname used for the post-deploy health check
#   --tls-port PORT       Port nginx serves on, for the health check (default: 8080)
#   --gcp-project ID      Value for GOOGLE_CLOUD_PROJECT (default: --project)
#   --gcs-bucket NAME     Public assets bucket -> GCS_ASSETS_BUCKET
#   --static-base-url URL Base URL images are served from -> STATIC_BASE_URL
#                         (default: the public URL of --gcs-bucket)
#   --clean               Wipe the application directory before extracting,
#                         keeping .venv and data/
#   --skip-deps           Do not run `uv sync` (code-only redeploy)
#   --skip-schema         Do not create database tables
#   --yes                 Skip confirmation prompts
#   --dry-run             Print what would happen, change nothing
#
# Examples:
#   ./scripts/gcp/deploy-app.sh --vm redpanda --vm-zone us-west1-b \
#       --user tangjiee --domain 136-109-216-214.nip.io --tls-port 8080 \
#       --gcs-bucket outstandingcandy-flight-matrix-assets
#
#   # Code-only redeploy after editing a template
#   ./scripts/gcp/deploy-app.sh --vm redpanda --user tangjiee \
#       --skip-deps --skip-schema --yes
#

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

VM_NAME=""
VM_ZONE="us-west1-b"
PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
APP_USER=""
APP_DIR=""
APP_PORT=8000
WORKERS=2
THREADS=4
MEMORY_MAX="2G"
DOMAIN=""
TLS_PORT=8080
GCP_PROJECT=""
GCS_BUCKET=""
STATIC_BASE_URL=""
CLEAN=false
SKIP_DEPS=false
SKIP_SCHEMA=false
ASSUME_YES=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --vm) VM_NAME="$2"; shift 2 ;;
        --vm-zone) VM_ZONE="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --user) APP_USER="$2"; shift 2 ;;
        --app-dir) APP_DIR="$2"; shift 2 ;;
        --app-port) APP_PORT="$2"; shift 2 ;;
        --workers) WORKERS="$2"; shift 2 ;;
        --threads) THREADS="$2"; shift 2 ;;
        --memory-max) MEMORY_MAX="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --tls-port) TLS_PORT="$2"; shift 2 ;;
        --gcp-project) GCP_PROJECT="$2"; shift 2 ;;
        --gcs-bucket) GCS_BUCKET="$2"; shift 2 ;;
        --static-base-url) STATIC_BASE_URL="$2"; shift 2 ;;
        --clean) CLEAN=true; shift ;;
        --skip-deps) SKIP_DEPS=true; shift ;;
        --skip-schema) SKIP_SCHEMA=true; shift ;;
        --yes) ASSUME_YES=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) sed -n '2,62p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
    [[ "$ASSUME_YES" == true || "$DRY_RUN" == true ]] && return 0
    printf '%s [y/N] ' "$1"
    read -r reply
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

[[ -n "$VM_NAME" ]] || die "--vm is required"
[[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"
[[ -n "$GCP_PROJECT" ]] || GCP_PROJECT="$PROJECT"
if [[ -z "$STATIC_BASE_URL" && -n "$GCS_BUCKET" ]]; then
    STATIC_BASE_URL="https://storage.googleapis.com/$GCS_BUCKET"
fi

# --- local preflight -------------------------------------------------------
log "Preflight"

[[ -f "$REPO_ROOT/web_app.py" ]] || die "web_app.py not found under $REPO_ROOT"
[[ -f "$REPO_ROOT/uv.lock" ]] || die "uv.lock not found; run 'uv lock' first"
[[ -f "$REPO_ROOT/scripts/systemd/flight-matrix-web.service" ]] \
    || die "scripts/systemd/flight-matrix-web.service is missing"

# The submodule is a hard requirement of `uv sync`, not of the web app. An empty
# checkout produces a resolution error on the host that reads like a network
# problem, so it is worth catching here.
if [[ ! -f "$REPO_ROOT/lib/resilient-scraper/pyproject.toml" ]]; then
    die "lib/resilient-scraper is not checked out, but pyproject.toml declares it as a
  path dependency, so 'uv sync' on the host will fail. Run:
    git submodule update --init --recursive"
fi

# gunicorn has to be in the lock file: the unit runs .venv/bin/gunicorn, and a
# lock that predates the dependency produces a unit that cannot start.
grep -q 'name = "gunicorn"' "$REPO_ROOT/uv.lock" \
    || die "uv.lock does not contain gunicorn. Run 'uv lock' and redeploy."

gcloud compute instances describe "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
    --format='value(status)' 2>/dev/null | grep -q RUNNING \
    || die "instance '$VM_NAME' is not RUNNING in $VM_ZONE (project $PROJECT)"

log "  repo: $REPO_ROOT"
log "  target: $VM_NAME ($VM_ZONE) in $PROJECT"

# What not to ship: build artefacts, caches, the local virtualenv (the host
# builds its own for its own platform and Python), and data/ -- scraped images
# and local SQLite files, which are host state rather than code.
#
# The top-level exclusions are done by *not naming* those entries, rather than
# with --exclude. tar's exclude patterns are matched against every path
# component and neither GNU tar nor bsdtar anchors them reliably (bsdtar
# normalises `./data` straight back to `data`), so `--exclude=data` also drops
# src/data/ -- the entire database layer. The archive stays valid, so the only
# symptom is an ImportError on the host after an apparently clean deploy.
skip_toplevel=" .git .venv .venv-cdk cdk.out data lambda_code layers .env .DS_Store "

tar_includes=()
while IFS= read -r entry; do
    name=${entry##*/}
    case "$skip_toplevel" in
        *" $name "*) continue ;;
    esac
    tar_includes+=("$name")
done < <(find "$REPO_ROOT" -mindepth 1 -maxdepth 1 | sort)

[[ ${#tar_includes[@]} -gt 0 ]] || die "nothing to ship from $REPO_ROOT"

# These are safe unanchored: no directory that should be shipped is named after
# a cache, and no shipped file ends in .pyc or .log.
tar_excludes=(
    --exclude=__pycache__
    --exclude=.mypy_cache
    --exclude=.pytest_cache
    --exclude=.ruff_cache
    --exclude=node_modules
    --exclude=.DS_Store
    --exclude='*.pyc'
    --exclude='*.log'
)

# Built once to a file rather than streamed twice: the manifest check below and
# the transfer itself must see the same archive, and a megabyte on disk is
# cheaper than taring the tree a second time.
payload_file=$(mktemp -t fm-payload)
cleanup() { rm -f "$payload_file" "${script_file:-}"; }
trap cleanup EXIT

tar "${tar_excludes[@]}" -cz -f "$payload_file" -C "$REPO_ROOT" "${tar_includes[@]}"
payload_bytes=$(wc -c < "$payload_file" | tr -d ' ')
log "  payload: $((payload_bytes / 1024)) KiB compressed, ${#tar_includes[@]} top-level entries"

# An over-broad exclude produces a valid archive that is missing a package, and
# the only symptom is an ImportError on the host after the deploy has otherwise
# succeeded. These are the two entry points the unit cannot start without.
manifest=$(tar -tzf "$payload_file")
for required in web_app.py src/data/db_manager.py config/config.yaml; do
    printf '%s\n' "$manifest" | grep -qxF "$required" \
        || die "the payload is missing $required -- check tar_excludes for an unanchored pattern"
done
log "  manifest: entry points present"

cat <<EOF

About to deploy flight-matrix to $VM_NAME ($VM_ZONE).

  application user   ${APP_USER:-<gcloud ssh login user>}
  application dir    ${APP_DIR:-~/flight-matrix}
  gunicorn           127.0.0.1:$APP_PORT, $WORKERS workers x $THREADS threads, MemoryMax=$MEMORY_MAX
  dependencies       $( [[ "$SKIP_DEPS" == true ]] && printf 'skipped (--skip-deps)' || printf 'uv sync --frozen --no-dev' )
  database schema    $( [[ "$SKIP_SCHEMA" == true ]] && printf 'skipped (--skip-schema)' || printf 'created if missing (idempotent)' )
  existing files     $( [[ "$CLEAN" == true ]] && printf 'directory wiped first, .venv and data/ kept' || printf 'overwritten in place; local deletions are NOT propagated' )
  GOOGLE_CLOUD_PROJECT $GCP_PROJECT
  GCS_ASSETS_BUCKET    ${GCS_BUCKET:-<left as found>}
  STATIC_BASE_URL      ${STATIC_BASE_URL:-<left as found>}

The web service is restarted at the end, so the site is briefly unavailable.
EOF
printf '\n'

if ! confirm "Proceed?"; then
    die "aborted by user"
fi

# ---------------------------------------------------------------------------
# The remote script
#
# Accumulated in a temp file for the same reason as in
# provision-existing-host.sh: bash 3.2, which macOS ships, does not recognise
# here-doc bodies while scanning for the closing paren of $( ), so an unbalanced
# `)` inside a here-doc silently truncates the substitution.
#
# The payload arrives on stdin, so every remote command that reads from the
# terminal is out of the question -- hence the explicit `-n`-free ordering: tar
# consumes stdin first, and nothing after it reads from it.
# ---------------------------------------------------------------------------
script_file=$(mktemp -t fm-deploy)

cat > "$script_file" <<EOF
CFG_APP_USER='$APP_USER'
CFG_APP_DIR='$APP_DIR'
CFG_APP_PORT='$APP_PORT'
CFG_WORKERS='$WORKERS'
CFG_THREADS='$THREADS'
CFG_MEMORY_MAX='$MEMORY_MAX'
CFG_CLEAN='$CLEAN'
CFG_SKIP_DEPS='$SKIP_DEPS'
CFG_SKIP_SCHEMA='$SKIP_SCHEMA'
CFG_GCP_PROJECT='$GCP_PROJECT'
CFG_GCS_BUCKET='$GCS_BUCKET'
CFG_STATIC_BASE_URL='$STATIC_BASE_URL'
EOF

cat >> "$script_file" <<'REMOTE'
set -euo pipefail

APP_USER="$CFG_APP_USER"
[[ -n "$APP_USER" ]] || APP_USER="${SUDO_USER:-$(id -un)}"
APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)
[[ -n "$APP_HOME" ]] || { echo "ERROR: no home directory for user '$APP_USER'" >&2; exit 1; }
APP_DIR="$CFG_APP_DIR"
[[ -n "$APP_DIR" ]] || APP_DIR="$APP_HOME/flight-matrix"
ENV_FILE=/etc/flight-matrix/env
UNIT=/etc/systemd/system/flight-matrix-web.service

say() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# The provisioning script is what creates these. Deploying without them would
# produce an app with no database URL and no session key, which fails as a 500
# on the first request rather than here.
[[ -d "$APP_DIR" ]] || { echo "ERROR: $APP_DIR does not exist; run provision-existing-host.sh or bootstrap-vm.sh first" >&2; exit 1; }
sudo test -f "$ENV_FILE" || { echo "ERROR: $ENV_FILE does not exist; run the provisioning script first" >&2; exit 1; }

say "user=$APP_USER dir=$APP_DIR"

# --- receive the code ------------------------------------------------------
# stdin is the tar stream. Read it before anything else so that a failure later
# cannot leave the pipe half-consumed and the local tar blocked on a full buffer.
if [[ "$CFG_CLEAN" == true ]]; then
    say "clean: emptying $APP_DIR (keeping .venv and data)"
    sudo find "$APP_DIR" -mindepth 1 -maxdepth 1 \
        ! -name .venv ! -name data -exec rm -rf {} +
fi

say "unpacking the payload"
sudo tar -xz -C "$APP_DIR" --no-same-owner -f -
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
say "unpacked $(sudo find "$APP_DIR" -name '*.py' -not -path '*/.venv/*' | wc -l) python files"

# --- environment additions -------------------------------------------------
# Only the values this script is the authority for. DEPLOY_TARGET, STAGE,
# DATABASE_URL and the OAuth URLs belong to the provisioning script and are left
# untouched, because they describe the host, not the code.
set_env() {
    local key="$1" value="$2"
    if sudo grep -qE "^${key}=" "$ENV_FILE"; then
        sudo sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" | sudo tee -a "$ENV_FILE" >/dev/null
    fi
}

set_env GOOGLE_CLOUD_PROJECT "$CFG_GCP_PROJECT"
# `if` rather than `[[ ... ]] && ...`: the && form evaluates to exit status 1
# when the value is empty, and `set -e` would abort the deploy right here --
# after the code is unpacked but before the venv build and the restart. Empty is
# the documented "leave as found" case, so it is not an error.
if [[ -n "$CFG_GCS_BUCKET" ]]; then
    set_env GCS_ASSETS_BUCKET "$CFG_GCS_BUCKET"
fi
if [[ -n "$CFG_STATIC_BASE_URL" ]]; then
    set_env STATIC_BASE_URL "$CFG_STATIC_BASE_URL"
fi
# CONFIG_PATH is relative in web_app.py's default; systemd's WorkingDirectory
# makes that resolve, but an absolute value survives anyone running the app by
# hand from another directory.
set_env CONFIG_PATH "$APP_DIR/config/config.yaml"
say "env: application values written"

# --- dependencies ----------------------------------------------------------
if [[ "$CFG_SKIP_DEPS" == true ]]; then
    say "deps: skipped"
    [[ -x "$APP_DIR/.venv/bin/gunicorn" ]] \
        || { echo "ERROR: --skip-deps was passed but $APP_DIR/.venv/bin/gunicorn does not exist" >&2; exit 1; }
else
    say "deps: uv sync (this takes a few minutes on the first run)"
    # --frozen: install exactly what uv.lock says and fail rather than silently
    # re-resolving, so the host cannot drift from the tested set.
    # --no-dev: pytest, mypy and ruff have no place on a server.
    sudo -u "$APP_USER" env -C "$APP_DIR" \
        UV_LINK_MODE=copy /usr/local/bin/uv sync --frozen --no-dev
    say "deps: $(sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -c 'import gunicorn; print("gunicorn", gunicorn.__version__)')"
fi

# --- database schema -------------------------------------------------------
if [[ "$CFG_SKIP_SCHEMA" == true ]]; then
    say "schema: skipped"
else
    say "schema: creating tables if missing"
    # DatabaseManager's constructor bootstraps the core, scraper and xiaohongshu
    # tables; the multi-user tables are created lazily by the app, so they are
    # forced here. Doing it now turns a schema problem into a deploy failure with
    # a traceback instead of a 500 on someone's first login.
    #
    # The script goes to a file and the environment is sourced inside the shell
    # that runs it. Passing DATABASE_URL on the command line instead would put
    # the database password in the process argument list, where any user on the
    # host can read it out of `ps` for as long as the process lives.
    init_py=$(mktemp)
    chmod 644 "$init_py"
    cat > "$init_py" <<'PYEOF'
import os
import sys

from src.data.db_manager import DatabaseManager

url = os.environ.get("DATABASE_URL")
if not url:
    sys.stderr.write("DATABASE_URL is not set in /etc/flight-matrix/env\n")
    raise SystemExit(1)

db = DatabaseManager(url)
db.ensure_multi_user_tables_exist()
db.ensure_report_tables_exist()
db.close()
print("schema ready")
PYEOF
    # PYTHONPATH rather than relying on the working directory: python puts the
    # *script's* directory on sys.path, and the script lives in /tmp. gunicorn
    # does not need this because it inserts its own cwd, which the unit sets.
    sudo -u "$APP_USER" env -C "$APP_DIR" PYTHONPATH="$APP_DIR" bash -c \
        "set -a; . '$ENV_FILE'; set +a; exec '$APP_DIR/.venv/bin/python' '$init_py'"
    rm -f "$init_py"
fi

# --- systemd unit ----------------------------------------------------------
# The unit shipped in the repo is written for the dedicated-VM layout (user
# ubuntu, /home/ubuntu/flight-matrix). Patched rather than templated so that the
# repo keeps one readable, commented copy instead of a file full of
# placeholders.
say "systemd: installing flight-matrix-web.service"
sudo install -d -o "$APP_USER" -g "$APP_USER" -m 755 /var/log/flight-matrix
sudo sed \
    -e "s|^User=.*|User=$APP_USER|" \
    -e "s|^Group=.*|Group=$APP_USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
    -e "s|^ExecStart=.*/gunicorn|ExecStart=$APP_DIR/.venv/bin/gunicorn|" \
    -e "s|--bind 127.0.0.1:[0-9]*|--bind 127.0.0.1:$CFG_APP_PORT|" \
    -e "s|--workers [0-9]*|--workers $CFG_WORKERS|" \
    -e "s|--threads [0-9]*|--threads $CFG_THREADS|" \
    -e "s|^MemoryMax=.*|MemoryMax=$CFG_MEMORY_MAX|" \
    "$APP_DIR/scripts/systemd/flight-matrix-web.service" \
    | sudo tee "$UNIT" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable flight-matrix-web >/dev/null 2>&1 || true
sudo systemctl restart flight-matrix-web

# --- verify ----------------------------------------------------------------
# gunicorn forks its workers after the master reports ready, and the app imports
# SQLAlchemy, plotly and the Google SDKs before it can answer. A poll is the
# honest way to wait for that; a fixed sleep is either too short or wasteful.
say "waiting for the app to answer on 127.0.0.1:$CFG_APP_PORT"
ok=false
for i in $(seq 1 60); do
    if ! sudo systemctl is-active --quiet flight-matrix-web; then
        echo "ERROR: flight-matrix-web stopped during startup" >&2
        sudo journalctl -u flight-matrix-web -n 40 --no-pager >&2 || true
        sudo tail -n 40 /var/log/flight-matrix/web.log 2>/dev/null >&2 || true
        exit 1
    fi
    # No `|| echo 000` fallback: curl already writes %{http_code} as 000 when the
    # request never completes, so the fallback would append a second 000 and turn
    # a timeout into the unmatched string "000000".
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://127.0.0.1:$CFG_APP_PORT/" 2>/dev/null) || true
    # 200 = rendered. 302 = redirected to the identity provider's login. 403 =
    # login_required found no provider configured and rendered 403.html, which is
    # the expected answer until the Google OAuth client credentials are in place.
    # All three prove the WSGI app imported and is serving requests; 000, 502 and
    # 503 do not.
    if [[ "$code" == 200 || "$code" == 302 || "$code" == 403 ]]; then
        say "app answered with HTTP $code after ${i}s"
        ok=true
        break
    fi
    sleep 1
done

if [[ "$ok" != true ]]; then
    echo "ERROR: the app did not answer within 60s" >&2
    sudo tail -n 40 /var/log/flight-matrix/web.log 2>/dev/null >&2 || true
    exit 1
fi

say "deploy complete"
REMOTE

if [[ "$DRY_RUN" == true ]]; then
    printf 'DRY-RUN: would stream %s KiB of code into gcloud compute ssh %s\n' \
        "$((payload_bytes / 1024))" "$VM_NAME"
    printf 'DRY-RUN: remote script:\n%s\n' "$(cat "$script_file")"
    exit 0
fi

log "Shipping code and deploying"
# The tar stream is the remote script's stdin. `--command` keeps gcloud from
# allocating a TTY, which would corrupt the binary payload with newline
# translation.
gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
    --command="$(cat "$script_file")" < "$payload_file"

if [[ -n "$DOMAIN" ]]; then
    log "Checking https://$DOMAIN:$TLS_PORT/ from here"
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        "https://$DOMAIN:$TLS_PORT/" 2>/dev/null) || true
    case "${code:-000}" in
        200|302|403) log "  HTTP $code -- reachable through nginx" ;;
        000) log "  no response. The app answered on the host, so this is either DNS,"
             log "  the VPC firewall, or egress filtering on this network blocking"
             log "  outbound port $TLS_PORT." ;;
        *) log "  HTTP $code -- nginx answered but not with a page; check the app log" ;;
    esac
fi

log "Done"
