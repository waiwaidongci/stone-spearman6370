# Schema migrations

## v2 — offline three-way merge

Added by SQLAlchemy `Base.metadata.create_all` at startup (non-destructive,
new tables only):

- `merge_sessions`: one offline merge per study. Holds the common base
  worksheet snapshot plus both laboratory revision packages, the computed
  three-way plan, carried adjudications, drift counter and lifecycle status
  (`awaiting / conflicts / resolved / applied / superseded`).
- `idempotency_records`: keyed by `(idempotency_key, action)` so retried
  package uploads, adjudications and applies never duplicate work.

No existing table is altered; historical `runs` snapshots and reviews stay
byte-identical. The `checksum` helper now deep-sorts dict keys, so values
dictionaries hash identically regardless of insertion order; checksums stored
on historical runs remain valid because they were computed once at freeze time
and are only re-verified against their own stored snapshot.

## v1

Defined by the SQLAlchemy models and created at startup via
`Base.metadata.create_all`. For this local small application no destructive
migration is run. Back up named db-data before modifying schemas.
