# Vision inspection and human review

A third manufacturing demo: one set of human reviews answers two questions, **how good are the parts**, and **how good is the model at judging them**?
The earlier calibration demos remain separate.

The images are SVG illustrations and the model predictions are fixed synthetic data.
No image classifier runs in this demo.
Reviews, revised assessments and reproducible reports use a real XTDB database.

## Run

```sh
docker compose up --build -d
```

Open http://localhost:5184.
XTDB is exposed on port 5460 and persists its data in the `vision-data` Docker volume.
Set `APP_PORT` or `XTDB_PORT` to change the host ports.
Each new batch is independent and remains accessible through its URL.

## Walkthrough

Twelve parts have passed through camera inspection using model version `vision-v1`.
The model rejected four and accepted eight.
Four early human reviews agree with the model, so the initial report shows 100% accuracy on those four reviewed parts.
The remaining eight outcomes are unknown.

1. Inspect **P-006**, a model rejection with a light surface scratch.
   Record it as **acceptable**: an unnecessary rejection, or false positive.
2. Inspect **P-005**, a model acceptance with a hairline crack.
   Record it as **defective**: a missed defect, or false negative.
3. Review the other parts, or use **Complete remaining reviews** for scripted assessments.
   This action preserves reviews you have already made.
4. Compare the production and model analytics.
   With the scenario's intended assessments, three of twelve parts are defective, and the model made three mistakes: two false positives and one false negative.
   Accuracy on this fully reviewed batch is 75%.
5. **Reproduce report** retrieves the initial database snapshot and reruns the query.
   The earlier report still shows four reviewed parts, one known defect and 100% accuracy on those four parts.
6. Save a current report, then revise a review.
   Reproduce the saved report to recover its earlier assessment and figures.

Cracks are defects; light scratches and removable dust are acceptable in this scenario.
You can record different assessments to explore the consequences.
The application compares predictions with the latest human judgement; it does not claim that human reviews are infallible.

## What the figures mean

Production quality counts defects among reviewed parts.
Before the whole batch has been reviewed, it does not extrapolate a defect rate to all parts or the wider production line.
Model quality compares original predictions with the current human assessments of the same parts.
Unknown outcomes are excluded from both the accuracy denominator and confusion matrix.

Reviews of accepted parts are necessary to find false negatives.
This demo ultimately reviews every part of a fixed batch, so later reviews do not change the population of parts under comparison.
All metrics are illustrative, not model benchmarks.

## Records and time

`part` records the part's production time and illustrated appearance.
`prediction` retains the model version, original score, prediction and line action.
`review` stores the human assessment, valid from the part's production time.
A later review can revise that assessment while XTDB system time retains what was previously recorded.
`report` stores a query snapshot timestamp, not precomputed metric totals.

Current and historical reports use `assessment.sql`.
The system-time basis applies to parts, predictions and reviews together.
Report metrics are calculated from the rows returned by that query.
The UI shows the actual historical SQL executed.

A review does not change the original line action or create a stock hold.
Released parts later judged defective need operational follow-up, which is outside this review and reporting demo.
All simulated parts remain in stock.
The supplied app uses a single worker with serialised database actions.

## Tests

Use the separate disposable playground, never the interactive database.

```sh
docker compose --profile test up -d playground
uv sync --locked
XTDB_TEST_DSN=postgresql://xtdb@localhost:5461/xtdb uv run pytest -q
```
