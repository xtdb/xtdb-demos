"""Executable contract for the trailing event-time features in the training extract.

Two things are pinned here.

**The values.** A hand-computed fixture, so the extraction can be reformulated without
anyone having to trust that a rewrite preserved the semantics. The frames are inclusive
at both ends (`[t-w, t]`), the counts subtract the anchor to make them strictly-prior,
and `mean_30d` / `std_30d` deliberately *include* the anchor — that last one is the
known `amount_zscore` train/serve skew, pinned as-is so a reformulation can't quietly
change behaviour while claiming to be mechanical. Fixing it is a separate change with
its own test.

**The portability.** The extraction must not use window functions. Aggregate window
frames (`AVG(...) OVER (... RANGE BETWEEN INTERVAL ...)`) exist only on an unreleased
XTDB branch, and requiring a locally-built image is the single biggest obstacle to
anyone running this demo. The same features expressed as a band self-join run on a
stock published image.

Needs a playground node, and skips silently without one:

    docker compose --profile test up -d playground
"""

from __future__ import annotations

import socket
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import model  # noqa: E402

UTC = timezone.utc
EPOCH = datetime(2025, 1, 1, tzinfo=UTC)
PLAYGROUND_PORT = 5445


def D(days: float) -> datetime:
    return EPOCH + timedelta(days=days)


def _lit(t: datetime) -> str:
    return "TIMESTAMP '" + t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ") + "'"


def _playground_up() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("localhost", PLAYGROUND_PORT)) == 0


def _connect():
    import psycopg
    from psycopg.types.string import StrDumper

    conn = psycopg.connect(
        f"postgresql://xtdb@localhost:{PLAYGROUND_PORT}/t{uuid4().hex[:12]}",
        autocommit=True)
    conn.adapters.register_dumper(str, StrDumper)
    return conn


# (id, account, day, amount). Two accounts so PARTITION-equivalence is exercised:
# b0 sits inside a0's windows in event time and must never contribute to a0's figures.
FIXTURE = [
    ("a0", "acct-a", 0,  10.0),
    ("a1", "acct-a", 1,  20.0),
    ("a2", "acct-a", 2,  30.0),
    ("a3", "acct-a", 10, 40.0),
    ("a4", "acct-a", 40, 50.0),
    ("b0", "acct-b", 1,  99.0),
]

# Hand-computed, and every figure is over the anchor's STRICTLY EARLIER transactions.
# The trailing edge is inclusive, so the day-0 txn is in the day-1 txn's 24h window and
# the day-10 txn is in the day-40 txn's 30d window; the anchor itself never is.
#
# `mean_30d` is the one that used to differ. Under the old `RANGE ... PRECEDING AND
# CURRENT ROW` frame the anchor sat inside its own baseline, so a large amount dragged
# the mean towards itself and scored as less unusual in training than in serving. The
# serving query has always been `txn_ts < :t`; these are the values that agree with it.
# `None` where an anchor has no priors at all: there is no baseline to compare against,
# and registry.zscore returns 0.0 for that case.
EXPECTED = {
    "a0": dict(txn_count_24h=0, n_30d=0, mean_30d=None),
    "a1": dict(txn_count_24h=1, n_30d=1, mean_30d=10.0),
    "a2": dict(txn_count_24h=1, n_30d=2, mean_30d=15.0),
    "a3": dict(txn_count_24h=0, n_30d=3, mean_30d=20.0),
    "a4": dict(txn_count_24h=0, n_30d=1, mean_30d=40.0),
    "b0": dict(txn_count_24h=0, n_30d=0, mean_30d=None),
}


class ExtractionPortabilityTest(unittest.TestCase):
    """The extraction must run on a stock published XTDB image."""

    def test_extraction_uses_no_window_functions(self):
        sql = model._window_sql()
        self.assertNotIn("OVER (", sql.replace("OVER(", "OVER ("))


@unittest.skipUnless(_playground_up(), f"no playground on :{PLAYGROUND_PORT}")
class TrailingWindowValueTest(unittest.TestCase):
    """The features themselves, against a node."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _connect()
        cur = cls.conn.cursor()
        for acct in ("acct-a", "acct-b"):
            cur.execute(
                "INSERT INTO account (_id, _valid_from, name, home_country, tier) "
                f"VALUES ('{acct}', {_lit(D(-1))}, '{acct}', 'GB', 'standard')")
        for tid, acct, day, amount in FIXTURE:
            cur.execute(
                "INSERT INTO txn (_id, _valid_from, account_id, txn_ts, amount, category, country) "
                f"VALUES ('{tid}', {_lit(D(day))}, '{acct}', {_lit(D(day))}, "
                f"{amount}, 'retail', 'GB')")
            cur.execute("INSERT INTO label (_id, _valid_from, is_fraud) "
                        f"VALUES ('{tid}', {_lit(D(day))}, false)")
        cur.close()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _rows(self) -> dict[str, dict]:
        cur = self.conn.cursor()
        cur.execute(model._window_sql())
        cols = [d.name for d in cur.description]
        rows = {r[cols.index("id")]: dict(zip(cols, r)) for r in cur.fetchall()}
        cur.close()
        return rows

    def test_every_anchor_is_returned(self):
        self.assertEqual(set(self._rows()), set(EXPECTED))

    def test_trailing_features_match_hand_computed_values(self):
        rows = self._rows()
        for tid, want in EXPECTED.items():
            with self.subTest(txn=tid):
                got = rows[tid]
                self.assertEqual(int(got["txn_count_24h"]), want["txn_count_24h"])
                self.assertEqual(int(got["n_30d"]), want["n_30d"])
                if want["mean_30d"] is None:
                    self.assertIsNone(got["mean_30d"])
                else:
                    self.assertAlmostEqual(float(got["mean_30d"]), want["mean_30d"], places=6)

    def test_windows_do_not_leak_across_accounts(self):
        # acct-b's single txn sits inside acct-a's 30d windows in event time.
        self.assertEqual(int(self._rows()["a3"]["n_30d"]), 3)

    def test_the_anchor_is_excluded_from_its_own_baseline(self):
        """The train/serve skew this fixes: a large amount must not pull down its own
        z-score by joining the mean it is measured against."""
        rows = self._rows()
        # a4 is 50.0 with a single prior of 40.0 — the mean it is compared against must
        # be that prior alone, not the 45.0 midpoint of the two.
        self.assertAlmostEqual(float(rows["a4"]["mean_30d"]), 40.0, places=6)
        # and the counts must describe the same sample as the mean
        self.assertEqual(int(rows["a4"]["n_30d"]), 1)

    def test_an_anchor_with_no_priors_has_no_baseline(self):
        # not 'a baseline of itself' — registry.zscore turns this into 0.0
        self.assertIsNone(self._rows()["a0"]["mean_30d"])
        self.assertEqual(int(self._rows()["a0"]["n_30d"]), 0)


if __name__ == "__main__":
    unittest.main()
