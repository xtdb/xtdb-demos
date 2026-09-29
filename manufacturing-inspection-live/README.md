# Live manufacturing inspection

A second version of the [manufacturing inspection demo](../manufacturing-inspection), with a simulated production line and a guided calibration investigation.
The original demo is unchanged.

## Run

```sh
docker compose up --build -d
```

Open http://localhost:5182.
XTDB is available on port 5458 and stores its data in the `line-data` Docker volume.
Use `APP_PORT` and `XTDB_PORT` to override the host ports.

## Walkthrough

The shift begins with 18 inspections from 08:00 to 10:50.
While the page is visible, a new part arrives every four seconds, advancing the simulated shift by ten minutes.
The camera measures a width in pixels, and the calibration converts it to millimetres.
Parts measuring 9.800–10.200 mm pass and are released into stock; other parts are held.

1. **Report calibration problem.** A check has found an incorrect calibration between 09:00 and 11:00.
   The demo identifies 12 candidate parts, showing their original decisions.
2. **Apply correction & reassess.** The conversion for that period changes from 0.050 to 0.051 mm per pixel.
   Five released parts now fail, one held part now passes, and six results are unchanged.
3. **Place released parts on hold.** Record five new quality holds without changing the original releases.
   The held part that now passes stays held, pending a separate release decision.
4. **Why was this released?** Select P-007 and reproduce its original assessment using the database snapshot recorded with that decision.
   Its 201 px reading originally gave 10.050 mm; the corrected assessment gives 10.251 mm.

P-019 has the same reading but was inspected at 11:00, outside the corrected period, so it still passes.
Production continues during the investigation using the calibration valid outside the affected period.
Pause the line to explore at your own pace.
A new shift has its own records; the previous shift remains accessible using its URL.

## Data and history

`inspection` keeps the original camera reading and inspection time.
`calibration` and `product_spec` are joined at that inspection time using valid time.
`inspection_decision` records the assessment, automatic release or hold, and database snapshot used to decide.
`incident` tracks the investigation, and `stock_hold` records the resulting containment action.

The assessment in `assessment.sql` is used both for current results and historical replay.
Replay applies one system-time snapshot to every table in that query.
The UI can show the actual SQL executed against XTDB.
Reassessment updates the calibration history, preserving the original inspection and decision records.

The scenario assumes a known affected interval and a reliable corrected conversion.
All parts remain in finished-goods stock, so containment needs no shipping or recall workflow.
The measurements are deterministic synthetic data, not a machine-vision model.
The browser drives the simulation; it pauses when hidden or closed.
The app uses one process and serialises actions within it; run the supplied single-worker configuration.

## Tests

Use a separate, disposable playground so tests cannot alter the interactive demo.

```sh
docker compose --profile test up -d playground
uv sync --locked
XTDB_TEST_DSN=postgresql://xtdb@localhost:5459/xtdb uv run pytest -q
```

Each test uses independent shift IDs.
Tests cover reassessment, containment, historical replay, interval boundaries, isolation, missing calibration, and duplicate actions.
