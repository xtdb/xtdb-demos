"""Live data simulator — the streaming backend for the demo.

Runs an accelerated sim-clock (default 1 real second = 1 sim-hour), continuously
inserting transactions into XTDB with occasional fraud bursts, and landing
chargebacks on a delay. System time follows the simulated arrival time. A label
applies from when its classification became available; txn_ts retains the event time.

  python sim.py                       # run forever (the demo backend)
  python sim.py --ticks 30 --fast     # bounded, no sleep (tests/bootstrap check)

Bootstraps ~20 sim-days of history instantly on start so a model can train from
t=0, then ticks live.
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime, timedelta, timezone

from queries import connect, sys_tx, require_label_history

UTC = timezone.utc
SIM_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)
SIM_STEP = timedelta(minutes=15)     # sim-time advanced per tick
BOOTSTRAP_DAYS = 20
N_ACCOUNTS = 150
# Chargeback settlement delay is VARIABLE, not fixed: a fraud is confirmed somewhere
# between DELAY_MIN and DELAY_MAX days after it happened. Variability is deliberate —
# with a fixed delay the leak-free prior_confirmed_fraud could be faked with a "skip the
# last N days" window; a variable delay requires the classification history for each decision.
DELAY_MIN_DAYS = 2
DELAY_MAX_DAYS = 30
CHARGEBACK_DELAY = timedelta(days=DELAY_MAX_DAYS)   # upper bound: older than this ⇒ resolved
BURST_PROB = 0.012                   # per tick; scaled down ~4x from 0.05/hr since ticks are 15-min
RETRAIN_EVERY = 96                   # ticks between retrains (~1 sim-day at 4 ticks/hr)
SEED = 7

# Repeat-offender structure: a minority of accounts are fraud-prone and absorb most
# fraud. This makes prior_confirmed_fraud genuinely predictive — a confirmed past
# fraud raises the odds of the next — so a chargeback visibly moves a later score
# (the correction lens) and as-of-then vs as-of-now training diverges.
PRONE_FRAC = 0.12
FRAUD_RATE_PRONE = 0.32
FRAUD_RATE_NORMAL = 0.015

# Live-stream fraud sprinkle: mixed into the normal per-account flow (on top of the rare
# bursts) so the stream always has some fraud to watch, not just during a burst. Blended
# to ~1/20 of live transactions, biased to prone accounts to keep the repeat-offender
# signal (and so confirming one fraud still visibly moves that account's later scores).
LIVE_FRAUD_PRONE = 0.20
LIVE_FRAUD_NORMAL = 0.03

MERCHANTS = [f"m{i:02d}" for i in range(40)]
CATEGORIES = ["grocery", "fuel", "dining", "travel", "electronics", "cash"]
COUNTRIES = ["GB", "US", "FR", "DE", "IE"]
FOREIGN = ["NG", "RU", "CN", "BR"]


def make_accounts(rng):
    accts = []
    for i in range(N_ACCOUNTS):
        accts.append(dict(
            id=f"a{i:04d}",
            name=f"acct-{i:04d}",
            home=rng.choices(COUNTRIES, weights=[5, 3, 1, 1, 1])[0],
            tier=rng.choices(["standard", "premium"], weights=[4, 1])[0],
            rate=rng.uniform(0.004, 0.02),          # txns per sim-hour
            typical=rng.lognormvariate(3.2, 0.5),   # ~£25 median
            prone=rng.random() < PRONE_FRAC,        # fraud-prone (repeat offender)
        ))
    return accts


class Sim:
    def __init__(self, conn, rng):
        self.conn = conn
        self.rng = rng
        self.accts = make_accounts(rng)
        self.by_id = {a["id"]: a for a in self.accts}
        self.sim_now = SIM_EPOCH
        self.seq = 0
        self.pending = []   # (txn_id, txn_ts, due_sim_time) chargebacks in flight

    # --- schema ----------------------------------------------------------
    def reset(self):
        with self.conn.cursor() as cur:
            require_label_history(cur)
            for tbl in ("account", "txn", "label", "sim_clock", "pending_chargeback"):
                try:
                    cur.execute(f"DELETE FROM {tbl}")
                except Exception:
                    pass
        # accounts exist from the epoch; stamp system-time = epoch so the node's clock
        # starts there and the bootstrap ticks (also at sim-time) stay monotonic.
        with sys_tx(self.conn, SIM_EPOCH) as cur:
            cur.executemany(
                """INSERT INTO account (_id, _valid_from, name, home_country, tier)
                   VALUES (%s, %s, %s, %s, %s)""",
                [(a["id"], SIM_EPOCH, a["name"], a["home"], a["tier"])
                 for a in self.accts],
            )

    def resume(self):
        """Continue an existing dataset (e.g. after backfill): pick up the sim-clock,
        the id counter, and any in-flight chargebacks from the DB rather than resetting.
        Accounts are the same deterministic set (same SEED), so they need no reload."""
        with self.conn.cursor() as cur:
            require_label_history(cur)
            cur.execute("SELECT MAX(sim_now) FROM sim_clock")
            row = cur.fetchone()
            if row and row[0]:
                self.sim_now = row[0] if row[0].tzinfo else row[0].replace(tzinfo=UTC)
            cur.execute("SELECT MAX(_id) FROM txn")
            mid = cur.fetchone()[0]
            if mid:
                self.seq = int(mid[1:])          # strip leading 't'
            try:
                cur.execute("SELECT _id, txn_ts, due FROM pending_chargeback")
                rows = cur.fetchall()
            except Exception:
                rows = []          # table only exists once a fraud has been recorded
            for tid, ts, due in rows:
                ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
                due = due if due.tzinfo else due.replace(tzinfo=UTC)
                self.pending.append((tid, ts, due))
        return self.seq

    def _new_txn(self, acct, ts, *, fraud):
        self.seq += 1
        tid = f"t{self.seq:08d}"   # 8 digits: matches backfill ids so append continues cleanly
        if fraud:
            # deliberately overlapping with legit tails, and only mostly-foreign,
            # so the model isn't trivially perfect
            amount = round(acct["typical"] * self.rng.uniform(2.5, 9), 2)
            country = self.rng.choice(FOREIGN) if self.rng.random() < 0.7 else acct["home"]
            cat = self.rng.choice(["cash", "electronics", "travel"])
        else:
            amount = round(acct["typical"] * self.rng.lognormvariate(0, 0.4), 2)
            country = acct["home"] if self.rng.random() < 0.97 else self.rng.choice(COUNTRIES)
            cat = self.rng.choice(CATEGORIES)
        return dict(id=tid, account_id=acct["id"], ts=ts, amount=amount,
                    category=cat, country=country, fraud=fraud)

    def _tick_txns(self):
        """Transactions generated in the current sim-hour."""
        out = []
        for a in self.accts:
            if self.rng.random() < a["rate"]:
                mins = self.rng.randint(0, 59)
                p_fraud = LIVE_FRAUD_PRONE if a["prone"] else LIVE_FRAUD_NORMAL
                out.append(self._new_txn(a, self.sim_now + timedelta(minutes=mins),
                                         fraud=self.rng.random() < p_fraud))
        if self.rng.random() < BURST_PROB:
            prone = [a for a in self.accts if a["prone"]]
            pool = prone if (prone and self.rng.random() < 0.85) else self.accts
            victim = self.rng.choice(pool)
            for k in range(self.rng.randint(3, 7)):
                out.append(self._new_txn(victim, self.sim_now + timedelta(minutes=self.rng.randint(0, 59)),
                                         fraud=True))
        return out

    def _land_chargebacks(self, cur):
        due = [p for p in self.pending if p[2] <= self.sim_now]
        self.pending = [p for p in self.pending if p[2] > self.sim_now]
        if due:
            cur.executemany(
                "INSERT INTO label (_id, _valid_from, is_fraud) VALUES (%s, %s, true)",
                [(tid, self.sim_now) for tid, _, _ in due],
            )
            cur.executemany(
                "DELETE FROM pending_chargeback WHERE _id = %s",
                [(tid,) for tid, _, _ in due],
            )
        return len(due)

    def _tick_once(self, txns):
        # Everything the DB learns this tick is stamped system-time = sim-now.
        with sys_tx(self.conn, self.sim_now) as cur:
            if txns:
                cur.executemany(
                    """INSERT INTO txn (_id, _valid_from, account_id, txn_ts,
                                        amount, category, country)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    [(t["id"], t["ts"], t["account_id"], t["ts"], t["amount"],
                      t["category"], t["country"]) for t in txns],
                )
                # every txn starts life labelled legit; chargebacks correct later
                cur.executemany(
                    "INSERT INTO label (_id, _valid_from, is_fraud) VALUES (%s, %s, false)",
                    [(t["id"], self.sim_now) for t in txns],
                )
                frauds = []
                for t in txns:
                    if t["fraud"]:
                        # variable settlement delay: we won't *know* until it lands
                        t["due"] = t["ts"] + timedelta(days=self.rng.randint(DELAY_MIN_DAYS, DELAY_MAX_DAYS))
                        self.pending.append((t["id"], t["ts"], t["due"]))
                        frauds.append(t)
                if frauds:
                    cur.executemany(
                        """INSERT INTO pending_chargeback
                             (_id, _valid_from, account_id, txn_ts, due)
                           VALUES (%s, %s, %s, %s, %s)""",
                        [(t["id"], t["ts"], t["account_id"], t["ts"], t["due"]) for t in frauds],
                    )
            # Status becomes available now; keeping the preceding false interval lets
            # training recover what earlier decisions could use. Unsettled cases stay pending.
            landed = self._land_chargebacks(cur)
            cur.execute("INSERT INTO sim_clock (_id, _valid_from, sim_now) VALUES (%s, %s, %s)",
                        ("clock", self.sim_now, self.sim_now))
        return landed

    def tick(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT MAX(system_time) FROM xt.txs")
            latest = cur.fetchone()[0]
        # Resuming or retraining can leave the saved sim clock at an existing commit.
        # Advance before generating events so the first resumed tick is visible too.
        while latest is not None and self.sim_now <= latest:
            self.sim_now += SIM_STEP
        txns = self._tick_txns()
        # Self-heal: system-time must only move forward across the whole node. If another
        # writer (a manual confirm, a retrain) has moved the clock past ours, jump sim-now
        # ahead of it and retry rather than stalling silently.
        landed = 0
        for _ in range(8):
            try:
                landed = self._tick_once(txns)
                break
            except Exception as e:
                if "system-time" in str(e).lower():
                    self.sim_now += SIM_STEP
                    continue
                raise
        self.sim_now += SIM_STEP
        return len(txns), landed


def run(bootstrap_days=BOOTSTRAP_DAYS, ticks=None, fast=False, append=False):
    rng = random.Random(SEED)
    sim = Sim(connect(), rng)

    import model  # deferred: heavy sklearn import, and lets sim.py run standalone

    if append:
        # continue an existing dataset (backfilled history + live append)
        sim.resume()
        print(f"resuming: sim_now={sim.sim_now:%Y-%m-%d %H:%M}, seq={sim.seq}, "
              f"pending={len(sim.pending)}")
    else:
        sim.reset()
        boot_ticks = bootstrap_days * 24
        for _ in range(boot_ticks):
            sim.tick()
        print(f"bootstrapped {bootstrap_days} sim-days -> {sim.seq} txns, "
              f"sim_now={sim.sim_now:%Y-%m-%d}")
        meta = model.train(sim.conn)
        if meta:
            print(f"initial model {meta['version']}  auc={meta['auc']:.3f}  n={meta['n_train']}")

    n = 0
    while ticks is None or n < ticks:
        made, landed = sim.tick()
        n += 1
        if n % RETRAIN_EVERY == 0:
            meta = model.train(sim.conn)
            if meta:
                print(f"  retrained {meta['version']}  auc={meta['auc']:.3f}  "
                      f"n={meta['n_train']} (fraud={meta['n_fraud']})")
        if n % 24 == 0 or ticks is not None:
            print(f"tick {n}: +{made} txns, {landed} chargebacks, "
                  f"sim_now={sim.sim_now:%Y-%m-%d %H:%M}, total={sim.seq}")
        if not fast:
            time.sleep(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap-days", type=int, default=BOOTSTRAP_DAYS)
    ap.add_argument("--ticks", type=int, default=None)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--append", action="store_true",
                    help="continue an existing (e.g. backfilled) dataset instead of resetting")
    a = ap.parse_args()
    run(bootstrap_days=a.bootstrap_days, ticks=a.ticks, fast=a.fast, append=a.append)
