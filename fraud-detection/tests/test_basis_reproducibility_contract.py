"""A pinned extraction must ignore later transaction, label and status corrections.

Needs the isolated playground node: docker compose --profile test up -d playground.
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

import model  # noqa: E402

UTC = timezone.utc
PLAYGROUND_PORT = 5445
EPOCH = datetime(2025, 1, 1, tzinfo=UTC)


def D(days: int) -> datetime:
    return EPOCH + timedelta(days=days)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _lit(t: datetime) -> str:
    return f"TIMESTAMP '{_iso(t)}'"


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


_write_times = {}


@contextmanager
def _learned_at(conn, system_time: datetime):
    cur = conn.cursor()
    try:
        cur.execute(f"BEGIN READ WRITE WITH (SYSTEM_TIME = {_lit(system_time)})")
        _write_times[id(cur)] = system_time
        yield cur
        cur.execute("COMMIT")
    finally:
        _write_times.pop(id(cur), None)
        cur.close()


def _txn(cur, tid: str, account: str, event: datetime, amount: float = 100.0):
    cur.execute(f"""INSERT INTO txn (_id, _valid_from, account_id, txn_ts, amount, category, country)
                    VALUES ('{tid}', {_lit(event)}, '{account}', {_lit(event)},
                            {amount}, 'retail', 'GB')""")


def _label(cur, tid: str, event: datetime, is_fraud: bool):
    cur.execute(f"""INSERT INTO label (_id, _valid_from, is_fraud)
                    VALUES ('{tid}', {_lit(event)}, {'true' if is_fraud else 'false'})""")
    cur.execute(f"""INSERT INTO fraud_status (_id, _valid_from, is_fraud)
                    VALUES ('{tid}', {_lit(_write_times[id(cur)])},
                            {'true' if is_fraud else 'false'})""")


def _account(cur, acct: str):
    cur.execute(f"""INSERT INTO account (_id, _valid_from, name, home_country, tier)
                    VALUES ('{acct}', {_lit(D(0))}, 'a', 'GB', 'std')""")


def _rows(cur, sql: str) -> list[tuple]:
    cur.execute(sql)
    return sorted(tuple(r) for r in cur.fetchall())


ACCT = "acc1"
BASIS = D(25)


@unittest.skipUnless(_playground_up(),
                     "playground node not running: docker compose --profile test up -d playground")
class BasisReproducibilityTest(unittest.TestCase):
    """Same basis, different wall-clock, more data since: same answer."""

    def setUp(self):
        self.conn = _connect()
        confirms = {4: ("t2", D(2)), 5: ("t3", D(3))}
        _account_written = False
        for i in range(1, 13):
            with _learned_at(self.conn, D(i)) as cur:
                if not _account_written:
                    _account(cur, ACCT)
                    _account_written = True
                _txn(cur, f"t{i}", ACCT, D(i))
                if i in confirms:
                    tid, event = confirms[i]
                    _label(cur, tid, event, True)

    def tearDown(self):
        self.conn.close()

    def _pcf_at(self, basis: datetime) -> list[tuple]:
        with self.conn.cursor() as cur:
            return _rows(cur, model._pcf_sql(since=D(0), resolved_before=D(13),
                                             account=ACCT, system_time=basis))

    def _window_at(self, basis: datetime) -> list[tuple]:
        with self.conn.cursor() as cur:
            return _rows(cur, model._window_sql(since=D(0), resolved_before=D(13),
                                                account=ACCT, system_time=basis))

    def test_the_basis_extract_counts_what_had_settled_by_then(self):
        # guards the two tests below: if this is empty they pass vacuously
        rows = self._pcf_at(BASIS)
        self.assertTrue(rows, "no anchors came back at the basis")
        self.assertTrue(any(r[1] for r in rows),
                        "no as_known_then fraud at the basis, so reproducibility is untested")

    def test_a_confirmation_after_the_basis_does_not_change_the_basis_extract(self):
        before = self._pcf_at(BASIS)
        with _learned_at(self.conn, D(30)) as cur:
            _label(cur, "t5", D(5), True)
            _label(cur, "t6", D(6), True)
        self.assertEqual(before, self._pcf_at(BASIS))
        # and the new knowledge really is visible without a basis, so the assertion above
        # is about the basis rather than about the writes having failed
        self.assertNotEqual(before, self._pcf_at(D(35)))

    def test_a_label_rewritten_after_the_basis_does_not_change_the_basis_extract(self):
        before = self._pcf_at(BASIS)
        with _learned_at(self.conn, D(31)) as cur:
            _label(cur, "t2", D(2), False)   # overturned, long after the basis
        self.assertEqual(before, self._pcf_at(BASIS))

    def test_a_first_confirmation_after_the_basis_is_excluded_for_a_future_dated_anchor(self):
        with _learned_at(self.conn, D(20)) as cur:
            _txn(cur, "late1", ACCT, D(45))        # recorded before its event time
        with self.conn.cursor() as cur:
            before = _rows(cur, model._pcf_sql(since=D(0), resolved_before=D(60),
                                               account=ACCT, system_time=BASIS))
        with _learned_at(self.conn, D(30)) as cur:
            _label(cur, "t7", D(7), True)          # first confirmed after the basis
        with self.conn.cursor() as cur:
            after = _rows(cur, model._pcf_sql(since=D(0), resolved_before=D(60),
                                              account=ACCT, system_time=BASIS))
        self.assertEqual(before, after)

    def test_a_retroactive_status_correction_does_not_change_the_basis_extract(self):
        before = self._pcf_at(BASIS)
        with _learned_at(self.conn, D(31)) as cur:
            cur.execute(f"""INSERT INTO fraud_status (_id, _valid_from, _valid_to, is_fraud)
                            VALUES ('t2', {_lit(D(4))}, {_lit(D(8))}, false)""")
        self.assertEqual(before, self._pcf_at(BASIS))
        self.assertNotEqual(before, self._pcf_at(D(35)))

    def test_a_transaction_restated_after_the_basis_does_not_change_the_basis_extract(self):
        before = self._window_at(BASIS)
        self.assertTrue(before, "no window rows came back at the basis")
        with _learned_at(self.conn, D(32)) as cur:
            _txn(cur, "t4", ACCT, D(4), amount=99999.0)   # corrected amount
        self.assertEqual(before, self._window_at(BASIS))


if __name__ == "__main__":
    unittest.main()
