"""Backend contracts for the fraud training-panel redesign.

These intentionally describe the next backend API. They are expected to fail until
that API is implemented; no XTDB node is required because DB/model boundaries are
represented by SQL and call-shape contracts.
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from typing import get_type_hints
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
import model  # noqa: E402
import registry  # noqa: E402
import sim  # noqa: E402


UTC = timezone.utc
BASIS = datetime(2026, 7, 27, 12, 34, 56, 123456, tzinfo=UTC)


class TrainingPanelBackendContractTest(unittest.TestCase):
    def test_selected_transaction_exposes_exact_system_from_as_knowledge_basis(self):
        parameters = inspect.signature(api.training).parameters
        self.assertIn("txn_id", parameters)
        self.assertIn("system_time", parameters)
        self.assertIn("knowledge_basis", get_type_hints(api.training)["return"].__annotations__)

    def test_training_dataset_requires_one_system_time_for_both_feature_reads(self):
        parameters = inspect.signature(model.extract).parameters
        self.assertIn("system_time", parameters)
        sql = model._extract_sql(system_time=BASIS)
        self.assertGreaterEqual(sql.count("SETTING DEFAULT SYSTEM_TIME AS OF"), 2)

    def test_training_uses_selected_row_only_for_exact_knowledge_basis_and_reports_dataset_totals(self):
        parameters = inspect.signature(api.training).parameters
        self.assertIn("system_time", parameters)
        self.assertNotIn("valid_time", parameters)

        response_fields = get_type_hints(api.TrainingDataset)
        self.assertIn("total_rows", response_fields)
        self.assertIn("total_fraud", response_fields)
        self.assertIn("displayed_rows", response_fields)
        self.assertNotIn("account_id", response_fields)

        source = inspect.getsource(api.training)
        sampling = source[source.index("M.sample_training("):source.index("\n    rows =", source.index("M.sample_training("))]
        self.assertIn("system_time=knowledge_basis", sampling)
        self.assertNotIn("account=selected", sampling)

    def test_training_dataset_sample_rows_include_account_id(self):
        source = inspect.getsource(api.training)
        rows = source[source.index("rows ="):source.index("\n    differing =", source.index("rows ="))]
        self.assertIn('"account_id":', rows)

    def test_training_sample_query_bounds_before_feature_extraction(self):
        self.assertTrue(hasattr(model, "sample_training"))
        self.assertTrue(hasattr(model, "_sample_sql"))
        parameters = inspect.signature(model.sample_training).parameters
        self.assertIn("limit", parameters)
        self.assertIn("system_time", parameters)
        sql = model._sample_sql(limit=7, resolved_before=BASIS, system_time=BASIS)
        self.assertIn("ORDER BY", sql)
        self.assertIn("txn_ts DESC", sql)
        self.assertIn("LIMIT 7", sql)

    def test_training_endpoint_uses_bounded_sample_and_keeps_totals_unbounded(self):
        class Cursor:
            def __init__(self):
                self.sql = ""

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql):
                self.sql = sql

            def fetchone(self):
                if "SELECT account_id" in self.sql:
                    return ("acct-1",)
                return (1234, 56)

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return Cursor()

        sample = pd.DataFrame([{
            "id": "txn-1", "txn_ts": BASIS, "account_id": "acct-1",
            "amount": 10.0, "country": "GB", "label": 0,
            "prior_confirmed_fraud_as_known_then": 0,
            "prior_confirmed_fraud_with_hindsight": 0,
        }])
        with patch.object(api, "connect", return_value=Connection()), \
             patch.object(api.M, "extract", side_effect=AssertionError("full extraction")), \
             patch.object(api.M, "sample_training", return_value=sample, create=True) as sample_training:
            result = api.training("txn-1", BASIS.isoformat(), limit=1)

        sample_training.assert_called_once_with(
            limit=1, resolved_before=BASIS - model.training_outcome_horizon(),
            system_time=BASIS,
        )
        self.assertEqual(result["total_rows"], 1234)
        self.assertEqual(result["total_fraud"], 56)
        self.assertEqual(result["displayed_rows"], 1)

    def test_get_training_dataset_discloses_sample_sql_explicitly(self):
        response_fields = get_type_hints(api.TrainingDataset)
        self.assertIn("sample_sql", response_fields)
        self.assertNotIn("sql", response_fields)
        source = inspect.getsource(api.training)
        self.assertIn('"sample_sql":', source)

    def test_historical_preview_discloses_full_two_part_training_sql(self):
        training_sql = """-- feature window pass\nSETTING DEFAULT SYSTEM_TIME AS OF TIMESTAMP '2026-07-27T12:34:56.123456Z'\nSELECT * FROM txn\n\n-- known-time label pass\nSETTING DEFAULT SYSTEM_TIME AS OF TIMESTAMP '2026-07-27T12:34:56.123456Z'\nSELECT * FROM label"""
        with patch.object(api, "connect"), \
             patch.object(api.M, "train_window", return_value={"n": 2, "auc": 0.5, "extract_ms": 1, "fit_ms": 2}), \
             patch.object(api.M, "_extract_sql", return_value=training_sql):
            result = api.train_preview(BASIS.isoformat())

        self.assertEqual(result["training_sql"], training_sql)
        self.assertGreaterEqual(result["training_sql"].count("SYSTEM_TIME"), 2)
        self.assertIn("feature window pass", result["training_sql"])
        self.assertIn("known-time label pass", result["training_sql"])

    def test_historical_preview_is_explicit_non_persisting_and_reports_live_model_unchanged(self):
        self.assertTrue(hasattr(api, "HistoricalTrainingPreview"))
        self.assertTrue(hasattr(api, "train_preview"))
        parameters = inspect.signature(api.train_preview).parameters
        self.assertIn("system_time", parameters)

        response_fields = get_type_hints(api.HistoricalTrainingPreview)
        self.assertIn("row_count", response_fields)
        self.assertIn("auc", response_fields)
        self.assertIn("elapsed_ms", response_fields)
        self.assertIn("live_model_unchanged", response_fields)

        source = inspect.getsource(api.train_preview)
        self.assertIn("system_time", source)
        self.assertIn("persist=False", source)
        self.assertIn('"live_model_unchanged"', source)
        self.assertNotIn("persist=True", source)

    def test_training_rows_compare_as_known_then_with_hindsight(self):
        sql = model._extract_sql(system_time=BASIS)
        self.assertIn("prior_confirmed_fraud_as_known_then", sql)
        self.assertIn("prior_confirmed_fraud_with_hindsight", sql)

    def test_past_basis_training_cannot_persist(self):
        parameters = inspect.signature(model.train_window).parameters
        self.assertIn("system_time", parameters)
        with self.assertRaises(ValueError):
            model.train_window(object(), system_time=BASIS, persist=True)

    def test_settled_outcome_horizon_matches_sim_maximum_chargeback_delay(self):
        horizon = model.training_outcome_horizon()
        self.assertEqual(horizon, timedelta(days=sim.DELAY_MAX_DAYS))
        self.assertEqual(horizon, sim.CHARGEBACK_DELAY)

    def test_timestamp_literals_preserve_microseconds(self):
        self.assertEqual(
            registry.ts_lit(BASIS),
            "TIMESTAMP '2026-07-27T12:34:56.123456Z'",
        )

    def test_training_foreign_feature_reads_account_at_each_transaction_event_time(self):
        sql = model._window_sql()
        self.assertIn("JOIN account FOR ALL VALID_TIME AS a", sql)
        self.assertIn("a._valid_time CONTAINS t.txn_ts", sql)
        self.assertNotIn("JOIN account a ON", sql)

    def test_model_can_be_loaded_by_version(self):
        self.assertTrue(hasattr(model, "load_version"))
        source = inspect.getsource(model.load_version)
        self.assertIn("WHERE _id =", source)

    def test_correction_preview_names_the_model_used_for_its_score(self):
        source = inspect.getsource(api.subject_impact)
        self.assertIn('"model_version": meta["version"]', source)

    def test_correction_replay_pins_the_previewed_model_version(self):
        request_fields = get_type_hints(api.ConfirmReq)
        self.assertIn("model_version", request_fields)
        source = inspect.getsource(api.audit_confirm)
        self.assertIn("M.load_version(c, req.model_version)", source)
        self.assertNotIn("M.load_latest(c)", source)
        response_fields = get_type_hints(api.ConfirmResponse)
        self.assertIn("model_version", response_fields)


if __name__ == "__main__":
    unittest.main()
