# Shared Caddy gateway (`~/gateway/`, host-only, not in Git)

Reference config for 阶段 0.5. Copy to `~/gateway/` on the redpanda
host, adjust the AppID / backend service names, and run
`docker compose up -d`. Not committed as an executable directory
because the plan explicitly keeps this off Git (user decision, per
`.claude/plans/ios-app-lucky-sonnet.md`).

## Purpose

Single :80/:443 ingress for every business project on the box, SNI-routed
to per-project internal ports. Replaces flight-matrix-caddy on :8444 and
collecdex-caddy on :443, and takes over ACME so no more manual certbot.

## Layout on the host

```
~/gateway/
  docker-compose.yml
  Caddyfile
```

## docker-compose.yml

Cross-compose networks joined here so caddy can resolve
`flight-matrix-web` and `collecdex-web` by DNS.

```yaml
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      # No 443/udp — HTTP/3 disabled in Caddyfile below; publishing
      # UDP without advertising it causes clients to time out.
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    networks:
      - flight_matrix_default
      - deploy_default

volumes:
  caddy_data:
  caddy_config:

networks:
  flight_matrix_default:
    external: true
    name: flight-matrix_default
  deploy_default:
    external: true
    name: deploy_default
```

## Caddyfile

```
{
  email admin@flightmatrix.top
  servers {
    protocols h1 h2
  }
}

flightmatrix.top, api.flightmatrix.top {
  reverse_proxy flight-matrix-web:8000
}

collecdex.top {
  redir https://lego.collecdex.top{uri} 302
}

lego.collecdex.top, matchbox.collecdex.top {
  reverse_proxy collecdex-web:8080
}
```

`protocols h1 h2` (no h3) matches the fix in the repo's `Caddyfile` —
without it Caddy advertises `alt-svc: h3=":443"`, which the mobile
network then tries and drops when UDP isn't reachable.

## First-time migration

Producing this ingress from the current state has one brief outage:

1. `docker compose -f ~/gateway/docker-compose.yml up -d` on a spare
   port (say `:10443`) — smoke-test SNI routing.
2. Stop the current owner of :80/:443 (usually collecdex-caddy).
3. `docker compose ... up -d` on :80/:443 for real. Caddy will ACME
   every host listed above; keep an eye on `caddy_data` reaching
   `/etc/ssl/caddy` after each cert lands.
4. `curl -sSI https://flightmatrix.top/` — no port, expect 200.
5. Google OAuth console: add `https://flightmatrix.top/auth/callback`
   (already added in the existing config), remove `:8444` variant if
   present. Aliyun DNS: add `api.flightmatrix.top` A record.
6. flight-matrix's own Caddyfile at repo root can retire — the sidecar
   `flight-matrix-caddy` service in `docker-compose.web.yml` becomes
   obsolete. Keep it briefly as an :8444 bypass, then delete.

## Adding a new project later

Append a host block to `~/gateway/Caddyfile`, `docker compose exec caddy
caddy reload --config /etc/caddy/Caddyfile`. Certificate lands
automatically.
