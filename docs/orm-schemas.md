# ORM / serialisation separation

## Current state (v0.1.0)

`src/data/models.py` mixes SQLAlchemy ORM definitions with serialisation:
14 of the models have a `to_dict()` method that the API layer calls to
produce JSON responses.

This is recognised tech debt (Phase 5.5 in the refactor plan) but has
been deliberately deferred. See the reasoning below.

## Target state

Two separate layers:

1. **ORM** (`src/data/models.py`) — pure SQLAlchemy Column / relationship
   declarations. No `to_dict`. No JSON awareness.
2. **Schemas** (`src/data/schemas.py`, to be created) — Pydantic v2
   classes mirroring each model's public shape. API handlers do
   `UserRead.model_validate(orm_user).model_dump()`.

## Why it's deferred

- The `to_dict` methods have accumulated behaviour (e.g. the
  `effective_*` merge properties on `AircraftStaticInfo` that consolidate
  `ad_*`, `ps_*`, and `jp_*` source prefixes). Porting requires a
  field-by-field review; it is not a mechanical change.
- There are ~4 caller files with ~dozens of call sites. Each call has
  an implicit contract about field names, null handling, and date format.
- **There is no test suite covering API responses today** (Phase 6.1).
  Rewriting the serialisation layer without regression tests would risk
  silently breaking downstream consumers (the frontend, email reports,
  any external integrations).

The right ordering is: lock in current behaviour with tests first
(Phase 6.1), then port to Pydantic schemas incrementally, then delete
`to_dict` once all callers are migrated.

## Migration procedure (for contributors)

Once tests exist, migrate one model at a time:

1. Add a Pydantic schema in `src/data/schemas.py`:
   ```python
   class UserRead(BaseModel):
       model_config = ConfigDict(from_attributes=True)
       id: int
       email: str
       ...
   ```
2. Add a golden-output test comparing `UserRead.model_validate(orm).model_dump()` against `orm.to_dict()`.
3. Update callers to use the schema. Keep `to_dict` in place.
4. Once no caller of `User.to_dict()` remains (grep must return 0),
   delete the method from the ORM.

Repeat per model.
