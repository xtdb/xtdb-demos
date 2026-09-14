import unittest
import random
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import model
import sim
from queries import sys_tx
from tests.test_label_version_contract import D, _connect, _playground_up


@unittest.skipUnless(_playground_up(), "needs playground on :5445")
class ModelPersistenceNodeTest(unittest.TestCase):
    def test_retraining_with_a_stopped_sim_preserves_both_models(self):
        with _connect() as conn, TemporaryDirectory() as directory, \
             patch.object(model, "MODELS_DIR", Path(directory)):
            with sys_tx(conn, D(1)) as cur:
                cur.execute("INSERT INTO sim_clock (_id, sim_now) VALUES (%s, %s)",
                            ("clock", D(1)))

            first = model._persist(conn, {"model": "first"}, 0.98, 5000, 300)
            first_pipe, first_meta = model.load_version(conn, first)
            self.assertEqual(first_pipe, {"model": "first"})

            second = model._persist(conn, {"model": "second"}, 0.99, 5000, 300)
            second_pipe, second_meta = model.load_latest(conn)
            self.assertEqual(second_pipe, {"model": "second"})
            self.assertEqual(second_meta["version"], second)
            self.assertGreater(second_meta["trained_at_sim"], first_meta["trained_at_sim"])
            self.assertEqual(model.load_version(conn, first)[0], {"model": "first"})

    def test_sim_resumes_after_a_model_write_at_its_stationary_clock(self):
        with _connect() as conn, TemporaryDirectory() as directory, \
             patch.object(model, "MODELS_DIR", Path(directory)):
            with sys_tx(conn, D(1)) as cur:
                cur.execute("INSERT INTO sim_clock (_id, sim_now) VALUES (%s, %s)",
                            ("clock", D(1)))
            model._persist(conn, {"model": "first"}, 0.98, 5000, 300)

            stream = sim.Sim(conn, random.Random(7))
            stream.sim_now = D(1)
            transaction = dict(id="resumed", account_id="a1", ts=D(1), amount=100.0,
                               category="retail", country="GB", fraud=False)
            with patch.object(stream, "_tick_txns", return_value=[transaction]):
                self.assertEqual(stream.tick(), (1, 0))
            with conn.cursor() as cur:
                cur.execute("SELECT _id FROM txn")
                self.assertEqual(cur.fetchall(), [("resumed",)])
