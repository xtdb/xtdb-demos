# Fraud detection

A fraud detection demo with transaction history, late chargebacks, and SQL features for training and scoring.
It shows how to reconstruct a decision after the data changes, using the original model and database timestamp.

The demo is pinned to **XTDB 2.2.0-rc0**, a published release candidate.
Docker Compose runs XTDB, a Python API, and a React UI; no XTDB source checkout is required.

## The thesis

The hard part of a feature store isn't storing features, it's retrieving them *as they were known at a past instant*.
Get it wrong and you leak the future into your training set: the model scores beautifully offline and rots in production.
Mainstream stores solve this with two stores — an *offline* one for training and an *online* one for serving — then spend enormous effort keeping them consistent (train/serve skew).

XTDB collapses that split. Every row carries a valid-time interval and a system-time interval, so:

| Concern | How XTDB handles it |
| --- | --- |
| Point-in-time features (no leakage) | `FOR VALID_TIME AS OF T` — the account's history as the world was at T |
| Reproducible re-runs despite later corrections | `SETTING DEFAULT SYSTEM_TIME AS OF <ts>` rewinds *what we knew* |
| Online serving with no separate store | the same feature SQL at `T = now` — no skew by construction |
| Late data / corrections | a chargeback self-places at the original event's valid-time; system-time records when we learned it |

A **feature is a query** (see `registry.py`): `amount_zscore`, `txn_count_24h`, `foreign`, `prior_confirmed_fraud`.
The `label` table records fraud outcomes, valid from the transaction's event time.
The `fraud_status` table records when each status was available to use, so bulk training can join its valid-time intervals to decision times.
Both are written in the same XTDB transaction; system time preserves earlier database states for reproducible extracts and score replay.

## The story (two lenses on one live dataset)

1. **Correction lens (the spine)** — a late chargeback confirms a past fraud.
   A *later* transaction's `prior_confirmed_fraud` rises, so the same model scores it higher; `SYSTEM_TIME AS OF` before the chargeback reproduces the original decision exactly.
   This fuses "late data" and "decision audit (now vs then)" — audit is the *rewind half* of this lens.
2. **Reproducible-training lens** — retrain as-of a past system-time and diff against the live model.
   Compliance framing: prove what the model was trained on.

Plus a **live serving** beat: score each incoming transaction inline, so "online serving, no skew" is shown, not just asserted.

See `DEMO.md` for presenter notes.

## Run it

```bash
git clone https://github.com/xtdb/xtdb-demos
cd xtdb-demos/fraud-detection

# 1. bring up node + API + UI
docker compose up -d

# 2. load a deterministic bitemporal dataset and train a model
./bin/seed.sh
```

The first start builds the API and UI containers and downloads their dependencies.

Then open <http://localhost:5173> and hit **▶ sim** to start the live stream.
New transactions flash in; a chargeback flips a row to fraud.
Pin the **system-time** cursor to rewind knowledge and watch a correction revert.

Copy `.env.example` to `.env` to change ports (to run alongside another node), or to raise the node's heap.
The default 4g is comfortable for the 50k-row seed; the full 300k demo set wants `XTDB_HEAP=10g`.

Data lives in the `xtdb-data` volume and survives restarts.
`docker compose down -v` wipes it; re-run `./bin/seed.sh` afterwards, which is cheap.

The `fraud_status` model requires a fresh seed if your database was created by an earlier version of the demo.
Use a new Compose project and unused ports to keep an existing dataset; restarting the API alone does not populate historical status intervals.

### Why the extract is a self-join

The natural way to write a trailing feature is an aggregate window frame:

```sql
COUNT(*) OVER (PARTITION BY account_id ORDER BY txn_ts
               RANGE BETWEEN INTERVAL 'PT24H' PRECEDING AND CURRENT ROW)
```

That's SQL:2011, but interval-valued RANGE offsets aren't in a published XTDB release yet ([xtdb/xtdb#5809](https://github.com/xtdb/xtdb/issues/5809)) and a stock image rejects it with `no viable alternative at input`.
So `model._window_sql` says the same thing with a band self-join from each anchor to its own account's prior transactions, which runs anywhere.
`tests/test_trailing_window_contract.py` pins the values against hand-computed figures, and asserts the extract stays free of window functions.

The band join is slower than the frame version, which is invisible at demo scale.

### Without Docker

The stack is three ordinary processes if you'd rather run them directly — an XTDB node on pgwire `:5444` and FlightSQL `:9834`, `uvicorn api:app --reload`, and `pnpm dev` in `ui/`.
`XTDB_DSN` and `XTDB_FLIGHT_URI` override the connection targets; both default to localhost.

```bash
uv sync
uv run uvicorn api:app --port 8000 --reload
```

## Tests

```bash
uv run python -m unittest discover -s tests -t .
```

Most are contract tests over the generated SQL and the API call shapes, and need nothing running.
The label-version tests in `tests/test_label_version_contract.py` exercise real bitemporal semantics against a node, and skip unless one is up:

```bash
docker compose --profile test up -d playground
```

That's a separate in-memory node on `:5445` on purpose.
It hands out an isolated database per connection name, so those tests can write their own system-time timeline — they can't use the demo node, whose log lives on the sim clock where system-time only ever moves forward.

## Layout

- `api.py` — FastAPI surface (the only web entrypoint).
- `registry.py` — the feature layer: each feature is a point-in-time SQL query; `serve()`, `vector()`, `_with_basis()`.
- `model.py` — the SQL extraction, logistic-regression training (`train_window`), `load_latest`, `explain`.
- `sim.py` — live simulator (append mode continues the backfilled history).
- `sim_control.py` — in-process controller so the sim starts/stops from the web app.
- `backfill_adbc.py` — bitemporal replay of history in learned-at order, plus seeded pending chargebacks.
- `queries.py` — shared `connect()` (pgwire, with the StrDumper XTDB needs).
- `ui/` — the React app (`src/components`, `src/views`).

## Data model

- `account` — per-account attributes (`home_country`, `tier`), read `FOR VALID_TIME AS OF T`.
- `txn` — the event log. `txn_ts` is the transaction instant; valid-time is open-ended from it.
- `label` — one row per transaction, `is_fraud` bool. Re-inserted on a chargeback (same `_id`) → a real system-time correction. **Source of truth for fraud.**
- `fraud_status` — one status timeline per transaction, with the same `_id` as `label`.
  Its valid-time starts when the status becomes available: a confirmation starts a true interval; an overturn starts a false interval.
  Bulk training joins the interval containing each decision time.
- `pending_chargeback` — frauds not yet confirmed; the correction-lens candidates.
- `sim_clock`, `model_registry` — the sim's current instant, and trained-model metadata + joblib path.

Event time is the explicit `txn_ts` column, not `_valid_from`.
They hold the same instant today, but valid-time answers "when was this fact true", which stops being the event instant the moment a transaction acquires a lifecycle.
Both the training windows and the serving queries read `txn_ts`, so the batch and online paths can't drift apart.

## Known rough edges

- Seeding requires an empty demo database because it replays writes at historical system timestamps.
  The Compose project `xtdb-fraud-detection` uses its own volumes; it does not reuse an existing `xtdb-feature-store` dataset.
- The bulk feature queries compute from history on demand.
  Larger seeds take more memory and time; the default seed has 50,000 transactions.
- Tests use a separate playground on port 5445.
  Start it before running the suite, or the database tests will skip.
- The model and generated transactions are for demonstration, not production fraud decisions.

## Provenance

Moved from [`xtdb/xtdb-feature-store`](https://github.com/xtdb/xtdb-feature-store/tree/d54325c) at commit `d54325c`.
The demo code is maintained here; the original repository retains its earlier history.

## See also

- [`point-in-time-feature-extraction`](https://docs.xtdb.com/adbc/guides/point-in-time-feature-extraction) — the loan-default sibling, ADBC/FlightSQL flavour.
- [Time in XTDB](https://docs.xtdb.com/about/time-in-xtdb) — the underlying bitemporal model.
