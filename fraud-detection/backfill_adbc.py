"""Bitemporal backfill — replays history in learned-at (system-time) order.

    python backfill_adbc.py --n 1000000

Each day is written at its simulated system time so score replay can recover the
labels available before a later confirmation. System time must increase monotonically,
so transactions and confirmations are replayed together in arrival order.

The classification in `label` applies from the day's arrival time, allowing training
to join its valid-time intervals to each historical decision. The transaction's event
time remains in txn_ts. That label timeline can also be imported in one transaction;
the daily system-time replay is needed for the demo's score-replay history.

Requires a FRESH node (empty log): the first transaction sets the clock's start, and
nothing already committed may be newer. Use a fresh Compose project, or explicitly reset the demo volume before bin/seed.sh.
"""

from __future__ import annotations

import argparse
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from queries import connect, iso, sys_tx, require_label_history
from sim import (CATEGORIES, COUNTRIES, DELAY_MAX_DAYS, DELAY_MIN_DAYS, FOREIGN,
                 FRAUD_RATE_NORMAL, FRAUD_RATE_PRONE, SEED, make_accounts)

UTC = timezone.utc
DAY_US = 86_400 * 1_000_000


def _dt(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1_000_000, UTC)


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return f"TIMESTAMP '{iso(v)}'"
    return "'" + str(v).replace("'", "''") + "'"


def run(n: int, span_days: int, chunk: int):
    rng = np.random.default_rng(0)
    # same SEED as sim.py so the deterministic accounts match: sim appends live
    # transactions for these exact accounts without re-generating or storing them.
    accts = make_accounts(random.Random(SEED))
    acc_id = np.array([a["id"] for a in accts])
    acc_home = np.array([a["home"] for a in accts])
    acc_typ = np.array([a["typical"] for a in accts], dtype="float64")
    acc_fraud_p = np.array([FRAUD_RATE_PRONE if a["prone"] else FRAUD_RATE_NORMAL
                            for a in accts], dtype="float64")
    cats, fcats = np.array(CATEGORIES), np.array(["cash", "electronics", "travel"])
    countries, foreign = np.array(COUNTRIES), np.array(FOREIGN)

    # History ends a little in the past, not at wall-clock now: the sim advances sim-time
    # ~750x real (15 sim-min per tick), so if it started at "now" the stream would race
    # into the future and as-of-now reads would hide it. Ending ~45 days back gives the
    # live sim room to run forward (staying behind wall-clock) for the length of a demo.
    HISTORY_ENDS_DAYS_AGO = 180
    span_end = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=HISTORY_ENDS_DAYS_AGO)
    end_us = int(span_end.timestamp() * 1_000_000)
    start_us = end_us - span_days * DAY_US
    step_us = max((end_us - start_us) // n, 1)

    idx = np.arange(n)
    event_us = start_us + idx * step_us
    ai = rng.integers(0, len(accts), n)
    fraud = rng.random(n) < acc_fraud_p[ai]
    n_fraud = int(fraud.sum())
    # variable settlement delay per fraud — makes status availability matter (see sim.py)
    delay_days = rng.integers(DELAY_MIN_DAYS, DELAY_MAX_DAYS + 1, n)
    confirm_us = event_us + delay_days * DAY_US

    typ, home = acc_typ[ai], acc_home[ai]
    amount = np.where(fraud, typ * rng.uniform(2.5, 9, n),
                      typ * np.exp(rng.normal(0, 0.4, n))).round(2)
    legit_ctry = np.where(rng.random(n) < 0.03, countries[rng.integers(0, len(countries), n)], home)
    fraud_ctry = np.where(rng.random(n) < 0.7, foreign[rng.integers(0, len(foreign), n)], home)
    country = np.where(fraud, fraud_ctry, legit_ctry)
    category = np.where(fraud, fcats[rng.integers(0, 3, n)], cats[rng.integers(0, len(cats), n)])
    ids = np.array([f"t{i:08d}" for i in idx])
    account_ids = acc_id[ai]

    # Bucket every operation by the *day it is learned* (system-time).
    day = lambda u: (int(u) // DAY_US) * DAY_US
    txn_by_day: dict[int, list[int]] = defaultdict(list)     # event day  -> txn rows
    confirm_by_day: dict[int, list[int]] = defaultdict(list)  # settle day -> label=true
    pending_by_day: dict[int, list[int]] = defaultdict(list)  # event day  -> still in flight
    for i in range(n):
        txn_by_day[day(event_us[i])].append(i)
        if fraud[i]:
            if confirm_us[i] <= end_us:
                confirm_by_day[day(confirm_us[i])].append(i)
            else:
                pending_by_day[day(event_us[i])].append(i)

    def emit(cur, prefix, rows):
        # one multi-row INSERT ... VALUES per chunk (inline literals) — far faster over
        # pgwire than row-by-row executemany, and sidesteps param-type inference.
        for k in range(0, len(rows), chunk):
            vals = ",".join("(" + ",".join(_lit(v) for v in row) + ")" for row in rows[k:k + chunk])
            cur.execute(f"{prefix} VALUES {vals}")

    conn = connect()
    with conn.cursor() as cur:
        require_label_history(cur)
    t0 = time.time()
    acct_ts = _dt(day(start_us))
    with sys_tx(conn, acct_ts) as cur:
        emit(cur, "INSERT INTO account (_id, _valid_from, name, home_country, tier)",
             [(a["id"], acct_ts, a["name"], a["home"], a["tier"]) for a in accts])

    days = sorted(set(txn_by_day) | set(confirm_by_day))
    done = 0
    for d in days:
        with sys_tx(conn, _dt(d)) as cur:
            tt = txn_by_day.get(d, [])
            if tt:
                emit(cur, "INSERT INTO txn (_id, _valid_from, account_id, txn_ts, amount, category, country)",
                     [(ids[i], _dt(event_us[i]), account_ids[i], _dt(event_us[i]),
                       float(amount[i]), category[i], country[i]) for i in tt])
                # everything starts labelled legit (learned when the txn is seen)
                emit(cur, "INSERT INTO label (_id, _valid_from, is_fraud)",
                     [(ids[i], _dt(d), False) for i in tt])
            cc = confirm_by_day.get(d, [])
            if cc:
                # Classification becomes true when confirmed; its previous interval remains available.
                emit(cur, "INSERT INTO label (_id, _valid_from, is_fraud)",
                     [(ids[i], _dt(d), True) for i in cc])
            pp = pending_by_day.get(d, [])
            if pp:
                emit(cur, "INSERT INTO pending_chargeback (_id, _valid_from, account_id, txn_ts, due)",
                     [(ids[i], _dt(event_us[i]), account_ids[i], _dt(event_us[i]), _dt(int(confirm_us[i])))
                      for i in pp])
        done += len(tt)
        if done and (len(days) < 40 or d == days[-1] or days.index(d) % 20 == 0):
            print(f"  {done:,}/{n:,} txns  ({done / (time.time() - t0):,.0f} rows/s)")

    # seed the sim-clock at the history end so sim.py --append continues from here
    with sys_tx(conn, span_end) as cur:
        cur.execute("INSERT INTO sim_clock (_id, _valid_from, sim_now) VALUES (%s, %s, %s)",
                    ("clock", span_end, span_end))
    conn.close()

    n_pending = sum(len(v) for v in pending_by_day.values())
    dt = time.time() - t0
    print(f"ingested {n:,} txns ({n_fraud:,} fraud, {n_fraud / n:.1%}; {n_pending:,} still in flight) "
          f"over {len(days)} system-days, {span_end:%Y-%m-%d} back {span_days}d, in {dt:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    # NOTE: this bitemporal replay writes each day at its own system-time, which stresses
    # the temporal index far more than the old single-system-time ADBC bulk load. On the
    # dev node's ~6g heap it OOMs around ~100k rows, so keep n at 50k. (Raising it needs a
    # bigger node heap or periodic block flushing during ingest.)
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--span-days", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=1_000)   # rows per INSERT; keep well under the ~2k that OOM'd
    a = ap.parse_args()
    span = a.span_days or max(365, a.n // 2000)
    run(a.n, span, a.chunk)
