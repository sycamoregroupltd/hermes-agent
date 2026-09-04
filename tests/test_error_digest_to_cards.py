import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts/error-digest-to-cards.py"
SPEC = importlib.util.spec_from_file_location("error_digest_to_cards", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ErrorDigestGuardReceiptTests(unittest.TestCase):
    def test_exact_positive_receipt(self):
        receipt = "Script exited with code 1\nstderr:\n" + json.dumps(
            {"status": "ok", "card": "t_abcdef12", "board": "jarvis-os"}
        )
        self.assertTrue(MODULE.expected_guard_bundle_failure("guard-bundle-tick-hourly", receipt))

    def test_required_negative_receipts(self):
        prefix = "Script exited with code 1\nstderr:\n"
        valid = {"status": "ok", "card": "t_abcdef12", "board": "jarvis-os"}
        cases = [
            ("malformed", prefix + "not-json", "guard-bundle-tick-hourly"),
            ("delivery error", prefix + json.dumps({"status": "error", "card": "t_abcdef12", "board": "jarvis-os"}), "guard-bundle-tick-hourly"),
            ("non-bundle", prefix + json.dumps(valid), "cron-worker"),
            ("unrelated exit 1", "Script exited with code 1\nstderr:\ncheck failed", "guard-bundle-tick-hourly"),
            ("bad card", prefix + json.dumps({**valid, "card": "t_ABCDEF12"}), "guard-bundle-tick-hourly"),
            ("blank board", prefix + json.dumps({**valid, "board": "  "}), "guard-bundle-tick-hourly"),
            ("extra key", prefix + json.dumps({**valid, "extra": 1}), "guard-bundle-tick-hourly"),
        ]
        for name, error, job in cases:
            with self.subTest(case=name):
                self.assertFalse(MODULE.expected_guard_bundle_failure(job, error))

    def _scan(self, rows, *, error_column=True, job_name="guard-bundle-tick-hourly"):
        with TemporaryDirectory() as td:
            root = Path(td)
            profile = root / "jarvis" / "cron"
            profile.mkdir(parents=True)
            (profile / "jobs.json").write_text(
                json.dumps({"jobs": [{"id": "job-1", "name": job_name}]})
            )
            db = profile / "executions.db"
            con = sqlite3.connect(db)
            if error_column:
                con.execute("CREATE TABLE executions (job_id TEXT, status TEXT, started_at TEXT, error TEXT)")
                for error in rows:
                    con.execute("INSERT INTO executions VALUES (?, ?, datetime('now'), ?)", ("job-1", "failed", error))
            else:
                con.execute("CREATE TABLE executions (job_id TEXT, status TEXT, started_at TEXT)")
                for _ in rows:
                    con.execute("INSERT INTO executions VALUES (?, ?, datetime('now'))", ("job-1", "failed"))
            con.commit()
            con.close()
            with patch.object(MODULE, "PROFILES_ROOT", root), patch(
                "urllib.request.urlopen", side_effect=OSError("no alertmanager in hermetic test")
            ):
                return MODULE.current_errors()

    def test_scan_suppresses_only_all_valid_receipts(self):
        receipt = "Script exited with code 1\nstderr:\n" + json.dumps(
            {"status": "ok", "card": "t_abcdef12", "board": "jarvis-os"}
        )
        self.assertNotIn("cron-jarvis-job-1", self._scan([receipt, receipt, receipt]))

    def test_scan_keeps_required_negative_cases_visible(self):
        prefix = "Script exited with code 1\nstderr:\n"
        valid = prefix + json.dumps({"status": "ok", "card": "t_abcdef12", "board": "jarvis-os"})
        cases = [
            ("malformed", [prefix + "not-json"] * 3, "guard-bundle-tick-hourly"),
            ("delivery", [prefix + json.dumps({"status": "error", "card": "t_abcdef12", "board": "jarvis-os"})] * 3, "guard-bundle-tick-hourly"),
            ("non-bundle", [valid] * 3, "cron-worker"),
            ("unrelated", ["Script exited with code 1\nstderr:\ncheck failed"] * 3, "guard-bundle-tick-hourly"),
            ("mixed", [valid, "Script exited with code 1\nstderr:\ncheck failed", valid], "guard-bundle-tick-hourly"),
        ]
        for name, rows, job_name in cases:
            with self.subTest(case=name):
                self.assertIn("cron-jarvis-job-1", self._scan(rows, job_name=job_name))

    def test_legacy_executions_without_error_column_stays_visible(self):
        self.assertIn("cron-jarvis-job-1", self._scan([None, None, None], error_column=False))


if __name__ == "__main__":
    unittest.main()
