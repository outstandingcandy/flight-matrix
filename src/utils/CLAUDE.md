# src/utils/

Cross-cutting utilities. Over time, modules here move out to more
specific packages — but don't move them prematurely. The only hard rule
is that new utilities go to the most specific package that fits, not
here.

## Current modules

| File | Kept here? | New home (if applicable) |
|------|------------|--------------------------|
| `database.py` | No — **shim** | `src/data/*` (Phase 5.1, done) |
| `yaml_config.py` | Yes, for now | `src/config/` is the planned home |
| `logging_config.py` | Yes | — |
| `retry.py` | Yes | — |
| `sql_utils.py` | Yes | Could move to `src/data/` |
| `rds_iam_auth.py` | Yes | `src/data/` candidate |
| `google_search.py` | Yes | `src/search/` or `src/integrations/` |
| `jetphotos_simple.py` | Yes | `src/integrations/` candidate |
| `compat.py` | Yes | — (backward-compat stub) |

## Why the reorganisation is deferred

The Phase 5 plan proposed splitting utils/ into `src/config/`,
`src/integrations/`, and `src/core/`. After doing the high-ROI splits
(`src/data/`, `src/web/`, flight_agent translation), the remaining
modules don't have a compelling reason to move yet:

- `yaml_config.py` is 660 lines and self-contained; moving it costs
  rewiring ~10 callers for no architectural benefit beyond cosmetics.
- `logging_config.py`, `retry.py`, `sql_utils.py` are small, leaf
  modules — their current location is fine.
- `jetphotos_simple.py` and `google_search.py` are genuine integration
  helpers but only have 1-2 callers each.

When a migration does happen, follow the pattern from Phase 5.1:

1. Create the new module in its target package.
2. Replace the `src/utils/<name>.py` file with a re-export shim that
   imports from the new location and updates `__all__`.
3. Optionally add a `DeprecationWarning` on import.

The shim pattern means no caller has to change at migration time.
