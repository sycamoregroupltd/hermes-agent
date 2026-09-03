import contextlib
import importlib.util
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "profiles/trading-devops/scripts/cron_necromancer_sweep.py"
spec = importlib.util.spec_from_file_location("cron_necromancer_sweep", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExpectedGuardBundleAlertTests(unittest.TestCase):
    def setUp(self):
        self.job = {"name": "guard-bundle-tick-hourly"}
        self.receipt = 'Script exited with code 1\nstderr:\n{"status":"ok","card":"t_12345678","board":"jarvis-os"}'

    def test_report_to_board_success_is_expected_alert(self):
        self.assertTrue(module.is_expected_guard_bundle_alert(self.job, self.receipt))

    def test_all_guard_bundle_cadences_are_supported(self):
        for cadence in ("5m", "15m", "hourly", "daily"):
            job = {"name": f"guard-bundle-tick-{cadence}"}
            self.assertTrue(module.is_expected_guard_bundle_alert(job, self.receipt))

    def test_non_bundle_job_is_not_suppressed(self):
        self.assertFalse(module.is_expected_guard_bundle_alert({"name": "other-job"}, self.receipt))

    def test_non_json_receipt_is_not_suppressed(self):
        error = "Script exited with code 1\nstderr:\nnot-json"
        self.assertFalse(module.is_expected_guard_bundle_alert(self.job, error))

    def test_nonzero_delivery_status_is_not_suppressed(self):
        error = 'Script exited with code 1\nstderr:\n{"status":"error","card":"t_12345678","board":"jarvis-os"}'
        self.assertFalse(module.is_expected_guard_bundle_alert(self.job, error))

    def test_missing_receipt_fields_are_not_suppressed(self):
        error = 'Script exited with code 1\nstderr:\n{"status":"ok","board":"jarvis-os"}'
        self.assertFalse(module.is_expected_guard_bundle_alert(self.job, error))

    def test_other_exit_code_is_not_suppressed(self):
        error = 'Script exited with code 2\nstderr:\n{"status":"ok","card":"t_12345678","board":"jarvis-os"}'
        self.assertFalse(module.is_expected_guard_bundle_alert(self.job, error))

    def test_main_scan_suppresses_expected_receipt_but_keeps_other_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_root = pathlib.Path(tmp) / "profiles" / "trading-devops"
            cron_root = profile_root / "cron"
            cron_root.mkdir(parents=True)
            jobs = {"jobs": [
                {"id": "guard-1", "name": "guard-bundle-tick-hourly", "enabled": True},
                {"id": "other-1", "name": "other-check", "enabled": True},
            ]}
            (cron_root / "jobs.json").write_text(json.dumps(jobs))
            con = sqlite3.connect(cron_root / "executions.db")
            con.execute("CREATE TABLE executions (job_id TEXT, status TEXT, error TEXT, claimed_at TEXT)")
            for job_id, error in (("guard-1", self.receipt), ("other-1", self.receipt.replace("guard", "other"))):
                for index in range(3):
                    con.execute("INSERT INTO executions VALUES (?, ?, ?, ?)",
                                (job_id, "error", error, f"2026-09-03T00:0{index}:00Z"))
            con.commit()
            con.close()

            output = io.StringIO()
            with mock.patch.object(module, "PROFILES", str(pathlib.Path(tmp) / "profiles")), \
                 mock.patch.dict(module.os.environ, {"NECRO_DRY_RUN": "1"}, clear=False), \
                 contextlib.redirect_stdout(output):
                module.main()

            digest = output.getvalue()
            self.assertIn("other-check", digest)
            self.assertNotIn("guard-bundle-tick-hourly", digest)
            self.assertIn("[ERROR-STREAK]", digest)


if __name__ == "__main__":
    unittest.main()
