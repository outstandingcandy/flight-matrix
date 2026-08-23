# Type checking

Flight Matrix uses `mypy` for static type checking. The long-term goal is
full `mypy --strict` compliance across `src/`. As of **2026-08-23** the
codebase is partially typed: `uv run mypy src/` reports **236 errors in 34
files (checked 123 source files)** in non-strict mode, concentrated in the
"god files" scheduled for refactor:

| File                                    | Errors |
|-----------------------------------------|-------:|
| `src/data/models.py`                    |     36 |
| `src/analysis/flight_agent.py`          |     25 |
| `src/services/aircraft_service.py`      |     21 |
| `src/services/report_service.py`        |     17 |
| `src/aircraft/cache.py`                 |     11 |
| `src/utils/jetphotos_simple.py`         |     10 |
| `src/services/filter_service.py`        |     10 |

Top error categories: `assignment` (76), `str` (49), `no-any-return` (38),
`return-value` (20), `arg-type` (19). `web_app.py` sits at the repo root
and is not included in `mypy src/`.

_Baseline for comparison — v0.1.0: ~91 source files with ~265 errors.
Between then and this refresh, coverage grew from 91 to 123 files while
total errors dropped by ~29._

## How to run it

```bash
# Default (non-strict) — the baseline CI check
uv run mypy src/

# Strict mode on a clean module
uv run mypy --strict src/core/

# Strict on everything (expect ~670 errors today)
uv run mypy --strict src/
```

Configuration lives in [`pyproject.toml`](../pyproject.toml) under
`[tool.mypy]`.

## Strict-clean modules

These modules pass `mypy --strict` with no errors and are gated at the
stricter level via `[[tool.mypy.overrides]]`:

- `src.core`

When you clean a module, add it here and to the overrides table.

### Non-strict clean modules

These pass the default (non-strict) `mypy src/` gate. They're the next
candidates to promote to strict as their remaining `type-arg` issues are
fixed:

- `src.data.db_manager`, `src.data.schema`, `src.data.snapshot_repo`,
  `src.data.cooldown_repo` (added in Phase 5.1).
- `src.utils.database` (now a re-export shim).

## The plan

Mypy cleanup is deliberately bundled with the Phase 5 module splits rather
than done as a separate upfront pass. Splitting `database.py` into
`db_manager.py` + `schema.py` + repositories, for example, is the natural
moment to add proper type hints — doing it before the split means writing
annotations that get immediately deleted.

The order matches the [refactor plan](architecture.md):

1. `src/core` — **done** (v0.1.0).
2. `src/auth` — 7 real type errors, small scope.
3. `src/data` (after Phase 5.1 split of `database.py`).
4. `src/services/*` (after Phase 5.2 repository refactor).
5. `src/web/routes/*` (after Phase 5.3 Blueprint split).
6. `src/analysis` (after Phase 5.4 `flight_agent.py` split).
7. Remaining (`src/scraper`, `src/reporting`, `src/notifications`, etc.).

## CI gate

CI runs `uv run mypy src/` in non-strict mode. That reports the current
type-issue count — it does **not** fail the build on existing errors. New
code should not add new errors. Once a module is strict-clean it's moved
under the strict override, and mypy *will* fail CI if regressions land.

## Dependencies that lack type stubs

Some dependencies (DrissionPage, kaleido, reverse-geocoder) don't ship type
stubs. These show up as `[import-untyped]`. We set `ignore_missing_imports =
true` globally to suppress the noise — a per-module `# type: ignore` is
preferred when a specific call needs annotation and the stubs are missing.
