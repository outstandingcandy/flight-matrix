# Frontend conventions

Flight Matrix ships a server-rendered Flask + Jinja2 UI with Bootstrap 5 and
Leaflet. There is **no build step** — CSS and JS are hand-written and served
as static assets.

## File layout

```
web_templates/              # Jinja2 HTML
  <page>.html
web_static/
  css/
    <page>.css              # Per-page stylesheet
    style.css               # Site-wide rules
    home.css                # Home-page rules that style.css imports
  js/
    <page>.js               # Per-page module
    app.js                  # Shared helpers loaded on every page
```

The naming convention is one CSS file and (if needed) one JS file per
template, named the same as the template. Example:
`web_templates/aircraft_detail.html` ↔ `web_static/css/aircraft_detail.css`
↔ `web_static/js/aircraft_detail.js`.

## Rule: no inline `<style>` or `<script>` blocks

Every new template must:

- Link stylesheets in `<head>` via
  `<link rel="stylesheet" href="{{ static_url }}/css/<page>.css">`.
- Include scripts just before `</body>` via
  `<script src="{{ static_url }}/js/<page>.js"></script>`.

Third-party CDN resources (Bootstrap, Font Awesome, Leaflet) stay in the
template — they're load-order-sensitive and it's clearer to keep them there.

## Context processor

`{{ static_url }}` is injected by `inject_static_url()` in `web_app.py`. It
resolves against the `STATIC_BASE_URL` env var / `storage.public_base_url`
config key:

- **local dev** — empty, so it resolves to `/static` and Flask serves the
  files directly.
- **AWS target** — the CloudFront domain in front of the `web_static/` S3
  sync (`aws s3 sync ... $S3_BUCKET_NAME/static/`).
- **GCP target** — either empty (the compose `web` container serves
  `/static` itself, since `web_static/` is copied into the image) or the
  public GCS bucket URL if assets were uploaded there.

Do not hardcode `/static/...` paths.

## Existing state (v0.1.0)

Only a subset of templates have been split. The remaining ones still have
inline blocks and are tracked as follow-up work:

| Template                          | Inline lines | Status     |
|-----------------------------------|-------------:|------------|
| `403.html`                        |    0         | done       |
| `admin_dashboard.html`            |    0         | done       |
| `aircraft_detail.html`            |    ~0        | done       |
| `aircraft_type_detail.html`       |    ~0        | done       |
| `airport_board.html`              |    ~0        | done       |
| `home.html`                       |    0         | done       |
| `search_track.html`               |    ~0        | done       |
| `flight_schedules.html`           |  ~3 000      | to do      |
| `admin_aircraft_query.html`       |  ~1 900      | to do      |
| `admin_scraped_data.html`         |    ~600      | to do      |
| `admin_aircraft.html`             |    ~600      | orphaned   |
| `admin_reports.html`              |    ~500      | to do      |
| `admin_users.html`                |    ~380      | to do      |
| `admin_scraper_status.html`       |    ~310      | to do      |
| `user_filters.html`               |    ~300      | to do      |
| `user_dashboard.html`             |    ~290      | to do      |
| `auth_callback.html`              |    ~150      | to do      |
| `_token_refresh.html`             |    ~170      | to do      |

Contributions to knock these off are welcome — each one is a small,
well-scoped PR.

`admin_aircraft.html` is marked *orphaned* because no route renders it: its
paginated list over `/api/v1/admin/aircraft` now lives in
`admin_aircraft_query.html`, which opens on the list and switches to the
per-registration detail when a row is picked. Extract that one instead; this
file is a leftover copy pending deletion.

## Extraction procedure (for contributors)

1. Copy the full `<style>...</style>` block into
   `web_static/css/<template_name>.css` (replacing the template base name
   with the file name, e.g. `admin_users.html` → `admin_users.css`).
2. Copy the full `<script>...</script>` block(s) that do not reference a
   CDN into `web_static/js/<template_name>.js`.
3. Replace the inline blocks with:
   ```html
   <link rel="stylesheet" href="{{ static_url }}/css/<template_name>.css">
   <script src="{{ static_url }}/js/<template_name>.js"></script>
   ```
4. Render the page locally (`uv run python web_app.py --skip-auth`) and
   confirm it looks and behaves identically.
5. Sync the new static files to the CDN before the next deploy:
   `aws s3 sync web_static/ "s3://$S3_BUCKET_NAME/static/" --delete`.

## When inline CSS/JS is acceptable

Two narrow exceptions:

- **Jinja-interpolated values that must become CSS/JS.** Prefer `data-*`
  attributes on an element and read them from the external JS module.
  Example: `<div id="chart-data" data-flights="{{ flights | tojson }}">`.
- **Critical above-the-fold CSS**, if we ever care about first-paint budget.
  We don't today.
