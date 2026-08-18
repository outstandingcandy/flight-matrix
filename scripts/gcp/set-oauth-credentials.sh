#!/bin/bash
#
# Install Google OAuth client credentials on a provisioned GCE host
#
# The client itself has to be created by hand: the GCP Console is the only place
# a generic "Web application" OAuth client can be created. gcloud only manages
# IAP-brand clients, whose redirect URI is fixed to IAP's own endpoint and
# therefore cannot point at this application.
#
# Console steps (APIs & Services -> Credentials -> Create credentials ->
# OAuth client ID -> Web application):
#
#   Authorised redirect URI:  https://HOST[:PORT]/auth/callback
#
# Google's rules for that URI, all of which the provisioning script's default
# satisfies:
#   - https is required (the only http exceptions are localhost and 127.0.0.1)
#   - a bare IP address is rejected; the host must be a name with a real TLD
#   - a non-standard port IS allowed
#   - it must match what the app sends byte for byte, port included
#
# This script reads the two secrets with the terminal echo off and pipes them to
# the host over stdin, so neither value appears in an argument list, in `ps`, or
# in either machine's shell history.
#
# Usage:
#   ./scripts/gcp/set-oauth-credentials.sh --vm NAME [options]
#
# Options:
#   --vm NAME         Target GCE instance (required)
#   --vm-zone ZONE    Zone of the instance (default: us-west1-b)
#   --project ID      GCP project (default: gcloud config value)
#   --callback-url U  Override GOOGLE_OAUTH_CALLBACK_URL. Normally left alone:
#                     provision-existing-host.sh already wrote the value that
#                     matches the host, port and scheme it configured.
#   --no-restart      Write the values but do not restart the web service
#
# Example:
#   ./scripts/gcp/set-oauth-credentials.sh --vm redpanda --vm-zone us-west1-b
#

set -euo pipefail

VM_NAME=""
VM_ZONE="us-west1-b"
PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
CALLBACK_URL=""
RESTART=true

while [[ $# -gt 0 ]]; do
    case $1 in
        --vm) VM_NAME="$2"; shift 2 ;;
        --vm-zone) VM_ZONE="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --callback-url) CALLBACK_URL="$2"; shift 2 ;;
        --no-restart) RESTART=false; shift ;;
        -h|--help) sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ -n "$VM_NAME" ]] || die "--vm is required"
[[ -n "$PROJECT" ]] || die "--project or a gcloud default project is required"

# Shown so that the value pasted into the Console can be compared against what
# the application will actually send. A mismatch here is the single most common
# cause of redirect_uri_mismatch.
# `-- -n` points ssh's stdin at /dev/null. Without it this call inherits the
# script's stdin and consumes the credential lines whenever the script is driven
# non-interactively (values piped in rather than typed), leaving the reads below
# to block or take the wrong input.
current=$(gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
    --command='sudo grep -E "^GOOGLE_OAUTH_(CALLBACK|LOGOUT)_URL=" /etc/flight-matrix/env || true' \
    -- -n 2>/dev/null | grep -v setlocale || true)

if [[ -z "$current" ]]; then
    die "no GOOGLE_OAUTH_CALLBACK_URL on $VM_NAME. Run provision-existing-host.sh first."
fi

cat <<EOF
The host currently expects:

$current

The "Authorised redirect URI" in the Console must equal the callback URL above
exactly, including the port.

EOF

printf 'GOOGLE_OAUTH_CLIENT_ID: '
read -r client_id
[[ -n "$client_id" ]] || die "client ID cannot be empty"

# -s: the secret is never echoed to the terminal and never reaches the history.
printf 'GOOGLE_OAUTH_CLIENT_SECRET (not echoed): '
read -rs client_secret
printf '\n'
[[ -n "$client_secret" ]] || die "client secret cannot be empty"

remote_script='
set -euo pipefail
ENV_FILE=/etc/flight-matrix/env

# Delete-then-append rather than sed-in-place. An in-place substitution has to
# carry the replacement text in sed argv, which on a shared host puts the client
# secret in `ps` output for every other user on the machine for as long as sed
# runs. Here only the key name reaches argv: printf is a shell builtin, so the
# value never becomes a process argument. The rewritten key moves to the end of
# the file, which an env file does not care about.
set_env() {
    local key="$1" value="$2"
    sudo sed -i "/^${key}=/d" "$ENV_FILE"
    printf "%s=%s\n" "$key" "$value" | sudo tee -a "$ENV_FILE" >/dev/null
}

# The values arrive on stdin, one per line, so they are never in argv.
IFS= read -r CLIENT_ID
IFS= read -r CLIENT_SECRET
IFS= read -r CALLBACK

set_env GOOGLE_OAUTH_CLIENT_ID "$CLIENT_ID"
set_env GOOGLE_OAUTH_CLIENT_SECRET "$CLIENT_SECRET"
# An `if` rather than `[[ ... ]] && ...`: the && form evaluates to exit status 1
# when CALLBACK is empty, which is the normal case, and `set -e` would then abort
# the script here -- after the credentials are written but before the restart.
if [[ -n "$CALLBACK" ]]; then
    set_env GOOGLE_OAUTH_CALLBACK_URL "$CALLBACK"
fi
echo "credentials written to $ENV_FILE (values not printed)"

if [[ "$RESTART_SERVICE" == true ]]; then
    sudo systemctl restart flight-matrix-web
    for i in $(seq 1 60); do
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8000/ 2>/dev/null) || true
        # 302 is the goal: with a provider configured, login_required now
        # redirects to Google instead of rendering 403.
        if [[ "$code" == 302 ]]; then
            echo "the app now redirects to the identity provider (HTTP 302)"
            exit 0
        fi
        if [[ "$code" == 403 ]]; then
            echo "WARNING: still HTTP 403 -- the app did not pick up a provider." >&2
            echo "         Check /var/log/flight-matrix/web.log for the reason." >&2
            exit 1
        fi
        sleep 1
    done
    echo "ERROR: the app did not answer with 302 or 403 within 60s" >&2
    exit 1
fi
'

printf '%s\n%s\n%s\n' "$client_id" "$client_secret" "$CALLBACK_URL" \
    | gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --project="$PROJECT" \
        --command="RESTART_SERVICE=$RESTART
$remote_script"
