#!/bin/bash
#
# Prepare an EXISTING GCE VM to also host the flight-matrix web application
#
# This is the counterpart to bootstrap-vm.sh, for the case where the VM was not
# created by create-vm.sh and is not ours alone. bootstrap-vm.sh assumes Ubuntu
# 24.04, a `ubuntu` user, an empty machine and sole ownership of ports 80 and
# 443; none of that holds on a host that is already serving something else. It
# would install nginx over the incumbent's port 80, delete
# /etc/nginx/sites-enabled/default, and hand certbot a port that is not free.
#
# So the governing rule here is: touch nothing that belongs to a co-tenant.
#   - nginx listens on ONE port, given by --tls-port, and this script refuses to
#     run if that port is already in use. The default site is left exactly as
#     found, whether present or absent.
#   - Postgres is a dedicated container on its own loopback port with its own
#     volume. Sharing an existing instance would need the incumbent's superuser
#     password and would put two applications' data in one blast radius, which
#     buys nothing on a host that already has the image cached.
#   - swap is added only when the host has none at all.
#   - The single co-tenant interruption is the ACME challenge, which needs port
#     80 for a few seconds. Nothing else can free it, so it is explicit, opt-in
#     via --acme-stop-container, and reversed by certbot's post-hook.
#
# What this does NOT do: ship application code, build the virtualenv, or start
# the app. deploy-app.sh owns all three, so re-deploying code can never
# re-partition swap or re-issue a certificate.
#
# Usage:
#   ./scripts/gcp/provision-existing-host.sh --vm NAME --domain HOST [options]
#
# Options:
#   --vm NAME               Target GCE instance (required)
#   --vm-zone ZONE          Zone of the instance (default: us-west1-b)
#   --project ID            GCP project (default: gcloud config value)
#   --user NAME             OS user the application runs as (default: the
#                           account gcloud ssh logs in as)
#   --app-dir PATH          Application directory (default: ~USER/flight-matrix)
#   --domain HOST           Hostname served over TLS. Required: Google rejects
#                           OAuth redirect URIs that are bare IP addresses, and
#                           certbot needs a name to validate.
#   --tls-port PORT         Port nginx serves HTTPS on (default: 8080). Must be
#                           free on the host AND open in the VPC firewall.
#   --app-port PORT         Loopback port gunicorn will bind (default: 8000)
#   --db-port PORT          Loopback port for the Postgres container (default: 5433)
#   --db-name NAME          Database to create (default: aircraft_data)
#   --db-user NAME          Role to create (default: aircraft_admin)
#   --db-container NAME     Postgres container name (default: flight-matrix-db)
#   --pg-image IMAGE        (default: postgres:16-alpine)
#   --swap-size GB          Swap file size when the host has no swap (default: 2)
#   --acme-stop-container N Container to stop for the duration of the ACME
#                           HTTP-01 challenge, because it holds port 80. Also
#                           recorded as certbot's renewal hook, so renewals
#                           repeat the same brief stop unattended.
#   --acme-email ADDR       Contact address for the ACME account. Without it,
#                           registration is --register-unsafely-without-email
#                           and expiry warnings go nowhere.
#   --skip-tls              Provision everything except the certificate and the
#                           nginx site.
#   --yes                   Skip confirmation prompts
#   --dry-run               Print the remote script, change nothing
#
# Examples:
#   # The shared host this was written for: Debian 12, port 80 held by a Docker
#   # container, 443 held by a proxy, 8080 free and already firewall-open.
#   ./scripts/gcp/provision-existing-host.sh \
#       --vm redpanda --vm-zone us-west1-b \
#       --domain 136-109-216-214.nip.io --tls-port 8080 \
#       --acme-stop-container deploy-web-1
#
#   # Read the remote script before letting it run
#   ./scripts/gcp/provision-existing-host.sh --vm redpanda \
#       --domain example.com --dry-run
#

set -euo pipefail

VM_NAME=""
VM_ZONE="us-west1-b"
PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
APP_USER=""
APP_DIR=""
DOMAIN=""
TLS_PORT=8080
APP_PORT=8000
DB_PORT=5433
DB_NAME="aircraft_data"
DB_USER="aircraft_admin"
DB_CONTAINER="flight-matrix-db"
PG_IMAGE="postgres:16-alpine"
SWAP_SIZE=2
ACME_STOP_CONTAINER=""
ACME_EMAIL=""
SKIP_TLS=false
ASSUME_YES=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --vm) VM_NAME="$2"; shift 2 ;;
        --vm-zone) VM_ZONE="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --user) APP_USER="$2"; shift 2 ;;
        --app-dir) APP_DIR="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --tls-port) TLS_PORT="$2"; shift 2 ;;
        --app-port) APP_PORT="$2"; shift 2 ;;
        --db-port) DB_PORT="$2"; shift 2 ;;
        --db-name) DB_NAME="$2"; shift 2 ;;
        --db-user) DB_USER="$2"; shift 2 ;;
        --db-container) DB_CONTAINER="$2"; shift 2 ;;
        --pg-image) PG_IMAGE="$2"; shift 2 ;;
        --swap-size) SWAP_SIZE="$2"; shift 2 ;;
        --acme-stop-container) ACME_STOP_CONTAINER="$2"; shift 2 ;;
        --acme-email) ACME_EMAIL="$2"; shift 2 ;;
        --skip-tls) SKIP_TLS=true; shift ;;
        --yes) ASSUME_YES=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) sed -n '2,80p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
[[ "$SKIP_TLS" == true || -n "$DOMAIN" ]] || die "--domain is required (or pass --skip-tls)"
for p in "$TLS_PORT" "$APP_PORT" "$DB_PORT"; do
    [[ "$p" =~ ^[0-9]+$ ]] || die "ports must be numeric, got '$p'"
done
[[ "$SWAP_SIZE" =~ ^[0-9]+$ ]] || die "--swap-size must be a whole number of GB"

# ---------------------------------------------------------------------------
# Preflight, run from here rather than on the VM: a firewall gap is invisible
# from inside the instance, and discovering after provisioning that the port is
# unreachable wastes the whole run.
# ---------------------------------------------------------------------------
log "Preflight"

gcloud compute instances describe "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
    --format='value(status)' >/dev/null 2>&1 \
    || die "instance '$VM_NAME' not found in $VM_ZONE (project $PROJECT)"

vm_tags=$(gcloud compute instances describe "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
    --format='value(tags.items)' 2>/dev/null || true)
log "  network tags: ${vm_tags:-none}"

# Filtered client-side: gcloud pushes --filter to the Compute API, which rejects
# predicates on allowed[].ports and on targetTags. A rule with no targetTags
# applies to every instance in the network, so it counts too. The list failing is
# fatal rather than a warning -- a check that always passes is worse than none.
port_open=$(gcloud compute firewall-rules list --project="$PROJECT" --format=json \
    | PORT="$TLS_PORT" TAGS="$vm_tags" python3 -c '
import json, os, sys

port = int(os.environ["PORT"])
tags = {t.strip() for t in os.environ["TAGS"].replace(";", ",").split(",") if t.strip()}


def covers(spec):
    if spec is None:
        return True
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return int(lo) <= port <= int(hi)
    return int(spec) == port


for rule in json.load(sys.stdin):
    if rule.get("disabled") or rule.get("direction") != "INGRESS":
        continue
    targets = set(rule.get("targetTags") or [])
    if targets and not (targets & tags):
        continue
    for allow in rule.get("allowed") or []:
        if allow.get("IPProtocol") not in ("tcp", "all"):
            continue
        ports = allow.get("ports")
        if ports is None or any(covers(p) for p in ports):
            print(rule["name"])
            break
') || die "could not list firewall rules in $PROJECT"

if [[ -z "${port_open//[[:space:]]/}" ]]; then
    log "  WARNING: no ingress rule opens tcp:$TLS_PORT to this instance."
    log "           The app will be provisioned but unreachable. Open it with:"
    log "             gcloud compute firewall-rules create flight-matrix-allow-$TLS_PORT \\"
    log "               --project=$PROJECT --allow=tcp:$TLS_PORT --target-tags=<a tag this VM has>"
else
    log "  tcp:$TLS_PORT opened by: $(printf '%s' "$port_open" | tr '\n' ' ')"
fi

nat_ip=$(gcloud compute instances describe "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true)

if [[ "$SKIP_TLS" != true ]]; then
    # Resolved through python3 rather than getent: getent does not exist on
    # macOS, where it fails silently and turns this check into a false negative.
    resolved=$(HOST="$DOMAIN" python3 -c '
import os, socket, sys

try:
    infos = socket.getaddrinfo(os.environ["HOST"], None, socket.AF_INET)
except OSError:
    sys.exit(0)
for info in infos:
    print(info[4][0])
    break
' 2>/dev/null || true)
    if [[ -z "$resolved" ]]; then
        die "$DOMAIN does not resolve. The ACME HTTP-01 challenge is answered at the
  address the name points to, so certbot cannot succeed until it resolves to
  ${nat_ip:-the external IP of this instance}."
    elif [[ "$resolved" != "$nat_ip" ]]; then
        die "$DOMAIN resolves to $resolved but this instance's external IP is ${nat_ip:-unknown}.
  The challenge would be sent to $resolved and fail."
    fi
    log "  DNS: $DOMAIN -> $resolved matches the instance"
fi

cat <<EOF

About to provision $VM_NAME ($VM_ZONE) as an additional host for flight-matrix.

  application user   ${APP_USER:-<gcloud ssh login user>}
  application dir    ${APP_DIR:-~/flight-matrix}
  HTTPS              $( [[ "$SKIP_TLS" == true ]] && printf 'skipped (--skip-tls)' \
                        || printf 'nginx on port %s, server_name %s' "$TLS_PORT" "$DOMAIN" )
  gunicorn upstream  127.0.0.1:$APP_PORT (loopback only)
  Postgres           container $DB_CONTAINER ($PG_IMAGE) on 127.0.0.1:$DB_PORT
                     database $DB_NAME, role $DB_USER, dedicated volume
  swap               ${SWAP_SIZE} GB, only if the host currently has none

Co-tenant impact:
EOF
if [[ -n "$ACME_STOP_CONTAINER" && "$SKIP_TLS" != true ]]; then
    cat <<EOF
  Container '$ACME_STOP_CONTAINER' is STOPPED for the few seconds the ACME
  HTTP-01 challenge needs port 80, then started again. This repeats on every
  renewal (roughly every 60 days), unattended, through the hooks certbot records
  in its renewal configuration. Whatever that container serves is briefly
  unavailable at those moments.
EOF
else
    cat <<EOF
  None. Ports 80 and 443 are not touched and no existing service is stopped.
EOF
fi
printf '\n'

if ! confirm "Proceed?"; then
    die "aborted by user"
fi

# ---------------------------------------------------------------------------
# The remote script
#
# Assembled as a configuration prelude plus QUOTED heredocs. The quoting is
# load-bearing: with an unquoted heredoc the local shell would expand the nginx
# variables ($host, $scheme, $proxy_add_x_forwarded_for) to empty strings on the
# way out, producing a config that is syntactically valid and silently wrong.
# Passing configuration as explicit assignments removes the need to escape
# anything at all.
#
# Accumulated in a temp file rather than a variable because bash 3.2 -- which is
# what macOS ships, and this script runs locally -- does not recognise here-doc
# bodies while scanning for the closing paren of $( ). The first unbalanced `)`
# in a here-doc, such as the one in `debian|ubuntu)` below, would end the command
# substitution early and produce a syntax error at a line that looks correct.
#
# Every step is guarded so a re-run is a no-op. The database password is
# generated ON the host and written straight into /etc/flight-matrix/env; it is
# never echoed and never appears in an argument list, so it stays out of this
# machine's shell history and out of the SSH command line.
# ---------------------------------------------------------------------------
script_file=$(mktemp -t fm-provision)
trap 'rm -f "$script_file"' EXIT

cat > "$script_file" <<EOF
CFG_APP_USER='$APP_USER'
CFG_APP_DIR='$APP_DIR'
CFG_TLS_PORT='$TLS_PORT'
CFG_APP_PORT='$APP_PORT'
CFG_DB_PORT='$DB_PORT'
CFG_DB_NAME='$DB_NAME'
CFG_DB_USER='$DB_USER'
CFG_DB_CONTAINER='$DB_CONTAINER'
CFG_PG_IMAGE='$PG_IMAGE'
CFG_SWAP_SIZE='$SWAP_SIZE'
CFG_DOMAIN='$DOMAIN'
CFG_ACME_STOP_CONTAINER='$ACME_STOP_CONTAINER'
CFG_ACME_EMAIL='$ACME_EMAIL'
EOF

cat >> "$script_file" <<'REMOTE'
set -euo pipefail

APP_USER="$CFG_APP_USER"
[[ -n "$APP_USER" ]] || APP_USER="${SUDO_USER:-$(id -un)}"
APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)
[[ -n "$APP_HOME" ]] || { echo "ERROR: no home directory for user '$APP_USER'" >&2; exit 1; }
APP_DIR="$CFG_APP_DIR"
[[ -n "$APP_DIR" ]] || APP_DIR="$APP_HOME/flight-matrix"
ENV_DIR=/etc/flight-matrix
ENV_FILE="$ENV_DIR/env"
LOG_DIR=/var/log/flight-matrix

say() { printf '[provision %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

say "user=$APP_USER dir=$APP_DIR"

# --- sanity: the OS this was written for -----------------------------------
. /etc/os-release
case "$ID" in
    debian|ubuntu) say "os: $PRETTY_NAME" ;;
    *) say "WARNING: $PRETTY_NAME is neither Debian nor Ubuntu; the apt steps may fail" ;;
esac

py=$(python3 --version 2>&1 | awk '{print $2}')
py_major=${py%%.*}
py_rest=${py#*.}
py_minor=${py_rest%%.*}
if [[ "$py_major" -lt 3 || ( "$py_major" -eq 3 && "$py_minor" -lt 11 ) ]]; then
    echo "ERROR: python3 is $py; flight-matrix requires 3.11+" >&2
    exit 1
fi
say "python: $py"

# --- refuse to fight a co-tenant for the TLS port --------------------------
if sudo ss -ltnH "sport = :$CFG_TLS_PORT" | grep -q .; then
    # Being a re-run is the common case, and on a re-run the holder is our own
    # nginx. Recognising that is not the same as displacing a stranger: the
    # listener has to be nginx *and* our site has to be the reason it is there.
    if sudo ss -ltnpH "sport = :$CFG_TLS_PORT" | grep -q '"nginx"' \
        && [[ -e /etc/nginx/sites-enabled/flight-matrix ]]; then
        say "port $CFG_TLS_PORT is held by our own nginx site (re-run)"
    else
        echo "ERROR: port $CFG_TLS_PORT is already in use on this host:" >&2
        sudo ss -ltnpH "sport = :$CFG_TLS_PORT" >&2
        echo "  Choose a free port with --tls-port rather than displacing the incumbent." >&2
        exit 1
    fi
else
    say "port $CFG_TLS_PORT is free"
fi

# --- swap ------------------------------------------------------------------
# Only when there is none at all. A host that already has swap was deliberately
# configured that way by somebody, and second-guessing that is out of scope.
if [[ -n "$(sudo swapon --show=NAME --noheadings 2>/dev/null)" ]]; then
    say "swap: already configured, leaving it alone"
elif [[ -f /swapfile ]]; then
    say "swap: /swapfile exists but is inactive; enabling"
    sudo swapon /swapfile
else
    say "swap: allocating ${CFG_SWAP_SIZE}G at /swapfile"
    sudo fallocate -l "${CFG_SWAP_SIZE}G" /swapfile \
        || sudo dd if=/dev/zero of=/swapfile bs=1M count=$((CFG_SWAP_SIZE * 1024)) status=none
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -qF /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    say "swap: active"
fi

# --- directories -----------------------------------------------------------
sudo install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$APP_DIR" "$LOG_DIR"
# Group-owned by the application user, not root: the env file inside is mode 640
# with that group, and without execute permission on the directory the app user
# cannot traverse into it to read the file it is allowed to read.
sudo install -d -o root -g "$APP_USER" -m 750 "$ENV_DIR"

# --- packages --------------------------------------------------------------
# policy-rc.d blocks daemon starts during installation. Without it apt starts
# nginx the moment it is unpacked, nginx tries to bind the packaged default site
# on port 80, the incumbent already holds it, and the install exits non-zero. Our
# own configuration never listens on 80; the incumbent's ownership of it is not
# disturbed at any point.
need=()
for pkg in nginx certbot python3-venv; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed" || need+=("$pkg")
done

if [[ ${#need[@]} -gt 0 ]]; then
    say "apt: installing ${need[*]}"
    printf '#!/bin/sh\nexit 101\n' | sudo tee /usr/sbin/policy-rc.d >/dev/null
    sudo chmod +x /usr/sbin/policy-rc.d
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${need[@]}"
    sudo rm -f /usr/sbin/policy-rc.d
else
    say "apt: nginx, certbot and python3-venv already installed"
fi

if [[ ! -x /usr/local/bin/uv ]]; then
    say "uv: installing"
    curl -fsSL https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
fi
say "uv: $(/usr/local/bin/uv --version 2>/dev/null || echo unavailable)"

# --- Postgres container ----------------------------------------------------
command -v docker >/dev/null \
    || { echo "ERROR: docker is required for the database container" >&2; exit 1; }

db_password=""
if sudo docker inspect "$CFG_DB_CONTAINER" >/dev/null 2>&1; then
    say "postgres: container $CFG_DB_CONTAINER already exists"
    sudo docker start "$CFG_DB_CONTAINER" >/dev/null 2>&1 || true
else
    # Generated here and consumed here. The only place it is persisted is
    # $ENV_FILE, mode 640, which is also where the application reads it from.
    db_password=$(openssl rand -hex 24)
    say "postgres: creating $CFG_DB_CONTAINER on 127.0.0.1:$CFG_DB_PORT"
    sudo docker volume create flight-matrix-pgdata >/dev/null
    sudo docker run -d \
        --name "$CFG_DB_CONTAINER" \
        --restart unless-stopped \
        -p "127.0.0.1:$CFG_DB_PORT:5432" \
        -e POSTGRES_DB="$CFG_DB_NAME" \
        -e POSTGRES_USER="$CFG_DB_USER" \
        -e POSTGRES_PASSWORD="$db_password" \
        -v flight-matrix-pgdata:/var/lib/postgresql/data \
        "$CFG_PG_IMAGE" >/dev/null
fi

say "postgres: waiting for readiness"
for i in $(seq 1 60); do
    if sudo docker exec "$CFG_DB_CONTAINER" \
        pg_isready -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -q 2>/dev/null; then
        say "postgres: ready after ${i}s"
        break
    fi
    if [[ "$i" -eq 60 ]]; then
        echo "ERROR: $CFG_DB_CONTAINER did not become ready in 60s" >&2
        sudo docker logs --tail 20 "$CFG_DB_CONTAINER" >&2 || true
        exit 1
    fi
    sleep 1
done

# --- environment file ------------------------------------------------------
sudo touch "$ENV_FILE"
sudo chmod 640 "$ENV_FILE"
sudo chgrp "$APP_USER" "$ENV_FILE"

set_env() {
    local key="$1" value="$2"
    if sudo grep -qE "^${key}=" "$ENV_FILE"; then
        # A literal | in a value would break this substitution. None of the keys
        # written here can contain one: hex tokens, hostnames, ports and URLs.
        sudo sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" | sudo tee -a "$ENV_FILE" >/dev/null
    fi
}

# DATABASE_URL is only written when this run created the container, i.e. when we
# are the ones holding the password. On a re-run the existing line is
# authoritative; overwriting it with an empty password would break a working
# install in a way that looks like an authentication bug.
if [[ -n "$db_password" ]]; then
    set_env DATABASE_URL \
        "postgresql+psycopg2://$CFG_DB_USER:$db_password@127.0.0.1:$CFG_DB_PORT/$CFG_DB_NAME"
    set_env DB_USERNAME "$CFG_DB_USER"
    set_env DB_NAME "$CFG_DB_NAME"
    set_env DB_HOST 127.0.0.1
    set_env DB_PASSWORD "$db_password"
    say "env: DATABASE_URL written for the freshly created database"
else
    say "env: DATABASE_URL left as found"
fi

if ! sudo grep -qE '^FLASK_SECRET_KEY=.+' "$ENV_FILE"; then
    set_env FLASK_SECRET_KEY "$(openssl rand -hex 32)"
    say "env: FLASK_SECRET_KEY generated"
fi
say "env: $ENV_FILE ready (contents not printed)"
REMOTE

if [[ "$SKIP_TLS" != true ]]; then
cat >> "$script_file" <<'REMOTE'

# --- certificate -----------------------------------------------------------
# HTTP-01 through --standalone. It is the only challenge available here:
# TLS-ALPN-01 needs port 443 and DNS-01 needs control of the zone, and on this
# class of host we have neither. certbot records the hooks below in the renewal
# configuration, so the packaged renewal timer repeats the same brief port-80
# handover on its own.
if [[ -n "$CFG_ACME_STOP_CONTAINER" ]]; then
    hook_args=(
        --pre-hook "docker stop $CFG_ACME_STOP_CONTAINER"
        --post-hook "docker start $CFG_ACME_STOP_CONTAINER"
    )
else
    hook_args=()
fi

if [[ -n "$CFG_ACME_EMAIL" ]]; then
    email_args=(--email "$CFG_ACME_EMAIL")
else
    email_args=(--register-unsafely-without-email)
fi

if sudo test -d "/etc/letsencrypt/live/$CFG_DOMAIN"; then
    say "certbot: certificate for $CFG_DOMAIN already present"
else
    say "certbot: requesting a certificate for $CFG_DOMAIN (port 80 borrowed briefly)"
    # Length-guarded because bash 3.2 treats "${arr[@]}" on an empty array as an
    # unbound variable under `set -u`, and this script may run under it.
    certbot_args=(certonly --standalone --non-interactive --agree-tos
                  --domain "$CFG_DOMAIN" "${email_args[@]}")
    if [[ ${#hook_args[@]} -gt 0 ]]; then
        certbot_args+=("${hook_args[@]}")
    fi
    sudo certbot "${certbot_args[@]}"
    say "certbot: certificate installed"
fi

# --- nginx site ------------------------------------------------------------
# One server block, one port. No listen directive for 80 or 443, and the
# packaged default site is left exactly as found: on this class of host,
# something else owns those ports.
site=/etc/nginx/sites-available/flight-matrix

# HTTP/2 is configured two incompatible ways depending on the nginx version, and
# using the wrong one is a hard config error, not a warning. The standalone
# `http2 on;` directive arrived in 1.25.1; before that it is the `http2`
# parameter of `listen`, which 1.25.1+ deprecates. Debian 12 ships 1.22.1, so
# both forms are live in practice and the version has to decide.
# Through sudo because nginx lives in /usr/sbin, which is not on an unprivileged
# user's PATH; the bare call would fail and `set -e` would end the run here.
nginx_ver=$(sudo nginx -v 2>&1 | sed 's|.*/||' | tr -d '[:space:]')
if [[ "$(printf '%s\n%s\n' "$nginx_ver" 1.25.1 | sort -V | head -1)" == "1.25.1" ]]; then
    listen_extra=""
    http2_line="    http2 on;"
else
    listen_extra=" http2"
    http2_line="    # http2 is enabled via the listen parameter above (nginx $nginx_ver < 1.25.1)"
fi
say "nginx: $nginx_ver"
sudo tee "$site" >/dev/null <<'NGINX_EOF'
# Managed by scripts/gcp/provision-existing-host.sh. Rewritten on every run.
upstream flight_matrix_app {
    server 127.0.0.1:__APP_PORT__ fail_timeout=0;
}

server {
    listen __TLS_PORT__ ssl__LISTEN_EXTRA__;
    listen [::]:__TLS_PORT__ ssl__LISTEN_EXTRA__;
__HTTP2_LINE__
    server_name __DOMAIN__;

    ssl_certificate     /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__DOMAIN__/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:fm_ssl:10m;
    ssl_session_timeout 1d;

    client_max_body_size 32m;

    # Images are served straight from the public GCS bucket via STATIC_BASE_URL,
    # so this only covers whatever the deploy leaves on local disk.
    location /static/ {
        alias __APP_DIR__/web_static/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass http://flight_matrix_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # $http_host rather than $host: it keeps the :port the client used.
        # ProxyFix runs with x_host=1, so this is what url_for(_external=True)
        # builds redirect URIs from -- including the OAuth callback, which has to
        # match what is registered with Google exactly, port included.
        proxy_set_header X-Forwarded-Host $http_host;
        # Aircraft analysis calls an LLM inline and can legitimately run for
        # minutes; the 60s default would return 504 mid-analysis.
        proxy_read_timeout 300s;
        proxy_connect_timeout 15s;
    }
}
NGINX_EOF

sudo sed -i \
    -e "s|__APP_PORT__|$CFG_APP_PORT|g" \
    -e "s|__TLS_PORT__|$CFG_TLS_PORT|g" \
    -e "s|__DOMAIN__|$CFG_DOMAIN|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__LISTEN_EXTRA__|$listen_extra|g" \
    -e "s|^__HTTP2_LINE__$|$http2_line|" \
    "$site"

sudo ln -sfn "$site" /etc/nginx/sites-enabled/flight-matrix

# The packaged default site listens on 80. On a host where something else already
# owns 80, nginx cannot bind and the whole service fails to start -- our own
# server block on $CFG_TLS_PORT included. So the default site is disabled, but
# only when one of the ports it asks for is genuinely taken: if nginx is serving
# a co-tenant on a free port, that site is theirs and stays.
default_link=/etc/nginx/sites-enabled/default
if [[ -e "$default_link" ]]; then
    default_conflict=""
    default_ports=$(sudo grep -hoE '^[[:space:]]*listen[^;]+;' "$default_link" \
        | grep -oE '[0-9]+' | sort -u)
    for dport in $default_ports; do
        if sudo ss -ltnH "sport = :$dport" | grep -q .; then
            default_conflict="$dport"
            break
        fi
    done
    if [[ -n "$default_conflict" ]]; then
        say "nginx: disabling the packaged default site -- it wants port $default_conflict, which is already in use"
        say "      (only the sites-enabled symlink is removed; sites-available/default is untouched)"
        sudo rm -f "$default_link"
    fi
fi

# The output is captured from the one test that matters -- the one run with the
# site enabled. Re-testing after removing the symlink would report on a
# configuration that no longer contains the broken file, which is to say it would
# print "test is successful" next to "test failed".
if nginx_test=$(sudo nginx -t 2>&1); then
    sudo systemctl enable nginx >/dev/null 2>&1 || true
    # reload only makes sense for a running service, and nginx is not running
    # after installation here: policy-rc.d deliberately blocked the post-install
    # start so that apt would not fail against the incumbent on port 80.
    if sudo systemctl is-active --quiet nginx; then
        sudo systemctl reload nginx
    elif ! sudo systemctl start nginx; then
        echo "ERROR: nginx failed to start" >&2
        sudo journalctl -u nginx -n 20 --no-pager >&2 || true
        sudo rm -f /etc/nginx/sites-enabled/flight-matrix
        exit 1
    fi
    say "nginx: serving https on :$CFG_TLS_PORT for $CFG_DOMAIN"
else
    echo "ERROR: nginx configuration test failed; the site was NOT enabled" >&2
    printf '%s\n' "$nginx_test" | tail -10 >&2
    sudo rm -f /etc/nginx/sites-enabled/flight-matrix
    exit 1
fi

# The settings that depend on the URL the browser will actually use. Written
# here rather than in deploy-app.sh because this script is what decides the
# scheme, host and port.
set_env DEPLOY_TARGET gcp
set_env STAGE prod
set_env APP_DOMAIN "$CFG_DOMAIN"
set_env GOOGLE_OAUTH_CALLBACK_URL "https://$CFG_DOMAIN:$CFG_TLS_PORT/auth/callback"
set_env GOOGLE_OAUTH_LOGOUT_URL "https://$CFG_DOMAIN:$CFG_TLS_PORT/"
say "env: DEPLOY_TARGET, STAGE and the Google OAuth URLs written"
REMOTE
fi

cat >> "$script_file" <<'REMOTE'

say "host provisioning complete"
say "next: ./scripts/gcp/deploy-app.sh  (ships code, builds the venv, starts gunicorn)"
REMOTE

remote_script=$(cat "$script_file")

if [[ "$DRY_RUN" == true ]]; then
    printf 'DRY-RUN: gcloud compute ssh %s --zone %s --project %s --command <script>\n' \
        "$VM_NAME" "$VM_ZONE" "$PROJECT"
    printf 'DRY-RUN: remote script:\n%s\n' "$remote_script"
    exit 0
fi

log "Running the remote provisioning script"
gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" --command="$remote_script"

log "Done"
