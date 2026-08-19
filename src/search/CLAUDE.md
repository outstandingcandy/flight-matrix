# src/search/

Two unrelated things live here. Read the module name before assuming which one
a change belongs to.

## Web search (`base.py`)

`SearchClient` / `SearchResponse` / `NullSearchClient` — the abstraction over
third-party *web* search (Tavily) used when enriching an aircraft the database
knows nothing about. Nothing in it touches OpenSearch.

## Aircraft full-text index (`opensearch_client.py`, `aircraft_index.py`, `aircraft_sync.py`)

The project's own OpenSearch index over `aircraft_static_info`, backing the
`search` parameter of `/api/admin/aircraft`.

```
aircraft_static_info ──▶ aircraft_sync.sync_aircraft_index ──▶ OpenSearch
                                                                   │
web_app.search_aircraft_registrations ◀── registrations only ◀──────┘
        │
        └──▶ SQL: WHERE asi.registration IN (…)  ── rows come from PostgreSQL
```

Invariants, all load-bearing:

- **The index is never the source of truth.** Queries ask for
  `_source: false` and get ids back; PostgreSQL supplies every rendered field.
  So the index can be dropped and rebuilt at any time, and a stale document
  costs a missing or extra match rather than a wrong row.
- **Search failure is not endpoint failure.** `get_client` returns `None` when
  unconfigured or when `opensearch-py` is absent, `search_registrations` raises
  `SearchError`, and `web_app.search_aircraft_registrations` turns both into
  `None` so the caller falls back to the old `LIKE` filter. Never let a search
  problem reach the client as a 500.
- **Freshness comes from `last_updated`, not dual writes.** Nine raw-SQL
  statements in `src/` write this table, one of them on the hot ADS-B path;
  `aircraft_sync` resyncs off the watermark stored in the index itself
  (`max_last_updated()` minus an overlap) instead. **A new write site that
  changes an indexed field must set `last_updated`** or search will not see it.
- **Index settings are static.** The n-gram analysers that give `B-12` →
  `B-1234` can only be set at creation. `ensure_index()` adds new fields to a
  live index but cannot change analysis or a field's type; that needs
  `scripts/reindex_aircraft.py --recreate`.

Deployment lives in `deploy/opensearch/docker-compose.yml` (its own container
name and volume — the host may already run another project's cluster) and
`scripts/systemd/flight-matrix-reindex.{service,timer}`.
