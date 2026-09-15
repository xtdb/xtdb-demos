"""Executable contract for what counts as a *correction* on the activity feed.

A correction is a transaction we first recorded as legit and later recorded as fraud —
a chargeback, recorded when the classification becomes available. Two things are easy to get wrong
and both are pinned here: a transaction booked as fraud from the outset is not a
correction, and the instant reported is when we *first* confirmed the fraud.

This started as a correlated `EXISTS` over every label version, which re-scanned all
versions for each candidate and cost ~28s on the default seed. The grouped form costs
~0.3s. The point of this test is that the rewrite kept the meaning.

Needs a playground node, and skips silently without one:

    docker compose --profile test up -d playground
"""

from __future__ import annotations

import socket
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402

UTC = timezone.utc
EPOCH = datetime(2025, 1, 1, tzinfo=UTC)
PLAYGROUND_PORT = 5445


def D(days: int) -> datetime:
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


@contextmanager
def _learned_at(conn, system_time: datetime):
    """A write stamped with the instant the database learns these facts."""
    cur = conn.cursor()
    try:
        cur.execute(f"BEGIN READ WRITE WITH (SYSTEM_TIME = {_lit(system_time)})")
        yield cur
        cur.execute("COMMIT")
    finally:
        cur.close()


@unittest.skipUnless(_playground_up(), f"no playground on :{PLAYGROUND_PORT}")
class CorrectionsFeedTest(unittest.TestCase):
    """Three transactions, three different label histories."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _connect()
        # day 1: three transactions arrive. Two look legit, one is flagged immediately.
        with _learned_at(cls.conn, D(1)) as cur:
            cur.execute("INSERT INTO account (_id, _valid_from, name, home_country, tier) "
                        f"VALUES ('acct', {_lit(D(0))}, 'acct', 'GB', 'standard')")
            for tid in ("charged-back", "stays-legit", "fraud-on-arrival"):
                cur.execute(
                    "INSERT INTO txn (_id, _valid_from, account_id, txn_ts, amount, category, country) "
                    f"VALUES ('{tid}', {_lit(D(1))}, 'acct', {_lit(D(1))}, 10.0, 'retail', 'GB')")
            cur.execute(f"INSERT INTO label (_id, _valid_from, is_fraud) VALUES ('charged-back', {_lit(D(1))}, false)")
            cur.execute(f"INSERT INTO label (_id, _valid_from, is_fraud) VALUES ('stays-legit', {_lit(D(1))}, false)")
            cur.execute(f"INSERT INTO label (_id, _valid_from, is_fraud) VALUES ('fraud-on-arrival', {_lit(D(1))}, true)")

        # day 30: the classification becomes available, while txn_ts retains the event date.
        with _learned_at(cls.conn, D(30)) as cur:
            cur.execute(f"INSERT INTO label (_id, _valid_from, is_fraud) VALUES ('charged-back', {_lit(D(30))}, true)")

        with _learned_at(cls.conn, D(40)) as cur:
            cur.execute(f"INSERT INTO label (_id, _valid_from, is_fraud) VALUES ('charged-back', {_lit(D(40))}, false)")
        with _learned_at(cls.conn, D(50)) as cur:
            cur.execute(f"INSERT INTO label (_id, _valid_from, is_fraud) VALUES ('charged-back', {_lit(D(50))}, true)")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _rows(self):
        cur = self.conn.cursor()
        cur.execute(api.corrections_sql(40))
        rows = cur.fetchall()
        cur.close()
        return {r[0]: r for r in rows}

    def test_a_label_that_flipped_to_fraud_is_a_correction(self):
        self.assertIn("charged-back", self._rows())

    def test_a_transaction_still_believed_legit_is_not(self):
        self.assertNotIn("stays-legit", self._rows())

    def test_fraud_recorded_from_the_outset_is_not_a_correction(self):
        """Nothing was corrected — we never believed otherwise."""
        self.assertNotIn("fraud-on-arrival", self._rows())

    def test_it_reports_when_we_learned_it_not_when_it_happened(self):
        _id, _acct, _amt, _ctry, event_at, confirmed_at = self._rows()["charged-back"]
        self.assertEqual(event_at.replace(tzinfo=UTC), D(1))
        self.assertEqual(confirmed_at.replace(tzinfo=UTC), D(30))


if __name__ == "__main__":
    unittest.main()
