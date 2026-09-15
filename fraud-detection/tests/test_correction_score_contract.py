"""Failing contracts for correction score/performance boundaries.

These tests deliberately mock XTDB and the model boundary.  They specify the
small amount of work the correction path may do without changing production.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import sys

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402


UTC = timezone.utc
WHEN = datetime(2026, 7, 28, 12, tzinfo=UTC)
PENDING = [("fraud-1", WHEN.replace(hour=9)), ("fraud-2", WHEN.replace(hour=10))]


def _connection_with_cursor():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cursor = MagicMock()
    cursor.fetchone.return_value = (WHEN.replace(day=27),)
    conn.cursor.return_value = cursor
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    return conn, cursor


def _request():
    return api.ConfirmReq(account_id="acct-1", later_ts=WHEN.isoformat(), amount=20.0,
                          country="GB", model_version="model-1")


class AuditConfirmScoreContractTest(unittest.TestCase):
    def test_confirm_reads_all_three_vectors_from_the_store(self):
        """before, after and reproduced are three real reads, not one read plus
        arithmetic. The demo's whole claim is that the store reproduces a past
        decision, so synthesising the reproduced score from the pre-correction
        vector would be asserting something we never asked the database."""
        conn, cursor = _connection_with_cursor()
        cursor.fetchall.return_value = PENDING
        pipe = object()
        meta = {"version": "model-1"}
        # what each read returns: the confirm lands between the first and second
        reads = [
            {"amount_zscore": 1.0, "txn_count_24h": 2.0, "foreign": 0.0, "prior_confirmed_fraud": 3.0},
            {"amount_zscore": 1.0, "txn_count_24h": 2.0, "foreign": 0.0, "prior_confirmed_fraud": 5.0},
            {"amount_zscore": 1.0, "txn_count_24h": 2.0, "foreign": 0.0, "prior_confirmed_fraud": 3.0},
        ]
        explain = lambda _pipe, values: {"prob": 0.25 if values["prior_confirmed_fraud"] == 3.0 else 0.75}

        with patch.object(api, "connect", return_value=conn), \
             patch.object(api.M, "load_version", return_value=(pipe, meta)), \
             patch.object(api.M, "sim_now", return_value=WHEN), \
             patch.object(api.R, "vector", side_effect=reads) as serve, \
             patch.object(api.M, "explain", side_effect=explain):
            response = api.audit_confirm(_request())

        self.assertEqual(serve.call_count, 3)
        # the third read must carry a system-time basis; the first two must not
        bases = [call.args[1].system_time for call in serve.call_args_list]
        self.assertIsNone(bases[0])
        self.assertIsNone(bases[1])
        self.assertIsNotNone(bases[2])
        assert response["before"] == 0.25
        assert response["after"] == 0.75
        assert response["reproduced"] == 0.25
        assert response["pcf_before"] == 3.0
        assert response["pcf_after"] == 5.0

    def test_confirm_writes_label_from_confirmation_time_and_removes_pending_chargebacks(self):
        conn, cursor = _connection_with_cursor()
        cursor.fetchall.return_value = PENDING
        pipe = object()
        meta = {"version": "model-1"}
        vector = {"amount_zscore": 1.0, "txn_count_24h": 2.0, "foreign": 0.0,
                  "prior_confirmed_fraud": 3.0}

        with patch.object(api, "connect", return_value=conn), \
             patch.object(api.M, "load_version", return_value=(pipe, meta)), \
             patch.object(api.M, "sim_now", return_value=WHEN), \
             patch.object(api.R, "vector", return_value=vector), \
             patch.object(api.M, "explain", return_value={"prob": 0.5}):
            api.audit_confirm(_request())

        assert cursor.executemany.call_count == 2
        statements = [call.args[0].upper() for call in cursor.executemany.call_args_list]
        assert any("INSERT INTO LABEL" in statement for statement in statements)
        label_call = next(call for call in cursor.executemany.call_args_list
                           if "INSERT INTO LABEL" in call.args[0].upper())
        self.assertEqual(list(label_call.args[1]), [(tid, WHEN) for tid, _ in PENDING])
        assert any("DELETE FROM PENDING_CHARGEBACK" in statement for statement in statements)


class AccountHistoryScoreContractTest(unittest.TestCase):
    def test_history_has_non_null_model_probability_for_every_row(self):
        conn, cursor = _connection_with_cursor()
        cursor.description = [MagicMock(name="id"), MagicMock(name="ts"), MagicMock(name="amount"),
                              MagicMock(name="country"), MagicMock(name="is_fraud"),
                              MagicMock(name="learned_at"), MagicMock(name="pending_id")]
        cursor.description[0].name = "id"
        cursor.description[1].name = "ts"
        cursor.description[2].name = "amount"
        cursor.description[3].name = "country"
        cursor.description[4].name = "is_fraud"
        cursor.description[5].name = "learned_at"
        cursor.description[6].name = "pending_id"
        cursor.fetchall.return_value = [
            ("txn-1", WHEN, 10.0, "GB", False, WHEN, None),
            ("txn-2", WHEN, 11.0, "GB", True, WHEN, None),
        ]
        pipe = object()
        scores = {"txn-1": 0.2, "txn-2": 0.9}

        with patch.object(api, "connect", return_value=conn), \
             patch.object(api.M, "load_latest", return_value=(pipe, {"version": "model-1"})), \
             patch.object(api.M, "sim_now", return_value=WHEN), \
             patch.object(api.M, "score_account", return_value=scores) as score_account:
            response = api.account_history("acct-1")

        score_account.assert_called_once()
        assert [row["p"] for row in response["rows"]] == [0.2, 0.9]
        assert all(row["p"] is not None for row in response["rows"])


class AccountHistoryKnowledgeBasisContractTest(unittest.TestCase):
    """Confirming a chargeback must move the correction panel's scores.

    `prior_confirmed_fraud_as_known_then` is frozen for historical rows — a
    confirmation recorded now is learned *after* their decision instants — so the
    correction lens has to score at current knowledge, the same basis serving uses.
    """

    def test_extract_requires_an_explicit_knowledge_basis(self):
        import inspect as _inspect
        import model

        self.assertIn("knowledge", _inspect.signature(model.extract).parameters)
        self.assertIn("knowledge", _inspect.signature(model.score_account).parameters)

    def test_account_history_scores_at_current_knowledge(self):
        import inspect as _inspect

        source = _inspect.getsource(api.account_history)
        self.assertIn('knowledge="current"', source)
