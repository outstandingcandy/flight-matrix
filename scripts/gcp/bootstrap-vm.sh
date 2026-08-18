#!/bin/bash
#
# GCE startup script: prepare the flight-matrix host
#
# Runs as root on every boot, passed by create-vm.sh via
# --metadata-from-file=startup-script. GCE re-runs startup scripts on each boot,
# so every step here is idempotent and cheap to repeat — that is a hard
# requirement, not a nicety: a reboot must not re-partition swap, reset the
# Postgres config, or clobber /etc/flight-matrix/env.
#
# Deliberately NOT done here:
#   - Shipping application code. deploy-app.sh owns that, so a code deploy can
#     never re-tune Postgres or resize swap.
#   - Enabling the application systemd units. They point at
#     /home/ubuntu/flight-matrix/.venv, which does not exist until deploy-app.sh
#     has run; enabling them now would produce a crash loop that masks real
#     failures.
#
# Configuration arrives as instance metadata (set by create-vm.sh):
#   deploy-target, gcs-assets-bucket, gcs-private-bucket, swap-size-gb, fm-domain
#
# Logs land in the serial console and journald:
#   sudo journalctl -u google-startup-scripts -f
#

set -euo pipefail

log() { printf '[bootstrap %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

MD="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
meta() {
    curl -sf -H "Metadata-Flavor: Google" "$MD/$1" 2>/dev/null || printf '%s' "${2:-}"
}

APP_USER="ubuntu"
APP_HOME="/home/$APP_USER"
APP_DIR="$APP_HOME/flight-matrix"
ENV_DIR="/etc/flight-matrix"
ENV_FILE="$ENV_DIR/env"
LOG_DIR="/var/log/flight-matrix"
SWAP_FILE="/swapfile"

DEPLOY_TARGET="$(meta deploy-target gcp)"
ASSETS_BUCKET="$(meta gcs-assets-bucket)"
PRIVATE_BUCKET="$(meta gcs-private-bucket)"
SWAP_GB="$(meta swap-size-gb 4)"
DOMAIN="$(meta fm-domain)"

# Postgres 16 is what Ubuntu 24.04 ships. Pinning the major version keeps the
# data directory path stable, which migrate-db-to-gcp.sh's restore step and the
# tuning below both depend on.
PG_VERSION=16
PG_CONF_DIR="/etc/postgresql/$PG_VERSION/main"
PG_TUNING_CONF="$PG_CONF_DIR/conf.d/flight-matrix.conf"

log "target=$DEPLOY_TARGET assets=$ASSETS_BUCKET private=$PRIVATE_BUCKET swap=${SWAP_GB}G domain=${DOMAIN:-none}"

# ---------------------------------------------------------------------------
# Swap
#
# 16 GB is not much for Chromium plus Postgres plus four Python services. Swap
# is the difference between a slow minute and the OOM killer choosing Postgres.
# ---------------------------------------------------------------------------
if swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
    log "swap: $SWAP_FILE already active"
else
    if [[ ! -f "$SWAP_FILE" ]]; then
        log "swap: allocating ${SWAP_GB}G at $SWAP_FILE"
        fallocate -l "${SWAP_GB}G" "$SWAP_FILE" || dd if=/dev/zero of="$SWAP_FILE" bs=1M count=$((SWAP_GB * 1024))
        chmod 600 "$SWAP_FILE"
        mkswap "$SWAP_FILE"
    fi
    log "swap: enabling $SWAP_FILE"
    swapon "$SWAP_FILE"
fi
grep -qF "$SWAP_FILE" /etc/fstab || printf '%s none swap sw 0 0\n' "$SWAP_FILE" >>/etc/fstab

# Prefer reclaiming page cache over swapping out a live Postgres backend, but
# still allow swap under real pressure. 10 is the usual database compromise.
sysctl_conf=/etc/sysctl.d/60-flight-matrix.conf
if [[ ! -f "$sysctl_conf" ]]; then
    log "sysctl: writing $sysctl_conf"
    cat >"$sysctl_conf" <<'SYSCTL'
vm.swappiness = 10
vm.overcommit_memory = 0
SYSCTL
    sysctl -q --load "$sysctl_conf"
fi

# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive

PACKAGES=(
    "postgresql-$PG_VERSION"
    "postgresql-client-$PG_VERSION"
    xvfb
    chromium-browser
    nginx
    certbot
    python3-certbot-nginx
    python3-venv
    python3-dev
    build-essential
    libpq-dev
    git
    jq
    unzip
    fonts-liberation
    fonts-noto-cjk
)

missing=()
for pkg in "${PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed" || missing+=("$pkg")
done

if [[ ${#missing[@]} -gt 0 ]]; then
    log "apt: installing ${missing[*]}"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${missing[@]}"
else
    log "apt: all packages already installed"
fi

# Chromium on Ubuntu 24.04 is a snap by default, and snap confinement blocks
# DrissionPage's user-data-dir handling. chromium-browser from the archive is the
# deb; verify we got a real binary rather than the snap shim.
chrome_bin=""
for candidate in /usr/bin/chromium-browser /usr/bin/chromium /snap/bin/chromium; do
    [[ -x "$candidate" ]] && { chrome_bin="$candidate"; break; }
done
if [[ -z "$chrome_bin" ]]; then
    log "WARNING: no chromium binary found; the scraper will not start"
else
    log "chromium: $chrome_bin ($("$chrome_bin" --version 2>/dev/null | head -1 || echo 'version unknown'))"
fi

# uv is the project's package manager; deploy-app.sh needs it on PATH.
if [[ ! -x /usr/local/bin/uv ]]; then
    log "uv: installing"
    curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi
log "uv: $(/usr/local/bin/uv --version 2>/dev/null || echo 'not available')"

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$APP_DIR" "$LOG_DIR"
install -d -o root -g root -m 750 "$ENV_DIR"

# ---------------------------------------------------------------------------
# Environment file
#
# Pulled from the private bucket rather than baked into metadata: instance
# metadata is readable by anything with compute.instances.get on the project,
# and this file holds the database password and API keys.
# ---------------------------------------------------------------------------
if [[ -n "$PRIVATE_BUCKET" ]] \
    && gcloud storage cp "gs://$PRIVATE_BUCKET/env" "$ENV_FILE.new" --quiet 2>/dev/null; then
    if [[ -f "$ENV_FILE" ]] && cmp -s "$ENV_FILE" "$ENV_FILE.new"; then
        log "env: gs://$PRIVATE_BUCKET/env unchanged"
        rm -f "$ENV_FILE.new"
    else
        log "env: installing gs://$PRIVATE_BUCKET/env -> $ENV_FILE"
        mv "$ENV_FILE.new" "$ENV_FILE"
    fi
elif [[ -f "$ENV_FILE" ]]; then
    log "env: gs://$PRIVATE_BUCKET/env unreadable; keeping the existing $ENV_FILE"
else
    # A placeholder rather than nothing, so EnvironmentFile= resolves and the
    # failure surfaces as a missing credential rather than a missing file.
    log "env: gs://$PRIVATE_BUCKET/env not found; writing a placeholder"
    cat >"$ENV_FILE" <<PLACEHOLDER
# Placeholder written by bootstrap-vm.sh. Replace it by uploading the real file:
#   gcloud storage cp .env.prod gs://$PRIVATE_BUCKET/env
# then re-run this bootstrap:
#   sudo google_metadata_script_runner startup
DEPLOY_TARGET=$DEPLOY_TARGET
STAGE=prod
GOOGLE_CLOUD_PROJECT=$(curl -sf -H "Metadata-Flavor: Google" \
    http://metadata.google.internal/computeMetadata/v1/project/project-id 2>/dev/null || printf '')
GCS_ASSETS_BUCKET=$ASSETS_BUCKET
GCS_PRIVATE_BUCKET=$PRIVATE_BUCKET
PLACEHOLDER
fi
chmod 640 "$ENV_FILE"
chgrp "$APP_USER" "$ENV_FILE"

# The env file is the source of truth, but these four values are derived from
# the instance itself and must not drift if someone edits the bucket copy.
set_env_var() {
    local key="$1" value="$2"
    [[ -n "$value" ]] || return 0
    if grep -qE "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
    fi
}
set_env_var DEPLOY_TARGET "$DEPLOY_TARGET"
set_env_var GCS_ASSETS_BUCKET "$ASSETS_BUCKET"
set_env_var GCS_PRIVATE_BUCKET "$PRIVATE_BUCKET"

# ---------------------------------------------------------------------------
# Postgres
#
# Bound to 127.0.0.1 only. create-infra.sh asserts that no firewall rule opens
# 5432, so this is the second of two independent layers keeping the database off
# the internet. Both are load-bearing; do not relax either.
# ---------------------------------------------------------------------------
if [[ ! -d "$PG_CONF_DIR" ]]; then
    log "WARNING: $PG_CONF_DIR missing; Postgres $PG_VERSION did not install cleanly"
else
    install -d -o postgres -g postgres -m 755 "$PG_CONF_DIR/conf.d"

    # Written once and left alone: a reboot must not silently revert a DBA's
    # hand-tuning. Delete the file to have this script regenerate it.
    if [[ -f "$PG_TUNING_CONF" ]]; then
        log "postgres: $PG_TUNING_CONF already present"
    else
        log "postgres: writing $PG_TUNING_CONF"
        cat >"$PG_TUNING_CONF" <<'PGCONF'
# flight-matrix tuning for a 16 GB single-host install shared with Chromium.
#
# shared_buffers is 2 GB rather than the usual 25% of RAM (4 GB): the scraper's
# Chromium peaks at ~1 GB per browser and the four Python services need headroom,
# so the database deliberately takes less than it would on a dedicated host.
listen_addresses = 'localhost'
port = 5432

shared_buffers = 2GB
effective_cache_size = 6GB
maintenance_work_mem = 512MB
work_mem = 16MB
max_connections = 50

# Matches the pd-balanced SSD this runs on; the default 4.0 assumes spinning
# disks and pushes the planner away from index scans.
random_page_cost = 1.1
effective_io_concurrency = 200

wal_compression = on
checkpoint_completion_target = 0.9
max_wal_size = 4GB
min_wal_size = 1GB

log_min_duration_statement = 5000
log_checkpoints = on
log_autovacuum_min_duration = 0
PGCONF
        chown postgres:postgres "$PG_TUNING_CONF"
        pg_restart_needed=true
    fi

    # scram-sha-256 over loopback: the application connects by TCP with a
    # password, while pg_restore keeps using peer auth via sudo -u postgres.
    hba="$PG_CONF_DIR/pg_hba.conf"
    if grep -qE '^host\s+all\s+all\s+127\.0\.0\.1/32\s+scram-sha-256' "$hba"; then
        log "postgres: pg_hba already allows scram over loopback"
    else
        log "postgres: enabling scram-sha-256 for 127.0.0.1"
        sed -i -E 's|^(host\s+all\s+all\s+127\.0\.0\.1/32\s+).*|\1scram-sha-256|' "$hba"
        sed -i -E 's|^(host\s+all\s+all\s+::1/128\s+).*|\1scram-sha-256|' "$hba"
        pg_restart_needed=true
    fi

    systemctl enable --now "postgresql@$PG_VERSION-main" >/dev/null 2>&1 \
        || systemctl enable --now postgresql >/dev/null 2>&1 || true

    if [[ "${pg_restart_needed:-false}" == true ]]; then
        log "postgres: restarting to apply configuration"
        systemctl restart "postgresql@$PG_VERSION-main" || systemctl restart postgresql
    fi

    # Role and database. migrate-db-to-gcp.sh --step restore also creates these
    # if absent; doing it here means a plain deploy works without a migration.
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        db_user=$(grep -E '^DB_USERNAME=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'')
        db_name=$(grep -E '^DB_NAME=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'')
        db_pass=$(grep -E '^DB_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'')
    fi
    db_user="${db_user:-aircraft_admin}"
    db_name="${db_name:-aircraft_data}"

    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$db_user'" | grep -q 1; then
        log "postgres: role $db_user exists"
    else
        log "postgres: creating role $db_user"
        sudo -u postgres createuser "$db_user"
    fi

    if [[ -n "${db_pass:-}" ]]; then
        # Set unconditionally: the env file is authoritative, and a stale
        # password here fails as an opaque authentication error at runtime.
        sudo -u postgres psql -qc \
            "ALTER ROLE \"$db_user\" WITH PASSWORD '${db_pass//\'/\'\'}'" >/dev/null
        log "postgres: password for $db_user synced from $ENV_FILE"
    else
        log "postgres: no DB_PASSWORD in $ENV_FILE; role $db_user has no password yet"
    fi

    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db_name'" | grep -q 1; then
        log "postgres: database $db_name exists"
    else
        log "postgres: creating database $db_name owned by $db_user"
        sudo -u postgres createdb -O "$db_user" "$db_name"
    fi

    listening=$(ss -ltnH 'sport = :5432' 2>/dev/null | awk '{print $4}' | tr '\n' ' ')
    log "postgres: listening on ${listening:-nothing}"
    case "$listening" in
        *0.0.0.0:5432*|*[*]:5432*)
            log "WARNING: Postgres is listening on all interfaces. listen_addresses must be localhost."
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# nginx
#
# Reverse-proxies to gunicorn on 127.0.0.1:8000, which deploy-app.sh starts.
# Configured now so certbot has a server_name to attach a certificate to.
# ---------------------------------------------------------------------------
server_name="${DOMAIN:-_}"
nginx_site=/etc/nginx/sites-available/flight-matrix
if [[ -f "$nginx_site" ]] && grep -q "server_name $server_name;" "$nginx_site"; then
    log "nginx: site already configured for $server_name"
else
    log "nginx: writing site for server_name $server_name"
    cat >"$nginx_site" <<NGINX
# Managed by scripts/gcp/bootstrap-vm.sh. Edits are overwritten when the
# server_name changes.
upstream flight_matrix_app {
    server 127.0.0.1:8000 fail_timeout=0;
}

server {
    listen 80;
    listen [::]:80;
    server_name $server_name;

    client_max_body_size 32m;

    # Images and web assets are served from GCS, not from here; this only covers
    # anything deploy-app.sh leaves on local disk.
    location /static/ {
        alias $APP_DIR/web_static/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass http://flight_matrix_app;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        # Aircraft analysis calls an LLM inline and can legitimately take
        # minutes; the 60s default would return 504 mid-analysis.
        proxy_read_timeout 300s;
        proxy_connect_timeout 15s;
    }
}
NGINX
    ln -sfn "$nginx_site" /etc/nginx/sites-enabled/flight-matrix
    rm -f /etc/nginx/sites-enabled/default
    nginx_changed=true
fi

if nginx -t >/dev/null 2>&1; then
    systemctl enable nginx >/dev/null 2>&1 || true
    if [[ "${nginx_changed:-false}" == true ]]; then
        systemctl reload nginx 2>/dev/null || systemctl restart nginx
    else
        systemctl start nginx 2>/dev/null || true
    fi
    log "nginx: active"
else
    log "WARNING: nginx configuration test failed; leaving the previous config in place"
    nginx -t 2>&1 | tail -5 || true
fi

# ---------------------------------------------------------------------------
# TLS
#
# Only with a domain, and only if there is no live certificate. Certbot's rate
# limits are per-domain per-week, so a reboot loop must not re-request.
# ---------------------------------------------------------------------------
if [[ -z "$DOMAIN" ]]; then
    log "certbot: skipped (no fm-domain metadata)"
elif [[ -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    log "certbot: certificate for $DOMAIN already present; renewal is handled by the packaged timer"
else
    admin_email=$(grep -E '^(ALERT_EMAIL|SENDER_EMAIL)=' "$ENV_FILE" 2>/dev/null \
        | head -1 | cut -d= -f2- | tr -d '"'"'"'' || true)
    certbot_args=(--nginx --non-interactive --agree-tos --redirect --domain "$DOMAIN")
    if [[ -n "$admin_email" ]]; then
        certbot_args+=(--email "$admin_email")
    else
        certbot_args+=(--register-unsafely-without-email)
    fi
    log "certbot: requesting a certificate for $DOMAIN"
    if certbot "${certbot_args[@]}"; then
        log "certbot: certificate installed"
    else
        log "WARNING: certbot failed. Usually DNS: $DOMAIN must already resolve to this"
        log "         instance's external IP. Fix DNS and re-run:"
        log "           sudo certbot --nginx -d $DOMAIN"
    fi
fi

# ---------------------------------------------------------------------------
# Xvfb
#
# Display :55 matches config/scraper/base.yaml's xvfb.display_base and the
# DISPLAY=:55 in flight-matrix-scraper.service. Started here because it has no
# dependency on application code, unlike the other units.
# ---------------------------------------------------------------------------
xvfb_unit=/etc/systemd/system/flight-matrix-xvfb.service
if [[ -f "$xvfb_unit" ]]; then
    log "xvfb: unit already installed"
else
    log "xvfb: installing unit for display :55"
    cat >"$xvfb_unit" <<XVFB
[Unit]
Description=Xvfb virtual display for flight-matrix scrapers
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
ExecStart=/usr/bin/Xvfb :55 -screen 0 1920x1080x24
Restart=always
RestartSec=5
StandardOutput=append:$LOG_DIR/xvfb.log
StandardError=append:$LOG_DIR/xvfb.log

[Install]
WantedBy=multi-user.target
XVFB
    systemctl daemon-reload
fi
systemctl enable --now flight-matrix-xvfb.service >/dev/null 2>&1 || true
log "xvfb: $(systemctl is-active flight-matrix-xvfb.service 2>/dev/null || echo inactive)"

# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------
logrotate_conf=/etc/logrotate.d/flight-matrix
if [[ ! -f "$logrotate_conf" ]]; then
    log "logrotate: writing $logrotate_conf"
    cat >"$logrotate_conf" <<ROTATE
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su $APP_USER $APP_USER
}
ROTATE
fi

log "flight-matrix bootstrap complete"
log "next: ./scripts/gcp/deploy-app.sh  (ships code, creates the venv, enables the app units)"
