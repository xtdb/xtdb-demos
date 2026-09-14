"""Failing contracts for correction-demo actionable-case resilience.

These tests specify the boundary behavior without changing the demo implementation or
inventing fixture transactions: the database/cache are represented by mocked calls and
source-level contracts describe the UI's required fallback wiring.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
UI = ROOT / "ui" / "src"
sys.path.insert(0, str(ROOT))

import api  # noqa: E402


class CorrectionBackendResilienceContractTest(unittest.TestCase):
    def setUp(self):
        self.old_cache = api._impactful_cache.copy()
        self.old_computing = api._impactful_computing

    def tearDown(self):
        api._impactful_cache.clear()
        api._impactful_cache.update(self.old_cache)
        api._impactful_computing = self.old_computing

    def test_cold_impactful_response_has_a_synchronous_pending_case_fallback(self):
        """A cold cache must not turn known pending chargebacks into an empty demo."""
        api._impactful_cache.update(at=0.0, data=None)
        fallback = {"account_id": "from-db", "txn_id": "real-txn"}

        with patch.object(api, "_impactful_fallback", return_value=[fallback], create=True) as get_fallback:
            response = api.impactful(limit=12)

        get_fallback.assert_called_once()
        self.assertEqual(response["cases"], [fallback])

    def test_cold_fallback_is_cheap_and_reads_pending_accounts_not_the_ranked_cache(self):
        """The cold path must use the pending-chargeback index and remain synchronous."""
        source = inspect.getsource(api.impactful)
        self.assertIn("_impactful_fallback", source)
        self.assertTrue(hasattr(api, "_impactful_fallback"))
        if hasattr(api, "_impactful_fallback"):
            fallback_source = inspect.getsource(api._impactful_fallback)
            self.assertIn("pending_chargeback", fallback_source)
        self.assertNotIn("_impactful_refresh", source)

    def test_stale_cache_retains_last_known_actionable_case_while_refreshing(self):
        retained = [{"account_id": "retained", "txn_id": "real-txn"}]
        api._impactful_cache.update(at=0.0, data=retained)
        with patch("api.time.time", return_value=100.0), patch.object(api.threading, "Thread") as thread:
            response = api.impactful(limit=12)

        self.assertEqual(response["cases"], retained)
        thread.assert_called_once()

    def test_refresh_failure_does_not_clear_last_known_cases(self):
        retained = [{"account_id": "retained", "txn_id": "real-txn"}]
        api._impactful_cache.update(at=1.0, data=retained)
        with patch.object(api, "connect", side_effect=RuntimeError("cold DB")):
            api._impactful_refresh()
        self.assertEqual(api._impactful_cache["data"], retained)


class CorrectionUiResilienceContractTest(unittest.TestCase):
    def test_data_view_fetches_a_guaranteed_subject_when_pending_account_is_not_in_fifty_rows(self):
        source = (UI / "components" / "DataView.tsx").read_text()
        self.assertIn("getBestSubject", source)
        self.assertIn("getPendingAccounts", source)
        self.assertIn("account_id", source)
        self.assertIn("rows.length", source)

    def test_correction_view_does_not_present_empty_ranked_cases_as_the_only_action(self):
        source = (UI / "views" / "AuditView.tsx").read_text()
        self.assertIn("getBestSubject", source)
        self.assertNotIn("no high-impact cases — start the sim to generate chargebacks.", source)
        self.assertIn("subject", source)


if __name__ == "__main__":
    unittest.main()
