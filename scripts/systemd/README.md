# Flight-Matrix systemd units

Drop-in templates for running flight-matrix under systemd. The same files
are used for local (SQLite) and prod (Aurora / EC2) installs; environment
differences are controlled by `/etc/flight-matrix/env`.

## Units

| Unit | Role | Depends on | AWS calls |
|---|---|---|---|
| `flight-matrix-xvfb.service` | Xvfb on `:55` for DrissionPage | — | none |
| `flight-matrix-scraper.service` | Worker consuming `scraper_tasks` | xvfb | s3 (if `s3.enabled`) |
| `flight-matrix-scheduler.service` | Writes `scraper_tasks`; orphan / stuck cleanup | db | ec2 + autoscaling (if `STAGE=prod`) |
| `flight-matrix-reindex.service` + `.timer` | Incremental aircraft → OpenSearch sync, every 5 min | db + OpenSearch container | none |

The scheduler and scraper connect to the same DB and share one YAML
config. The difference between the two machine roles is which units are
enabled: a scheduler node runs only `flight-matrix-scheduler`; a worker
node runs `flight-matrix-xvfb` + `flight-matrix-scraper`.

## Environment file

Create `/etc/flight-matrix/env` with the fields the deployment needs. The
unit files read it via `EnvironmentFile=-/etc/flight-matrix/env` so the
service won't fail to start if the file is missing — defaults from the
repo's own `.env.local` / `.env.prod` take over instead.

### Local (SQLite)

```bash
sudo mkdir -p /etc/flight-matrix
sudo tee /etc/flight-matrix/env <<'EOF'
STAGE=local
DATABASE_URL=sqlite:////home/ubuntu/flight-matrix/aircraft_data.db
EOF
```

### Prod (Aurora, STAGE=prod)

```bash
sudo tee /etc/flight-matrix/env <<'EOF'
STAGE=prod
DATABASE_URL=postgresql+psycopg2://USER:PASS@HOST:5432/aircraft_data
AWS_REGION=us-east-1
S3_BUCKET_NAME=flight-matrix-assets-xxx
COGNITO_JWKS=...
EOF
```

## Install (local)

```bash
# Logs and env dir
sudo mkdir -p /var/log/flight-matrix /etc/flight-matrix
sudo chown ubuntu:ubuntu /var/log/flight-matrix

# Units
sudo cp scripts/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# Scheduler + scraper (+ xvfb as scraper's Requires=)
sudo systemctl enable --now flight-matrix-scheduler flight-matrix-scraper

# Logs
sudo journalctl -u flight-matrix-scheduler -f
tail -f /var/log/flight-matrix/scraper.log
```

## Install (prod)

On the **scheduler** node (single-instance ASG, see
`infra/scraper/scraper_worker_stack.py:SchedulerASG`):

```bash
# Installed automatically by deploy_workers.sh --role scheduler
sudo systemctl enable --now flight-matrix-scheduler
```

On **worker** nodes (scraper ASG):

```bash
# Installed automatically by deploy_workers.sh --role worker
sudo systemctl enable --now flight-matrix-xvfb flight-matrix-scraper
```

## Aircraft search (OpenSearch)

The index is optional: with `OPENSEARCH_URL` unset the admin aircraft list keeps
using its SQL `LIKE` filter and the timer below does nothing. To turn it on:

```bash
# 1. Host prerequisite for OpenSearch. New hosts get it from bootstrap-vm.sh;
#    on an older one append to the sysctl file it already wrote.
grep -q max_map_count /etc/sysctl.d/60-flight-matrix.conf \
  || echo 'vm.max_map_count = 262144' | sudo tee -a /etc/sysctl.d/60-flight-matrix.conf
sudo sysctl -q --load /etc/sysctl.d/60-flight-matrix.conf

# 2. The container — its own name and volume, never a co-tenant's cluster.
#    Check `free -m` first and set OPENSEARCH_HEAP=256m if `available` is under
#    ~1.5 GB; 256 MB of heap is enough for a ~100k-aircraft index.
echo "OPENSEARCH_ADMIN_PASSWORD=$(openssl rand -hex 16)Aa1!" >> deploy/opensearch/.env
docker compose -f deploy/opensearch/docker-compose.yml up -d

# 3. Point the app at it
sudo tee -a /etc/flight-matrix/env <<'EOF'
OPENSEARCH_URL=http://127.0.0.1:9201
EOF
sudo systemctl restart flight-matrix-web

# 4. First population, then hand over to the timer. The credentials live in the
#    unit's EnvironmentFile, so borrow it rather than sourcing it into a shell.
reindex() {
  sudo systemd-run -P --uid="$USER" --working-directory="$PWD" \
    --property=EnvironmentFile=/etc/flight-matrix/env \
    "$PWD/.venv/bin/python" scripts/reindex_aircraft.py "$@"
}
reindex --full
sudo cp scripts/systemd/flight-matrix-reindex.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flight-matrix-reindex.timer

# Check
reindex --stats
systemctl list-timers flight-matrix-reindex.timer
```

`--full` re-indexes every row into the existing index; `--recreate` is only
needed when the analysers or a field type in `src/search/aircraft_index.py`
change, since OpenSearch cannot alter either in place.

## Operational notes

- Both scraper and scheduler log to `/var/log/flight-matrix/` (tailed by
  CloudWatch Logs agent in prod).
- Changing YAML: `sudo systemctl restart flight-matrix-{scheduler,scraper}`.
- `auto_terminate_unhealthy` and `auto_scale.enabled` in
  `config/scraper/base.yaml` default to `false`; override to `true` in
  the prod-stage YAML. They are also force-disabled on SQLite regardless.
