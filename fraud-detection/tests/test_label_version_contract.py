"""Event-time joins and fraud-status timelines, exercised on isolated playground databases.

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

import model  # noqa: E402

UTC = timezone.utc
BASIS = datetime(2026, 7, 27, 12, 34, 56, 123456, tzinfo=UTC)

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
    """A write stamped with the instant the database *learns* these facts."""
    cur = conn.cursor()
    try:
        cur.execute(f"BEGIN READ WRITE WITH (SYSTEM_TIME = {_lit(system_time)})")
        _write_times[id(cur)] = system_time
        yield cur
        cur.execute("COMMIT")
    finally:
        _write_times.pop(id(cur), None)
        cur.close()


def _txn(cur, tid: str, account: str, event: datetime, country: str = "GB",
         amount: float = 100.0):
    cur.execute(f"""INSERT INTO txn (_id, _valid_from, account_id, txn_ts, amount, category, country)
                    VALUES ('{tid}', {_lit(event)}, '{account}', {_lit(event)},
                            {amount}, 'retail', '{country}')""")


def _label(cur, tid: str, is_fraud: bool, *, available: datetime | None = None):
    start = available if available is not None else _write_times[id(cur)]
    cur.execute(f"""INSERT INTO label (_id, _valid_from, is_fraud)
                    VALUES ('{tid}', {_lit(start)}, {'true' if is_fraud else 'false'})""")


def _rows_by_id(cur, sql: str) -> dict[str, dict]:
    cur.execute(sql)
    cols = [d.name for d in cur.description]
    return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


class TrainingSqlShapeContractTest(unittest.TestCase):
    """Static shape contracts — the mistakes, spelled out so they can't come back."""

    def test_event_time_reads_the_explicit_txn_ts_column(self):
        sql = model._window_sql()
        self.assertIn("t.txn_ts", sql)
        self.assertNotIn("t._valid_from", sql)

    def test_account_version_join_uses_period_containment(self):
        sql = model._window_sql()
        self.assertIn("a._valid_time CONTAINS t.txn_ts", sql)
        self.assertNotIn("a._valid_to", sql)

    def test_anchor_filters_are_event_time_not_valid_time(self):
        sql = model._pcf_sql(resolved_before=BASIS, since=BASIS)
        self.assertIn("r.txn_ts <=", sql)
        self.assertIn("r.txn_ts >=", sql)
        self.assertNotIn("r._valid_from", sql)

    def test_training_uses_label_valid_time_without_scanning_system_history(self):
        for name, sql in (("pcf", model._pcf_sql()), ("sample", model._sample_sql(10, BASIS))):
            with self.subTest(query=name):
                self.assertIn("label FOR ALL VALID_TIME", sql)
                self.assertIn("._valid_time CONTAINS", sql)
                self.assertNotIn("fraud_status", sql)
                self.assertNotIn("FOR ALL SYSTEM_TIME", sql)
                self.assertNotIn("MIN(_system_from)", sql)

    def test_training_label_history_respects_the_default_knowledge_basis(self):
        for sql in (model._pcf_sql(system_time=BASIS), model._sample_sql(10, BASIS, BASIS)):
            self.assertIn(f"SETTING DEFAULT SYSTEM_TIME AS OF {_lit(BASIS)}", sql)
            self.assertNotIn("FOR ALL SYSTEM_TIME", sql)


@unittest.skipUnless(_playground_up(),
                     f"needs a playground node on :{PLAYGROUND_PORT} "
                     f"(./gradlew :run --args=\"playground --port {PLAYGROUND_PORT}\")")
class PriorConfirmedFraudNodeTest(unittest.TestCase):
    """The bitemporal count, against a real node with a hand-written label history."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _connect()
        # One account. Three earlier frauds with deliberately awkward label histories,
        # and one anchor at D20 whose 90d lookback covers all three.
        with _learned_at(cls.conn, D(5)) as cur:
            _txn(cur, "reconfirmed", "a1", D(5))
            _label(cur, "reconfirmed", False)
        with _learned_at(cls.conn, D(6)) as cur:
            _txn(cur, "reversed", "a1", D(6))
            _label(cur, "reversed", False)
        with _learned_at(cls.conn, D(7)) as cur:
            _txn(cur, "late", "a1", D(7))
            _label(cur, "late", False)
        with _learned_at(cls.conn, D(10)) as cur:
            _label(cur, "reconfirmed", True)
        with _learned_at(cls.conn, D(11)) as cur:
            _label(cur, "reversed", True)
        with _learned_at(cls.conn, D(20)) as cur:
            _txn(cur, "anchor", "a1", D(20))
            _label(cur, "anchor", False)
        # the histories that break 'the current version carries the first confirmation'
        with _learned_at(cls.conn, D(40)) as cur:
            _label(cur, "reconfirmed", True)     # rewritten, still fraud
        with _learned_at(cls.conn, D(41)) as cur:
            _label(cur, "reversed", False)       # chargeback overturned
        with _learned_at(cls.conn, D(50)) as cur:
            _txn(cur, "after-reversal", "a1", D(50))
            _label(cur, "after-reversal", False)
        with _learned_at(cls.conn, D(150)) as cur:
            _label(cur, "late", True)            # confirmed after the basis

        cls.basis = D(100)
        with cls.conn.cursor() as cur:
            cls.rows = _rows_by_id(cur, model._pcf_sql(system_time=cls.basis))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_as_known_then_counts_the_first_confirmation_not_the_latest_rewrite(self):
        # learned D10, rewritten D40. The anchor decides at D20: it knew.
        self.assertEqual(self.rows["anchor"]["prior_confirmed_fraud_as_known_then"], 2)

    def test_a_since_overturned_fraud_still_counted_at_the_time_we_believed_it(self):
        # 'reversed' was confirmed D11 and overturned D41; the D20 decision saw a fraud.
        known = self.rows["anchor"]["prior_confirmed_fraud_as_known_then"]
        hindsight = self.rows["anchor"]["prior_confirmed_fraud_with_hindsight"]
        self.assertEqual(known - hindsight, 1)

    def test_a_fraud_overturned_before_the_decision_does_not_count(self):
        self.assertEqual(self.rows["after-reversal"]["prior_confirmed_fraud_as_known_then"], 1)

    def test_displayed_sample_and_training_agree(self):
        with self.conn.cursor() as cur:
            sample = _rows_by_id(cur, model._sample_sql(100, D(100), self.basis))
        for tid, row in self.rows.items():
            for field in ("prior_confirmed_fraud_as_known_then", "prior_confirmed_fraud_with_hindsight"):
                self.assertEqual(sample[tid][field], row[field], (tid, field))

    def test_hindsight_reflects_belief_at_the_basis(self):
        # at D100 only 'reconfirmed' is still fraud: 'reversed' was overturned and
        # 'late' is not confirmed until D150.
        self.assertEqual(self.rows["anchor"]["prior_confirmed_fraud_with_hindsight"], 1)

    def test_confirmations_after_the_basis_are_invisible(self):
        # re-running at the same basis must give the same answer forever, so the
        # D150 confirmation cannot reach back into a D100 extraction.
        with self.conn.cursor() as cur:
            later = _rows_by_id(cur, model._pcf_sql(system_time=D(200)))
        self.assertEqual(self.rows["anchor"]["prior_confirmed_fraud_with_hindsight"], 1)
        self.assertEqual(later["anchor"]["prior_confirmed_fraud_with_hindsight"], 2)


@unittest.skipUnless(_playground_up(), "needs playground on :5445")
class ImportedLabelTimelineNodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = _connect()
        with _learned_at(cls.conn, D(100)) as cur:
            _txn(cur, "fraud", "a1", D(1))
            for start, end, fraud in ((1, 10, False), (10, 20, True),
                                      (20, 30, False), (30, None, True)):
                cur.execute(f"""INSERT INTO label (_id, _valid_from, _valid_to, is_fraud)
                                VALUES ('fraud', {_lit(D(start))}, {_lit(D(end)) if end is not None else "NULL"},
                                        {'true' if fraud else 'false'})""")
            for day in (9, 10, 19, 20, 29, 30, 91, 92):
                _txn(cur, f"anchor-{day}", "a1", D(day))
                _label(cur, f"anchor-{day}", False, available=D(day))
        with cls.conn.cursor() as cur:
            cls.rows = _rows_by_id(cur, model._pcf_sql(system_time=D(150)))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_imported_history_uses_when_status_was_available_not_when_it_was_loaded(self):
        for day, count in ((9, 0), (10, 1), (19, 1), (20, 0), (29, 0), (30, 1)):
            self.assertEqual(self.rows[f"anchor-{day}"]["prior_confirmed_fraud_as_known_then"],
                             count, f"day {day}")

    def test_status_containment_does_not_duplicate_hindsight_counts(self):
        for day in (9, 10, 19, 20, 29, 30):
            self.assertEqual(self.rows[f"anchor-{day}"]["prior_confirmed_fraud_with_hindsight"], 1)

    def test_a_confirmed_anchor_does_not_count_itself(self):
        with _connect() as conn:
            with _learned_at(conn, D(10)) as cur:
                _txn(cur, "self", "a1", D(10))
                _label(cur, "self", True)
            with conn.cursor() as cur:
                row = _rows_by_id(cur, model._pcf_sql())["self"]
            self.assertEqual(row["prior_confirmed_fraud_as_known_then"], 0)
            self.assertEqual(row["prior_confirmed_fraud_with_hindsight"], 0)

    def test_the_ninety_day_lower_boundary_is_inclusive(self):
        self.assertEqual(self.rows["anchor-91"]["prior_confirmed_fraud_as_known_then"], 1)
        self.assertEqual(self.rows["anchor-92"]["prior_confirmed_fraud_as_known_then"], 0)


@unittest.skipUnless(_playground_up(), "needs playground on :5445")
class ImportedLabelReplayNodeTest(unittest.TestCase):
    def test_late_availability_and_interval_corrections_preserve_the_original_database_basis(self):
        import registry

        with _connect() as conn:
            with _learned_at(conn, D(1)) as cur:
                cur.execute(f"INSERT INTO account (_id, _valid_from, home_country) VALUES ('a1', {_lit(D(1))}, 'GB')")
                _txn(cur, "fraud", "a1", D(1))
                _label(cur, "fraud", False)
            for day in (12, 20):
                with _learned_at(conn, D(day)) as cur:
                    _txn(cur, f"anchor-{day}", "a1", D(day))
                    _label(cur, f"anchor-{day}", False)

            def counts(basis):
                with conn.cursor() as cur:
                    return _rows_by_id(cur, model._pcf_sql(system_time=basis))

            def prior_fraud(basis):
                context = registry.Ctx("a1", D(12), 100, "GB", system_time=basis)
                with conn.cursor() as cur:
                    cur.execute(registry._with_basis(registry._prior_confirmed_fraud(context), context))
                    return cur.fetchone()[0]

            original = counts(D(35))
            self.assertEqual(prior_fraud(D(12)), 0)
            with _learned_at(conn, D(40)) as cur:
                _label(cur, "fraud", True, available=D(10))
            imported = counts(D(45))
            self.assertEqual(imported["anchor-12"]["prior_confirmed_fraud_as_known_then"], 1)
            self.assertEqual(imported["anchor-12"]["prior_confirmed_fraud_with_hindsight"], 1)
            self.assertEqual(prior_fraud(D(45)), 1)
            self.assertEqual(prior_fraud(D(12)), 0)
            self.assertEqual(counts(D(35)), original)
            with conn.cursor() as cur:
                targets = _rows_by_id(cur, model._window_sql(system_time=D(45)))
            self.assertEqual(targets["fraud"]["label"], 1)

            with _learned_at(conn, D(50)) as cur:
                cur.execute(f"""INSERT INTO label (_id, _valid_from, _valid_to, is_fraud)
                                VALUES ('fraud', {_lit(D(10))}, {_lit(D(15))}, false)""")
            corrected = counts(D(55))
            self.assertEqual(corrected["anchor-12"]["prior_confirmed_fraud_as_known_then"], 0)
            self.assertEqual(corrected["anchor-12"]["prior_confirmed_fraud_with_hindsight"], 1)
            self.assertEqual(corrected["anchor-20"]["prior_confirmed_fraud_as_known_then"], 1)
            self.assertEqual(counts(D(45)), imported)
            self.assertEqual(counts(D(35)), original)
            self.assertEqual(prior_fraud(D(12)), 0)
            self.assertEqual(prior_fraud(D(55)), 1)


@unittest.skipUnless(_playground_up(),
                     f"needs a playground node on :{PLAYGROUND_PORT} "
                     f"(./gradlew :run --args=\"playground --port {PLAYGROUND_PORT}\")")
class AccountVersionJoinNodeTest(unittest.TestCase):
    """`foreign` must read the account version in force at the transaction's own event."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _connect()
        with _learned_at(cls.conn, D(1)) as cur:
            cur.execute(f"""INSERT INTO account (_id, _valid_from, _valid_to, home_country)
                            VALUES ('a1', {_lit(D(0))}, {_lit(D(10))}, 'GB')""")
            cur.execute(f"""INSERT INTO account (_id, _valid_from, home_country)
                            VALUES ('a1', {_lit(D(10))}, 'FR')""")
            _txn(cur, "before-move", "a1", D(5), country="GB")
            _label(cur, "before-move", False)
            _txn(cur, "on-move", "a1", D(10), country="GB")
            _label(cur, "on-move", False)
            _txn(cur, "after-move", "a1", D(20), country="GB")
            _label(cur, "after-move", False)
        with cls.conn.cursor() as cur:
            cls.rows = _rows_by_id(cur, model._window_sql(system_time=D(100)))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_each_transaction_sees_the_account_as_it_was_at_its_own_event_time(self):
        self.assertEqual(self.rows["before-move"]["foreign"], 0)
        self.assertEqual(self.rows["after-move"]["foreign"], 1)

    def test_containment_is_half_open_so_a_boundary_event_matches_one_version(self):
        # a transaction landing exactly on the version boundary must not join twice
        self.assertEqual(len(self.rows), 3)
        self.assertEqual(self.rows["on-move"]["foreign"], 1)


if __name__ == "__main__":
    unittest.main()
