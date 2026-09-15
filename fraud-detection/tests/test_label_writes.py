"""Production writers put classifications on a single availability timeline."""

import io
import random
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import timedelta
from unittest.mock import patch

import api
import backfill_adbc
import model
import queries
import sim
from tests.test_label_version_contract import (
    D, _connect, _label, _learned_at, _lit, _playground_up, _rows_by_id, _txn,
)


@unittest.skipUnless(_playground_up(), "needs playground on :5445")
class LabelWriterNodeTest(unittest.TestCase):
    def test_confirmation_replay_preserves_a_fact_committed_one_microsecond_before(self):
        conn = _connect()
        with _learned_at(conn, D(1)) as cur:
            cur.execute(f"INSERT INTO account (_id, _valid_from, home_country) VALUES ('a1', {_lit(D(1))}, 'GB')")
            for tid in ("confirmed", "pending"):
                _txn(cur, tid, "a1", D(1))
                _label(cur, tid, False)
            cur.execute(f"""INSERT INTO pending_chargeback (_id, _valid_from, account_id, txn_ts, due)
                            VALUES ('pending', {_lit(D(1))}, 'a1', {_lit(D(1))}, {_lit(D(50))})""")
        with _learned_at(conn, D(20)) as cur:
            _txn(cur, "anchor", "a1", D(20))
            _label(cur, "anchor", False)
        with _learned_at(conn, D(30)) as cur:
            _label(cur, "confirmed", True)
        request = api.ConfirmReq(account_id="a1", later_ts=D(20).isoformat(), amount=100,
                                 country="GB", model_version="test")
        with patch.object(api, "connect", return_value=conn), \
             patch.object(api.M, "sim_now", return_value=D(30)), \
             patch.object(api.M, "load_version", return_value=(None, {"version": "test"})), \
             patch.object(api.M, "explain", side_effect=lambda _, v: {"prob": v["prior_confirmed_fraud"]}):
            result = api.audit_confirm(request)
        self.assertEqual([result[k] for k in ("pcf_before", "pcf_after", "pcf_reproduced")], [1, 2, 1])

    def test_sim_initial_label_and_confirmation_start_at_the_write_time(self):
        with _connect() as conn:
            stream = sim.Sim(conn, random.Random(7))
            stream.sim_now = D(1)
            transaction = dict(id="fraud", account_id="a1", ts=D(1) + timedelta(minutes=15),
                               amount=100.0, category="retail", country="GB", fraud=True)
            stream._tick_once([transaction])
            stream.sim_now = D(40)
            stream._tick_once([])
            with conn.cursor() as cur:
                cur.execute("""SELECT is_fraud, _valid_from, _system_from
                               FROM label FOR ALL VALID_TIME
                               WHERE _id = 'fraud' ORDER BY _valid_from""")
                self.assertEqual(cur.fetchall(), [(False, D(1), D(1)), (True, D(40), D(40))])
                cur.execute("SELECT is_fraud, _valid_from, _system_from FROM label WHERE _id = 'fraud'")
                self.assertEqual(cur.fetchone(), (True, D(40), D(40)))
                cur.execute("SELECT COUNT(*) FROM pending_chargeback")
                self.assertEqual(cur.fetchone()[0], 0)

    def test_retried_confirmation_uses_committed_time_and_replays_the_original_score(self):
        conn = _connect()
        dsn = conn.info.dsn
        with _learned_at(conn, D(1)) as cur:
            cur.execute(f"INSERT INTO account (_id, _valid_from, home_country) VALUES ('a1', {_lit(D(1))}, 'GB')")
            _txn(cur, "fraud", "a1", D(1))
            _label(cur, "fraud", False)
            cur.execute(f"""INSERT INTO pending_chargeback (_id, _valid_from, account_id, txn_ts, due)
                            VALUES ('fraud', {_lit(D(1))}, 'a1', {_lit(D(1))}, {_lit(D(50))})""")
        with _learned_at(conn, D(20)) as cur:
            _txn(cur, "anchor", "a1", D(20))
            _label(cur, "anchor", False)
        request = api.ConfirmReq(account_id="a1", later_ts=D(20).isoformat(), amount=100,
                                 country="GB", model_version="test")
        real_sys_tx = queries.sys_tx

        @contextmanager
        def contested_transaction(connection, timestamp):
            with real_sys_tx(connection, timestamp) as cur:
                yield cur
                if timestamp == D(30):
                    raise RuntimeError("system-time contended")

        with patch.object(queries, "sys_tx", side_effect=contested_transaction), \
             patch.object(api, "connect", return_value=conn), \
             patch.object(api.M, "sim_now", side_effect=[D(30), D(31)]), \
             patch.object(api.M, "load_version", return_value=(None, {"version": "test"})), \
             patch.object(api.M, "explain", side_effect=lambda _, v: {"prob": v["prior_confirmed_fraud"]}):
            result = api.audit_confirm(request)
        self.assertEqual([result[k] for k in ("pcf_before", "pcf_after", "pcf_reproduced")], [0, 1, 0])
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            rows = _rows_by_id(cur, model._pcf_sql())
            self.assertEqual(rows["anchor"]["prior_confirmed_fraud_as_known_then"], 0)
            self.assertEqual(rows["anchor"]["prior_confirmed_fraud_with_hindsight"], 1)
            cur.execute("SELECT _valid_from, _system_from FROM label WHERE _id = 'fraud'")
            self.assertEqual(cur.fetchone(), (D(31), D(31)))
            cur.execute("""SELECT COUNT(*) FROM label FOR ALL VALID_TIME WHERE is_fraud""")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_backfill_writes_labels_at_each_days_actual_availability(self):
        conn = _connect()
        dsn = conn.info.dsn
        with patch.object(backfill_adbc, "connect", return_value=conn), redirect_stdout(io.StringIO()):
            backfill_adbc.run(n=600, span_days=60, chunk=100)
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM label")
            self.assertEqual(cur.fetchone()[0], 600)
            cur.execute("SELECT COUNT(*) FROM label FOR ALL VALID_TIME WHERE is_fraud")
            self.assertGreater(cur.fetchone()[0], 0)
            cur.execute("""SELECT COUNT(*) FROM label FOR ALL VALID_TIME
                           WHERE _valid_from <> _system_from""")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'fraud_status'")
            self.assertEqual(cur.fetchall(), [])


@unittest.skipUnless(_playground_up(), "needs playground on :5445")
class LabelHistoryCompatibilityNodeTest(unittest.TestCase):
    def test_empty_dataset_is_compatible(self):
        with _connect() as conn, conn.cursor() as cur:
            model._require_label_history(cur)

    def test_legacy_dataset_requires_a_fresh_seed(self):
        with _connect() as conn:
            with _learned_at(conn, D(1)) as cur:
                _txn(cur, "legacy", "a1", D(1))
            with conn.cursor() as cur, self.assertRaisesRegex(RuntimeError, "fresh seed"):
                model._require_label_history(cur)

    def test_partial_label_population_requires_a_fresh_seed(self):
        with _connect() as conn:
            with _learned_at(conn, D(1)) as cur:
                _txn(cur, "old", "a1", D(1))
                _txn(cur, "new", "a1", D(1))
                _label(cur, "new", False)
            with conn.cursor() as cur, self.assertRaisesRegex(RuntimeError, "fresh seed"):
                model._require_label_history(cur)

    def test_finite_label_tail_is_rejected_even_when_history_exists(self):
        with _connect() as conn:
            with _learned_at(conn, D(1)) as cur:
                _txn(cur, "old", "a1", D(1))
                cur.execute(f"""INSERT INTO label (_id, _valid_from, _valid_to, is_fraud)
                                VALUES ('old', {_lit(D(1))}, {_lit(D(2))}, false)""")
            with conn.cursor() as cur:
                with self.assertRaisesRegex(RuntimeError, "fresh seed"):
                    model._require_label_history(cur)
                model._require_label_history(cur, system_time=D(0))

    def test_guard_respects_the_basis_for_transactions_and_labels(self):
        with _connect() as conn:
            with _learned_at(conn, D(1)) as cur:
                _txn(cur, "old", "a1", D(1))
                _label(cur, "old", False)
            with _learned_at(conn, D(3)) as cur:
                _txn(cur, "uncovered", "a1", D(3))
            with conn.cursor() as cur:
                model._require_label_history(cur, system_time=D(2))
                with self.assertRaisesRegex(RuntimeError, "fresh seed"):
                    model._require_label_history(cur)

    def test_two_table_dataset_is_rejected_before_training_or_writes(self):
        for operation in ("training", "resume", "reset", "confirm", "seed"):
            with self.subTest(operation=operation):
                conn = _connect()
                with _learned_at(conn, D(1)) as cur:
                    _txn(cur, "legacy", "a1", D(1))
                    _label(cur, "legacy", False)
                    cur.execute(f"INSERT INTO fraud_status (_id, _valid_from, is_fraud) VALUES ('legacy', {_lit(D(1))}, false)")
                try:
                    with self.assertRaisesRegex(RuntimeError, "fresh seed"):
                        if operation == "training":
                            with conn.cursor() as cur:
                                model._require_label_history(cur)
                        elif operation == "resume":
                            sim.Sim(conn, random.Random(7)).resume()
                        elif operation == "reset":
                            sim.Sim(conn, random.Random(7)).reset()
                        elif operation == "confirm":
                            req = api.ConfirmReq(account_id="a1", later_ts=D(20).isoformat(), amount=100,
                                                 country="GB", model_version="test")
                            with patch.object(api, "connect", return_value=conn):
                                api.audit_confirm(req)
                        else:
                            with patch.object(backfill_adbc, "connect", return_value=conn), redirect_stdout(io.StringIO()):
                                backfill_adbc.run(n=10, span_days=60, chunk=10)
                finally:
                    conn.close()

    def test_controller_rejects_legacy_before_starting_a_worker(self):
        import sim_control

        conn = _connect()
        with _learned_at(conn, D(1)) as cur:
            _txn(cur, "legacy", "a1", D(1))
            _label(cur, "legacy", False)
            cur.execute(f"INSERT INTO fraud_status (_id, _valid_from, is_fraud) VALUES ('legacy', {_lit(D(1))}, false)")
        controller = sim_control.SimController()
        with patch.object(sim_control, "connect", return_value=conn), \
             patch.object(sim_control.threading, "Thread") as thread:
            with self.assertRaisesRegex(RuntimeError, "fresh seed"):
                controller.start()
            thread.assert_not_called()
        self.assertFalse(controller.running)
        conn.close()
