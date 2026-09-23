# Manufacturing inspection with XTDB

A camera calibration correction changes which released parts need review.
XTDB applies the correction to the relevant inspection period and preserves the data behind the original release decisions.
The dashboard shows both assessments, their calculations and the SQL that produced them.

## Run

Install Docker with Compose, then run from this directory:

```sh
docker compose up --build -d
```

Open [the dashboard](http://localhost:5180).
The first startup downloads the images and waits for XTDB to become healthy.
If it does not start, check `docker compose logs app xtdb`.

1. **Run batch** creates 24 simulated inspections between 08:00 and 12:00 UTC on 15 September 2026.
2. **Release passing parts** records 19 releases and saves the database timestamp used for the assessment.
3. **Apply correction** changes the camera's calibration from 0.050 to 0.051 mm per pixel, effective from 09:00 up to 11:00.
   Five released parts now need review.
4. **Original assessment** re-runs the query at the saved database timestamp.
   Every part has its original assessment again; the release records and current review flags remain visible.

Select **P-007**, inspected at 09:00, to see a 201-pixel observation change from 10.050 mm to 10.251 mm.
The upper tolerance is 10.20 mm.
Compare it with **P-019** at 11:00: the same pixel measurement is outside the correction interval and stays unchanged.
**P-013** moves from failing to passing, but is not automatically released.

The browser remembers the last batch, and each batch has its own URL.
Running a new batch creates independent records and retains previous batches.
The named Docker volume preserves the database across container restarts.
Use `docker compose down` to stop the demo while retaining its data.

The app uses port 5180 and XTDB uses port 5456, bound to localhost.
Override `APP_PORT` or `XTDB_PORT` if needed:

```sh
APP_PORT=5181 XTDB_PORT=5458 docker compose up --build -d
```

## What is stored

| Table | Purpose |
| --- | --- |
| `inspection` | Part, camera, product, inspection time and raw pixel width. |
| `calibration` | Millimetres per pixel, with valid time describing when the calibration applied. |
| `product_spec` | Minimum and maximum widths, effective over time. |
| `release` | The action taken, its recorded width, release time and assessment basis. |
| `demo_run` | The demo's workflow stage and saved assessment basis. |

Each run has its own camera and product identifiers so repeating the scenario cannot affect earlier batches.
Observations and release records are never rewritten by the correction.
The application derives the review list from the latest assessments of released parts.

## The temporal query

[`assessment.sql`](assessment.sql) joins each inspection to the calibration and product specification whose valid-time intervals contain its inspection time.
The left joins keep inspections visible if applicable calibration or specification history is missing; their assessment is `unknown`, and they cannot be released.
Widths are rounded to three decimal places before comparison with inclusive tolerances.

`FOR ALL VALID_TIME` makes the historical configuration intervals available to those joins.
There is no scan across all system-time versions.
Every assessment query uses one explicit system-time basis for all its tables:

```sql
SETTING DEFAULT SYSTEM_TIME AS OF TIMESTAMP '...'
```

The current assessment uses the latest database transaction timestamp.
The original assessment uses the timestamp saved before writing the releases.
The dashboard shows the executed query and its run-ID parameter.

The correction is an ordinary write to the same calibration ID, with an explicit valid-time interval:

```sql
INSERT INTO calibration (_id, _valid_from, _valid_to, mm_per_pixel)
VALUES (
  'RUN_ID/camera',
  TIMESTAMP '2026-09-15T09:00:00Z',
  TIMESTAMP '2026-09-15T11:00:00Z',
  0.051
)
```

XTDB records that write at the actual time it is submitted.
The correction applies to the earlier inspection period while a query at the original system-time basis still sees the previous calibration.
No application-maintained copy of the original assessment dataset is needed.

## Development and tests

The app is FastAPI with a static HTML, CSS and JavaScript frontend.
Docker builds from `uv.lock`; there is no frontend build step.
To run the app locally with reload, install [uv](https://docs.astral.sh/uv/) and use:

```sh
docker compose up -d xtdb
uv sync --locked
uv run uvicorn app:app --reload --port 5181
```

The default local connection uses XTDB on port 5456.
Set `XTDB_DSN` to change it.

Tests use a separate playground node and a fresh database for each test:

```sh
docker compose --profile test up -d playground
uv run --locked pytest -q
```

Wait for the playground's server-started log before running tests on a first startup.
Tests fail if the playground is unavailable; database checks are never silently skipped.
Set `TEST_PORT` for both Compose and the test command to change the default test port, 5457.
Coverage includes the correction's start/end boundaries, inclusive tolerances, replay after calibration and specification changes, preserved release records, missing history, independent batches and the HTTP workflow.

## Scope

Camera measurements are generated, and the calibration is a deliberately simple scalar conversion.
There is no trained vision model, camera integration or physical release control.
The review list is an application query, not an automatic database trigger or a completed recall workflow.

Run one application worker against the dedicated demo database.
It serializes demo actions in that process; it is not a concurrency design for a production release system.
The generated numbers illustrate the temporal behaviour, not manufacturing accuracy or safety requirements.
