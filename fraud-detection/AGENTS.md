# Agents

Instructions for AI agents working in this repo.
If something here would be useful to a human too, put it in README.md and point here.

Interpret MUST, MUST NOT, SHOULD, SHOULD NOT per RFC 2119.

Read README.md first — it covers the thesis, the layout, and how to run the stack.
This file covers what you can't infer from the code.

## What this is

A demo, and it is presented live.
The audience reads the SQL on screen, so the SQL is the product: clarity in a query matters more here than in ordinary application code.
Every endpoint that returns a number also returns the SQL that produced it, and that property MUST be preserved.

## Running it

`docker compose up -d`, then `./bin/seed.sh`.
Full instructions in README.md.

A stock published node is enough. The training extract used to need temporal RANGE window frames, which are unreleased ([xtdb/xtdb#5809](https://github.com/xtdb/xtdb/issues/5809)); it now expresses the same trailing windows as a band self-join.

**Do not reintroduce window functions into `model._window_sql`.**
`tests/test_trailing_window_contract.py` asserts their absence, because needing a locally-built image is the single biggest obstacle to anyone running this.
If you see `no viable alternative at input`, something has put a frame clause back.

Container healthchecks MUST use `wget -q -O /dev/null`, never `wget --spider`.
`/healthz/alive` answers 405 to HEAD, so the spider form never passes and `depends_on: service_healthy` deadlocks.
This is an upstream bug in xtdb/xtdb's `docker/Dockerfile`; when it's fixed, this note can go.

## Tests

```bash
uv run python -m unittest discover -s tests -t .
```

Most are contract tests over generated SQL and call shapes, and need nothing running.

`tests/test_label_version_contract.py` is different: it needs a node, and it **skips silently** without one.

```bash
docker compose --profile test up -d playground
```

You MUST start the playground before claiming those tests pass — a green run with no node up has told you nothing about the bitemporal semantics.

The playground is a separate in-memory node on purpose.
Tests MUST NOT write to the demo node: its log lives on the sim clock where system-time only ever moves forward, so a single write stamped at wall-clock-now wedges the sim permanently.
A playground hands out an isolated database per connection name, which is what lets those tests write their own system-time timeline.

TDD: write the failing test first. For SQL semantics that means a node-backed test, not a string assertion — a query can generate exactly the SQL you asked for and still return a plausible wrong answer.

## Invariants that are easy to break

**Event time is `txn_ts`, never `_valid_from`.**
Both the training windows (`model.py`) and the serving queries (`registry.py`) read `txn_ts`, and they MUST stay in agreement or the batch and online features silently drift apart.
The two columns hold the same instant today, which is exactly why this breaks quietly.
Valid-time answers "when was this fact true", which stops being the event instant the moment a transaction acquires a lifecycle.

**Fraud outcomes and available status have different valid times.**
`label` is valid from the transaction event; `fraud_status` is valid from when the status became available.
Every writer MUST update both tables atomically, including false initial statuses and later reversals.
Bulk training joins `fraud_status FOR ALL VALID_TIME` on `_valid_time CONTAINS` the anchor's `txn_ts`.
Do not derive first confirmation from system history: that loses intervening reversals and makes the query harder to explain.
Keep the query-level system-time basis in force for every training table.

**The anchor is never in its own baseline.**
Every trailing aggregate in `model._window_sql` bands to `p.txn_ts < t.txn_ts`, strictly earlier, matching the serving path's `txn_ts < T`.
This was wrong until the band self-join landed: the old `RANGE ... PRECEDING AND CURRENT ROW` frame put each transaction inside the mean it was scored against, so a large fraudulent amount dragged the baseline towards itself and trained as less unusual than it served.
Reverting it costs about 0.002 AUC in the *wrong* direction — the leaked self-information flatters the offline number, which is exactly what a leak does.
Pinned by `tests/test_trailing_window_contract.py`.

**The two prior-fraud counts mean different things.**
`as_known_then` is what the decision could see at its own instant, and keeps a fraud that was later overturned, because the decision did act on it.
`with_hindsight` is current belief at the basis, so an overturned fraud drops out.
Training MUST use `as_known_then` or it leaks.

## Known traps

Things already investigated — don't rediscover them, and don't trust the artifacts they touch:


## Toolchain

- Python: `uv`. Add dependencies to `pyproject.toml`, never `pip install` into a venv by hand.
- UI: `pnpm`, in `ui/`.
- Conventional commits, with a scope where it helps (`fix(api):`, `feat(ui):`).
- Comments explain *why*. The test name and assertion are the documentation; don't restate them.
- Sentence-per-line in markdown, so diffs stay readable.
