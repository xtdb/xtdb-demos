# Fraud detection

A fraud detection demo with transaction history, late chargebacks, and SQL features for training and scoring.
It shows how to reconstruct a decision after the data changes, using the original model and database timestamp.

The demo is pinned to **XTDB 2.2.0-rc0**, a published release candidate.
Docker Compose runs XTDB, a Python API, and a React UI; no XTDB source checkout is required.

## What it shows

The model uses four features computed from transaction history and account attributes: `amount_zscore`, `txn_count_24h`, `foreign`, and `prior_confirmed_fraud`.
Training needs the feature values available when each historical transaction was scored.
Later fraud confirmations must not leak into those inputs.

The demo lets you:

- Score incoming transactions and inspect the SQL behind each feature.
- Confirm a chargeback and see how it changes a later transaction's score.
- Reproduce the original score using its saved model and database timestamp.
- Build historical training data using the fraud confirmations available at each decision.

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

### Training queries

Bulk training uses self-joins to compute trailing features, excluding the transaction being scored from its own history.
The pinned XTDB image does not support the temporal window frames these features would require.
The tests compare the training and serving feature values.
The prior-fraud query joins `label FOR ALL VALID_TIME` on `_valid_time CONTAINS` each transaction's `txn_ts`.
A second join to the same table reads current labels for the hindsight count, while the query-level system-time basis applies to both.

### Without Docker

The stack is three ordinary processes if you'd rather run them directly: an XTDB node on pgwire `:5444` and FlightSQL `:9834`, `uvicorn api:app --reload`, and `pnpm dev` in `ui/`.
`XTDB_DSN` and `XTDB_FLIGHT_URI` override the connection targets; both default to localhost.

```bash
uv sync
uv run uvicorn api:app --port 8000 --reload
```

## Tests

Start the separate test database before running the suite:

```bash
docker compose --profile test up -d playground
uv run --locked python -m unittest discover -s tests -t .
```

Database tests use the playground on port 5445 and skip if it is unavailable.
Each test connection uses an isolated database.
Do not run tests against the demo database: its system timestamps follow the simulation clock, and a write at the current wall-clock time can prevent the simulation from advancing.

## Layout

- `api.py`: FastAPI surface (the only web entrypoint).
- `registry.py`: the feature layer: each feature is a point-in-time SQL query; `serve()`, `vector()`, `_with_basis()`.
- `model.py`: the SQL extraction, logistic-regression training (`train_window`), `load_latest`, `explain`.
- `sim.py`: live simulator (append mode continues the backfilled history).
- `sim_control.py`: in-process controller so the sim starts/stops from the web app.
- `backfill_adbc.py`: bitemporal replay of history in learned-at order, plus seeded pending chargebacks.
- `queries.py`: shared `connect()` (pgwire, with the StrDumper XTDB needs).
- `ui/`: the React app (`src/components`, `src/views`).

## Data model

- `account`: per-account attributes (`home_country`, `tier`), read `FOR VALID_TIME AS OF T`.
- `txn`: the event log. `txn_ts` is the transaction instant; valid-time is open-ended from it.
- `label`: the classification history for each transaction, keyed by its transaction ID.
  `is_fraud` becomes true in valid time when fraud is confirmed, and false if the classification is overturned.
  Keep the latest interval open-ended until another classification replaces it.
  Bulk training selects the interval containing each decision time; training targets and hindsight use the current classification.
  System time preserves earlier database states for replay and reproducible training.
- `pending_chargeback`: frauds not yet confirmed; the correction-lens candidates.
- `sim_clock`, `model_registry`: the sim's current instant, and trained-model metadata + joblib path.

Event time is the explicit `txn_ts` column, not `_valid_from`.
They hold the same instant today, but valid-time answers "when was this fact true", which stops being the event instant the moment a transaction acquires a lifecycle.
Both the training windows and the serving queries read `txn_ts`, so the batch and online paths can't drift apart.

## Caveats

- The generated transactions and model illustrate historical feature queries and decision replay.
  They are not intended for production fraud detection.
- Seeding requires an empty demo database because it replays transactions at historical system timestamps.
  Docker Compose keeps the demo's data in its own volumes.
- The default dataset contains 50,000 transactions.
  Larger datasets require more memory and take longer to seed and query.

## See also

- [`point-in-time-feature-extraction`](https://docs.xtdb.com/adbc/guides/point-in-time-feature-extraction): the loan-default sibling, ADBC/FlightSQL flavour.
- [Time in XTDB](https://docs.xtdb.com/about/time-in-xtdb): the underlying bitemporal model.
