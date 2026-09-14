"""Static contract for the SQL expand affordance.

The demo UI has no runnable browser/component test harness yet, so this keeps the
interaction contract executable without adding a test framework or production code.
"""

from pathlib import Path
import unittest


SQL_SOURCE = (Path(__file__).parents[1] / "ui/src/components/Sql.tsx").read_text()


class SqlUiContractTest(unittest.TestCase):
    def test_every_sql_block_exposes_an_accessible_expandable_dialog_contract(self):
        self.assertIn("<button", SQL_SOURCE)
        self.assertRegex(SQL_SOURCE, r"aria-label=.{0,80}(expand|SQL)")
        self.assertIn("<dialog", SQL_SOURCE)
        self.assertIn("aria-modal=\"true\"", SQL_SOURCE)
        self.assertIn("showModal", SQL_SOURCE)
        self.assertRegex(SQL_SOURCE, r"max-w-(?:[a-z0-9-]+).*max-h-(?:[a-z0-9-]+|\[[^\]]+\])")
        self.assertIn("overflow-auto", SQL_SOURCE)

    def test_sql_dialog_can_be_dismissed_by_close_backdrop_and_escape(self):
        self.assertRegex(SQL_SOURCE, r"<button[^>]+aria-label=.{0,80}(close|Close)")
        self.assertRegex(SQL_SOURCE, r"onClick=\{[^}]*close\(")
        self.assertRegex(SQL_SOURCE, r"onClick=\{[^}]*target === (?:e\.)?currentTarget[^}]*close\(")
        self.assertRegex(SQL_SOURCE, r"key === ['\"]Escape['\"]")

    def test_training_dataset_toggle_is_named_sample_sql(self):
        train_view = (Path(__file__).parents[1] / "ui/src/views/TrainView.tsx").read_text()
        self.assertIn("sample SQL", train_view)
        self.assertNotIn("show SQL", train_view)

    def test_train_at_this_point_result_exposes_training_sql(self):
        train_view = (Path(__file__).parents[1] / "ui/src/views/TrainView.tsx").read_text()
        api_source = (Path(__file__).parents[1] / "ui/src/api.ts").read_text()
        self.assertIn("training_sql", api_source)
        self.assertRegex(train_view, r"result\.training_sql")
        self.assertRegex(train_view, r"training SQL")

    def test_open_sql_dialog_does_not_leave_background_interactive(self):
        self.assertRegex(SQL_SOURCE, r"document\.body\.style\.overflow\s*=\s*['\"]hidden['\"]")
        self.assertIn("finally", SQL_SOURCE)


if __name__ == "__main__":
    unittest.main()
