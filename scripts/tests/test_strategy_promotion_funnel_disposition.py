#!/usr/bin/env python3
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "strategy_promotion_funnel_disposition.py"
FIXTURE = Path(__file__).with_name("strategy_promotion_funnel_fixture.json")
sys.path.insert(0, str(ROOT))
from strategy_promotion_funnel_disposition import classify_arm, quality_statement  # noqa: E402


class StrategyPromotionFunnelDispositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(FIXTURE.read_text())
        cls.states = cls.payload["strategies"]

    def test_fixture_producer_covers_all_required_dispositions(self):
        output = subprocess.check_output([sys.executable, str(HELPER), "--fixture", str(FIXTURE)], text=True)
        result = json.loads(output)
        statuses = {row["arm_id"]: row["status"] for row in result["arms"]}
        self.assertEqual(statuses["fa8d1b58-4f82-434f-9b3f-ebb2b75965f8"], "RETIRED_NON_PROMOTABLE")
        self.assertEqual(statuses["random_entry_control"], "CONTROL_ONLY_NON_PROMOTABLE")
        self.assertEqual(statuses["11111111-1111-4111-8111-111111111111"], "COLLECT_MORE")
        self.assertEqual(statuses["22222222-2222-4222-8222-222222222222"], "READY_FOR_EVALUATION")
        self.assertEqual(statuses["LONG_1h"], "COLLECT_MORE")
        self.assertEqual(statuses["SHORT_1h"], "READY_FOR_EVALUATION")
        self.assertEqual(statuses["33333333-3333-4333-8333-333333333333"], "READY_FOR_EVALUATION")
        self.assertIn("Sample threshold alone does not satisfy promotionQuality", result["quality_statement"])

    def test_lookup_failure_is_blocked(self):
        row = classify_arm({"arm_id": "22222222-2222-4222-8222-222222222222", "n": 999}, self.states, lookup_error=True)
        self.assertEqual(row["disposition"], "UNKNOWN_BLOCKED")
        self.assertEqual(row["status"], "UNKNOWN_BLOCKED")

    def test_unknown_uuid_without_state_is_blocked(self):
        row = classify_arm({"arm_id": "44444444-4444-4444-8444-444444444444", "n": 999}, self.states)
        self.assertEqual(row["status"], "UNKNOWN_BLOCKED")

    def test_quality_statement_is_explicit(self):
        statement = quality_statement()
        for term in ("net-of-fee", "leak-free signal-time", "OOS/temporal-stability", "independent risk review"):
            self.assertIn(term, statement)


if __name__ == "__main__":
    unittest.main()
