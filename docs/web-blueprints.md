# Web blueprint migration

Flight Matrix's Flask app was originally a single `web_app.py` file with
~6 000 lines and ~90 `@app.route` decorators (5 971 lines / 88 routes as of
2026-08-23). Phase 5.3 splits it into blueprints under `src/web/routes/` so
each slice of the URL tree lives in its own module.

## Status (v0.1.0)

| Blueprint (target file)                   | Prefix / routes                       | Status      |
|-------------------------------------------|---------------------------------------|-------------|
| `src/web/routes/auth.py`                  | `/login`, `/logout`, `/auth/*`        | done        |
| `src/web/routes/ingest.py`                | `/api/v1/ingest/*` (scraper write path)  | done        |
| `src/web/routes/pages.py`                 | `/`, `/dashboard`, `/aircraft/…`, `/admin/…` HTML pages | to do |
| `src/web/routes/api_aircraft.py`          | `/api/v1/aircraft/*`                     | to do       |
| `src/web/routes/api_airports.py`          | `/api/v1/airports/*`                     | to do       |
| `src/web/routes/api_search.py`            | `/api/v1/search/*`                       | to do       |
| `src/web/routes/api_user.py`              | `/api/v1/user/*`                         | to do       |
| `src/web/routes/api_admin.py`             | `/api/v1/admin/*`                        | to do       |
| `src/web/routes/api_flight_schedules.py`  | `/api/v1/flight-schedules*`, `/api/v1/flight/*` | to do   |
| `src/web/routes/api_stats.py`             | `/api/v1/statistics`                     | to do       |

Infrastructure that was module-level in `web_app.py` already lives in
`src/web/`:

- `src/web/middleware.py` — `TTLCache`, `CustomDomainMiddleware`.
- `src/web/auth_shim.py` — centralises the `--skip-auth` / `AUTH_ENABLED`
  branching, provides the `login_required` / `admin_required` /
  `group_required` / `optional_login` / `flight_schedules_required` /
  `get_current_user` imports regardless of auth state.

## Migration procedure (per blueprint)

1. Create `src/web/routes/<name>.py` and define `bp = Blueprint("<name>", __name__)`.
2. Move the routes out of `web_app.py`. Replace `@app.route(...)` with
   `@bp.route(...)`. Keep the handler bodies identical.
3. Replace `from src.auth.decorators import ...` and the shim functions
   with `from src.web.auth_shim import ...`.
4. Register the blueprint in `web_app.py`:
   `app.register_blueprint(bp)`.
5. Replace any Chinese comments inside the migrated handlers with English
   equivalents (the legacy file has ~164 Chinese lines that were skipped
   in Phase 4.3 — they'll land in English as each blueprint moves out).
6. Run the smoke test (below). If it passes, commit.

## Smoke test after each migration

```bash
uv run python -c "
import sys; sys.path.insert(0, '.')
import os
os.environ['STAGE'] = 'local'
os.environ['SKIP_AUTH'] = 'true'
os.environ['DATABASE_URL'] = 'sqlite:///aircraft_data.db'
import web_app
app = web_app.app
routes = sorted(set(str(r) for r in app.url_map.iter_rules()))
print('total routes:', len(routes))
# Smoke-test a handful with the Flask test client
client = app.test_client()
for path in ['/', '/login', '/logout', '/admin']:
    r = client.get(path)
    print(path, r.status_code)
"
```

Before opening the PR, verify the route count matches the previous
baseline — a missing blueprint registration would silently drop routes.

## Shared state

`web_app.py` holds two module-level singletons that blueprints need to
access:

- `db_manager: DatabaseManager` — populated by `init_app()`.
- `config: YAMLConfig` — populated by `init_app()`.

For now blueprints import them directly from `web_app` (`from web_app
import db_manager, config`). A proper app factory that attaches them to
`app.extensions` or passes them via dependency injection is a follow-up
task — it should land after all blueprints are split out so the change
isn't fighting in-progress migrations.
