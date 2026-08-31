import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import jarvis_mechanism_liveness_collect as collector


class MarkJobRunDropConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile_cron = self.root / "jarvis" / "cron"
        self.profile_cron.mkdir(parents=True)
        collector.PROFILES = self.root
        self.job = {
            "id": "job-1",
            "name": "probe",
            "enabled": True,
            "state": "scheduled",
            "last_run_at": "2026-09-01T00:00:00+00:00",
            "last_status": "ok",
            "next_run_at": "2026-09-01T01:00:00+00:00",
            "schedule": {"kind": "interval"},
        }
        (self.profile_cron / "jobs.json").write_text(json.dumps({"jobs": [self.job]}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_sidecar_is_clean_and_visible(self):
        result = collector.load_mark_job_run_drops("jarvis")
        self.assertEqual(result["state"], "absent")
        self.assertEqual(result["count"], 0)

    def test_recorded_drop_is_consumed_and_gates_row(self):
        (self.profile_cron / "mark_job_run_drops.json").write_text(
            json.dumps({"count": 2, "last_at": "2026-09-01T00:30:00+00:00", "last_job_id": "job-2"})
        )
        exp = collector.Expected("probe", "probe", "jarvis", name="probe", max_age_minutes=90)
        row = collector.row_for_expected(exp, datetime(2026, 9, 1, 0, 31, tzinfo=timezone.utc))
        self.assertEqual(row["status"], "DEAD")
        self.assertEqual(row["mark_job_run_drops"]["count"], 2)
        self.assertIn("drops=2", row["reason"])
        self.assertIn("drops=2", row["last_error"])

    def test_malformed_sidecar_is_visible_failure(self):
        (self.profile_cron / "mark_job_run_drops.json").write_text("not-json")
        result = collector.load_mark_job_run_drops("jarvis")
        self.assertEqual(result["state"], "malformed")
        self.assertIsNone(result["count"])


if __name__ == "__main__":
    unittest.main()
