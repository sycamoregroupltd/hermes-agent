#!/usr/bin/env python3
"""Regression tests for no_black_holes_detector evidence modes.

Run:
  python3 scripts/test_no_black_holes_detector.py
  python3 -m pytest scripts/test_no_black_holes_detector.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("no_black_holes_detector.py")


class NoBlackHolesDetectorEvidenceModeTests(unittest.TestCase):
    def run_detector(self, *args: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hermes_home = Path(tmp.name) / "hermes"
        (hermes_home / "profiles").mkdir(parents=True)
        env = {
            **os.environ,
            "HERMES_HOME": str(hermes_home),
            "HERMES_REAL_HOME": str(hermes_home),
            "NO_BLACK_HOLES_BOARD": "fixture-board",
        }
        cp = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )
        return cp, hermes_home

    def test_default_human_fixture_output_remains_capped(self) -> None:
        cp, _ = self.run_detector("--fixture", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("NO-BLACK-HOLES DETECTOR: 38 finding(s) (fixture-dry-run)", cp.stdout)
        self.assertIn("## cron-output (35)", cp.stdout)
        self.assertIn("fixture:local-weekly-summary-29", cp.stdout)
        self.assertNotIn("fixture:local-weekly-summary-30", cp.stdout)
        self.assertIn("… 5 more", cp.stdout)

    def test_expanded_human_fixture_output_lists_all_findings(self) -> None:
        cp, _ = self.run_detector("--fixture", "--dry-run", "--max-findings", "-1")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("fixture:local-weekly-summary-34", cp.stdout)
        self.assertNotIn("… 5 more", cp.stdout)

    def test_json_fixture_output_is_machine_readable_and_full(self) -> None:
        cp, _ = self.run_detector("--fixture", "--dry-run", "--json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        payload = json.loads(cp.stdout)
        self.assertEqual(payload["mode"], "fixture-dry-run")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["state_written"])
        self.assertIsNone(payload["triage_card"])
        self.assertEqual(payload["counts"]["total"], 38)
        self.assertEqual(payload["counts"]["by_section"]["cron-output"], 35)
        self.assertEqual(len(payload["findings"]), 38)
        self.assertEqual(payload["findings"][34]["key"], "fixture:local-weekly-summary-34")

    def test_dry_run_fixture_does_not_create_state_or_cards(self) -> None:
        cp, hermes_home = self.run_detector("--fixture", "--dry-run", "--json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse((hermes_home / "state" / "no_black_holes_state.json").exists())
        self.assertFalse((hermes_home / "state" / "no_black_holes_allowlist.json").exists())
        self.assertNotIn("triage_card=", cp.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
