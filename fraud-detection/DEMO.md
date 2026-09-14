# Fraud Feature Store - demo runbook

Use the slides as claims, not a script. Pause long enough for people to scan each one, then use
these prompts for the story around it. **Patter** supplies context, **Show** proves the claim in
the live system, and **Land** is the one sentence to leave behind.

## Talk-track index

**Introduction**

- Fraud model and features
- Serving versus training
- Offline and online stores
- Point-in-time correctness
- Delayed chargeback outcomes
- Valid-time and system-time
- Synthetic data caveat

**Live system**

- Transaction, score, outcome
- Prediction before knowledge
- Features computed on read
- Query at transaction-time
- Inspect exact feature SQL
- Chargeback adds knowledge
- Insert at valid-time
- Re-score with new facts
- Preserve both time axes
- Rewind system-time
- Pin model version
- Reproduce before score
- Window pass for training
- Per-row known-time cutoff
- Compare hindsight

## Preflight (before the room is watching)

1. From `fraud-detection`, run `docker compose up -d`, then `./bin/seed.sh` against a fresh database.
   This loads 50,000 transactions and trains the model.
2. Use the pinned XTDB image in `docker-compose.yml`; no local node build is needed.
3. Open the UI: http://localhost:5173
4. Click **present** (top right) to open the slide pane. Arrow keys page slides.
5. Leave the sim **stopped** for now. You start it live in the serving beat.
6. Sanity check: the data table shows rows, the lens dock has Query / Correction / Training tabs.

Three things to keep in your head:
- **Keep the sim paused when demoing a correction.** Running, it auto-settles chargebacks fast
  and drains the pending case you're about to confirm.
- **Red-but-legit rows are expected.** The fraud flag fires at p >= 0.5 on a balanced model, so
  it is deliberately trigger-happy; those are honest false positives, and the recorded column is
  the arbiter. If asked, this is a threshold choice, not a bug.
- **Dates read ~6 months ago.** That's the sim clock, anchored in the past so the live stream has
  room to run forward. Don't apologise for it, just don't dwell.

## The walkthrough

### Introduction (spoken, ~90s, before the slides)

This is a small fraud-detection system.

// Arm it here

Accounts make transactions and a model assigns each one a probability of fraud.
The model does not work directly from the raw transaction: it uses features derived from the
account's history, such as how often the account has transacted recently, how unusual the amount
is, whether it is abroad, and whether the account has prior confirmed fraud.

Those features are needed in two different ways.
Serving needs them for one transaction immediately, training needs them across every historical
transaction in bulk.
Conventional feature stores often split those access patterns across online and offline stores.
That leaves two difficult jobs: keeping the two copies in sync, and reconstructing
each historical feature using only what was knowable when that decision was made.
Get the latter wrong and future information leaks into training, leading to models that perform
well offline but degrade in production.

Fraud makes that temporal problem concrete because the outcome is delayed.
The model scores a transaction now, but the bank may only learn that it was fraudulent when a
chargeback arrives days or weeks later.
That new fact changes what the system knows about the past and can change the features of later
transactions.

XTDB lets the serving and training query shapes read one bitemporal history.
Valid-time records when a fact is true in the domain; system-time records when the system actually
learned it.
This demo is attempting to answer: what becomes simpler when both timelines are directly queryable?

The model is deliberately just ordinary logistic regression, so each decision decomposes into visible
feature contributions.
The synthetic dataset exaggerates the fraud rate so the temporal effects are legible;
the point is getting the right historical data, not the model's quality.

// Then open the slides.

### Slide 1: "Fraud detection with no second store"

// Let the audience scan the premise. Do not read the bullets.

**Patter**

There are slides build into the demo, which is possibly a bit unconventional, on some level I wanted
it to work as a standalone artifact, but we'll see if that was wise. I don't want to read out the slides
so I'll give you a brief moment to read, we can always revisit if there are questions at the end.

// Pause

So this is the system, when you arm it it generates new transactions every few seconds.
Transactions arrive, XTDB supplies the historical features, and the model produces a fraud score.
The recorded outcome is separate: the bank may not learn it until a chargeback arrives later.
That gap between prediction and knowledge is what exercises both time axes.

**Show**

- Point at the live data table.
- Identify transaction, model score, and recorded outcome as three different things.

### Slide 2: "A feature is a query"

**Patter**

When a transaction arrives, XTDB reads the account history at that transaction's valid-time.
The query returns the feature values, and the model turns those values into `p(fraud)`.
Nothing had to publish those feature values ahead of the transaction.

> The model computes the probability; XTDB supplies the point-in-time feature vector.

**Show**

- Start the sim and let several rows arrive.
- Point out `model p(fraud)` versus `recorded`; a pending row is suspicion before ground truth.
- Select a row and open **Query**.
- Show the valid-time bound and trailing aggregates in the serving SQL.

### Slide 3: "A correction is just an insert"

**Patter**

A chargeback is a new fact learned weeks after the transaction.
It belongs at the moment the fraud happened, but the system must also retain when it learned that.
Because features are queries over the valid-time history, there is no materialized feature backfill 
to run.

**Show**

- Pause the sim so the pending case stays stable.
- Select a ⚠ account and open **Correction**.
- Note the before score, then confirm its chargebacks.
- Show the label changing in account history.
- Show `prior_confirmed_fraud` rise and the same model re-score the borderline transaction.

### Slide 4: "Reproduce a decision, exactly"

**Patter**

A situation occurs a few weeks later where a customer complains that a transaction was
declined and we have to explain the decision, the data and the model have moved on precisely
because of the chargebacks we just confirmed. Re-scoring today gives a different number and
answers a different question.

The before card is a decision made before confirmation.
To reproduce it after the correction, we need both the data basis it saw and the model version
that turned those features into a score.
Rewinding system-time restores the feature vector, while the recorded model version restores the
scoring function. Bitemporality also lets us reconstruct an entire training set as it was knowable
at time T, so we can even retrain from that historical basis if required.

**Show**

- Compare the **before**, **after**, and **reproduced** cards.
- Point out that **reproduced** uses `SYSTEM_TIME AS OF` before the chargeback.
- Point out the pinned model version beside the reproduced score.
- Show that the same model and data basis land on the before score.

### Slide 5: "Training is an as-of join"

**Patter**

Training repeats the same historical question for every row in the dataset.
Each training row joins the fraud-status interval that contains its decision time.
System time pins the database state used by the whole extract.
Serving is a point lookup and training is a window pass: different query shapes over one
bitemporal history, rather than two stores to synchronize.

> A model must train on exactly the information it will have when it predicts, no more.

**Show**

- Open **Training** and show the extraction query.
- Point out `p.txn_ts < t.txn_ts` in the trailing self-join: the anchor never counts itself.
- Point out `s._valid_time CONTAINS r.txn_ts`: each row selects the fraud status available at its decision.
- Compare **as known then** with **with hindsight**.
- Pick one row where hindsight includes a chargeback that had not settled yet.
- Optionally train; the expensive part is feature extraction, not fitting logistic regression.

### Close

**Patter**

Three things needed both time axes. A correction recorded a late fact at the time it was true.
A replay read one past decision's basis.
Training selected the status interval for each row, using a shared database basis for reproducibility.

Conventional setups often spread that across three systems: the operational store, an offline
store for training, and an online store for serving. If your application data already lives in
XTDB, the feature layer stops being a separate system.

## Q&A cheat sheet

**What is logistic regression, in one breath?**
A weighted sum, squashed into a probability.
Each feature gets one weight the model learned from history — how much that signal matters in
general — and scoring multiplies each feature value by its weight, adds them up along with a
baseline, then squashes the total onto 0-1.
Training is just the search for weights that make fraudulent rows come out high and legitimate
rows come out low.
We use it because that sum is readable: the decision decomposes into per-feature contributions you
can point at, which a gradient-boosted tree would not give you without reaching for SHAP.
It scores worse than the fancier options; being able to explain a decision is the point here.

**What's the difference between a feature value, a weight, and a bar?**
Three different things.
The feature value is a fact about this account at this moment (4 transactions in 24h); the weight
is the model's general belief about that signal, fixed until you retrain; the bar is what happened
when they met — weight times standardised value, for this decision only.
So weights are the model's opinions, feature values are the account's facts, and the bars are the
result of the two meeting.

**What's AUC, and why 0.99?**
The chance that a randomly chosen fraudulent transaction scores higher than a randomly chosen
legitimate one — 0.5 is a coin flip, 1.0 is perfect ranking.
It's the metric here because fraud is rare: with 5% fraud, a model that says "legit" to everything
is 95% accurate and worthless, whereas AUC only looks at ordering.
It's also threshold-independent, so it says nothing about where the 0.5 cutoff sits.
0.99 is high because the synthetic generator plants an obvious signal (fraud amounts 2.5-9x the
account's typical spend, 70% foreign); real fraud models land nearer 0.85-0.95.
Run `uv run --extra analysis python explain_auc.py` for a two-panel picture of this against the live model.

**What's an as-of join / why must features be at the row's event time?**
A model must train on exactly what it'll have when it predicts, no more. The reason you *know* a
transaction was fraud (the chargeback) is the same event that updates the feature, so using the
current value smuggles the answer into the inputs. Great offline, collapses live. It's the
train/serve skew problem at its root.

**Why join status intervals as well as pinning system time?**
Each training row has a different decision time, so it needs the status interval containing that instant.
The system-time basis applies to the extract as a whole and preserves the database state used for that run.
Later corrections to a status interval can change a fresh extract without changing one pinned to the earlier basis.

**Why do the Query pane numbers not match the decision bars?**
The pane shows raw feature values; the bars show contributions = coefficient x standardised(value),
in log-odds. Different quantities, different units. A feature at 0 can still contribute because 0
may be below the population mean.

**What's the intercept?**
The model's baseline log-odds before any feature moves it. Strongly negative here because fraud is
rare; a transaction needs enough positive contributions to overcome it. (Tuned up a bit by
class_weight=balanced, so read it as "the model's baseline", not the raw base rate.)

**Fraud 0.81 but recorded legit, is that wrong?**
No. That row genuinely isn't fraud; the model scored it high and was wrong, a false positive. The
two columns are meant to disagree sometimes; the recorded chargeback outcome is the truth. Flag at
0.5 on a balanced model is deliberately trigger-happy.

**Why is nothing recorded as fraud in the live view?**
Chargebacks take 2 to 30 days to settle; the top of the feed is minutes old, so nothing recent is
settled yet. Settled frauds exist (thousands) but are spread thin; page back ~10+ days to see
`fraud`. On the live head you see `pending` next to red model scores, which is the story.

**Is it really the same query for training and serving?**
No. Serving is a point lookup and training is a window pass, so they are deliberately different
query shapes. One bitemporal store removes the offline/online synchronization problem; parity
between the query implementations still needs contract tests.

**Why have both `label` and `fraud_status`?**
`label` says the transaction was fraudulent from its event time.
`fraud_status` says the confirmation was available from its arrival time.
They are written together in XTDB.
Training joins status intervals to each decision time; current scoring and system-time replay read outcomes from `label`.
A reversal closes the confirmed interval, so decisions before and after it get the appropriate counts.
System time still pins the database state used by an extract or a replay, including any subsequent corrections to those intervals.

**Why exclude recent transactions from training?**
Chargebacks settle up to 30 days after a transaction. A recent `legit` label may simply be an
unsettled fraud, so training excludes the maximum settlement horizon. That is the standard tradeoff
between label maturity and recency; bitemporality makes it explicit but does not remove it.

**Does computing features on read slow down as history grows?**
The features use bounded 24-hour, 30-day, and 90-day windows. Work therefore follows an account's
activity in the requested window rather than its lifetime, and the temporal index can prune by the
valid-time bounds. Hot paths can still materialize derived values while retaining the query as the
definition and audit path.

**Why pgwire for serving and Flight SQL for training?**
They are two transports into the same node. Pgwire suits small row-oriented point lookups; Flight
SQL returns Arrow batches for a bulk training extract. Different access shapes do not require
different stores.

**Why not use a dedicated feature store?**
Dedicated systems bring low-latency online materialization and mature registry and monitoring
tooling. This demo claims a different advantage: one temporal source of truth makes corrections,
point-in-time training, and audit directly queryable. It is a correctness argument, not a claim
that XTDB wins every feature-store workload.

**How much leakage does the comparison quantify?**
It measures corruption of `prior_confirmed_fraud`: how many rows differ and by how much when
hindsight is allowed. It does not train a second leaky model or quantify AUC damage, and the size
of the gap comes from synthetic settlement delays. Treat it as an existence proof, not an industry
statistic.

**Can a chargeback be reversed?**
The status timeline and queries handle reversals: write a false status from the time the reversal becomes available and correct the outcome in `label` in the same transaction.
Decisions before the reversal keep their confirmed-fraud count; decisions afterwards do not count it.
The tests cover this sequence, although the UI only offers confirmation.

**What is serving latency?**
Measure it on the running version and machine.
The demo computes features from history on each request, without a cache or precomputed vectors.
Its purpose is to make the SQL and temporal behaviour visible, not to establish a production latency target.

**Does it scale?**
The default seed has 50,000 transactions.
Increasing it increases the work in the trailing joins and the memory needed for ingestion and training.
Measure those costs for the intended workload before drawing a production conclusion.

## Recovery / gotchas

- For connection problems, run `docker compose ps` and `docker compose logs xtdb api ui`.
- Keep the sim stopped while showing a correction, so it does not automatically settle the selected case.
  If a confirmation has already landed, choose another pending case.
- The database runs on simulated system time.
  Do not run tests or make wall-clock-stamped writes against the demo node; use the separate playground.
- To reset this demo deliberately, run `docker compose down -v`, `docker compose up -d`, then `./bin/seed.sh` from this directory.
  This deletes this Compose project's demo data and saved models.
  Use a new Compose project and alternate ports if you need to retain the existing dataset.
- For a smaller fresh dataset, use `./bin/seed.sh 10000`.
  Seeding an existing database will fail when the replay tries to move system time backwards.
