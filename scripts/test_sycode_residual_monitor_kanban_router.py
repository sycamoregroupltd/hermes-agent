#!/usr/bin/env python3
"""Tests for residual Sycode monitor consumers (t_dd27733b).

Covers: healthy silence, breach route, delivery failure fail-visible,
dedupe/recovery, exact detector rerun, fill-rate supersession.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import sycode_pit_context_join as pit
import sycode_residual_monitor_kanban_router as router
import sycode_candle_per_symbol_freshness as candle


class DetectorRerunTests(unittest.TestCase):
    def test_candle_evaluate_rerun_is_deterministic(self):
        healthy = {tf: floor for tf, _, floor, _ in candle.TIMEFRAMES}
        a1, r1 = candle.evaluate(candle.TIMEFRAMES, healthy, baseline=healthy)
        a2, r2 = candle.evaluate(candle.TIMEFRAMES, healthy, baseline=healthy)
        self.assertEqual(a1, [])
        self.assertEqual(a1, a2)
        self.assertEqual(r1, r2)

        dead = dict(healthy)
        dead["1D"] = 10
        b1, _ = candle.evaluate(candle.TIMEFRAMES, dead, baseline=healthy)
        b2, _ = candle.evaluate(candle.TIMEFRAMES, dead, baseline=healthy)
        self.assertEqual(len(b1), 1)
        self.assertEqual(b1, b2)
        self.assertIn("1D", b1[0])

    def test_candle_cli_self_test_rerun(self):
        cmd = [sys.executable, str(SCRIPT_DIR / "sycode_candle_per_symbol_freshness.py"), "--self-test"]
        p1 = subprocess.run(cmd, capture_output=True, text=True)
        p2 = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)
        self.assertEqual(p1.returncode, p2.returncode)
        self.assertEqual(p1.stdout, p2.stdout)

    def test_pit_evaluate_healthy_and_leak(self):
        healthy = [
            {"event_id": "e1", "event_ts": 1000, "context_ts": 900},
            {"event_id": "e2", "event_ts": 2000, "context_ts": 2000},
        ]
        a1, _ = pit.evaluate(healthy)
        a2, _ = pit.evaluate(healthy)
        self.assertEqual(a1, [])
        self.assertEqual(a1, a2)
        leak = healthy + [{"event_id": "e3", "event_ts": 3000, "context_ts": 3500}]
        b1, rows = pit.evaluate(leak)
        b2, _ = pit.evaluate(leak)
        self.assertEqual(len(b1), 1)
        self.assertEqual(b1, b2)
        self.assertEqual(rows[-1][-1], "ALERT_LEAK")

    def test_pit_cli_self_test_rerun(self):
        cmd = [sys.executable, str(SCRIPT_DIR / "sycode_pit_context_join.py"), "--self-test"]
        p1 = subprocess.run(cmd, capture_output=True, text=True)
        p2 = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)
        self.assertEqual(p1.stdout, p2.stdout)


class RouterContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orig_ledger = router.LEDGER_PATH
        self.orig_audit = router.AUDIT_PATH
        router.LEDGER_PATH = Path(self.tmp.name) / "ledger.json"
        router.AUDIT_PATH = Path(self.tmp.name) / "audit.jsonl"
        self.created, self.commented, self.completed = [], [], []
        self.statuses = {}
        self.fail_create = False
        self.addCleanup(self._restore)

    def _restore(self):
        router.LEDGER_PATH = self.orig_ledger
        router.AUDIT_PATH = self.orig_audit

    def _fake_run(self, args, **kw):
        return router._fake_run(
            args, self.created, self.commented, self.completed, self.statuses,
            fail_create=self.fail_create,
        )

    def test_healthy_silence(self):
        with patch.object(router, "_run_hermes", self._fake_run), \
             patch.object(router, "_card_status", lambda tid: self.statuses.get(tid, "ready")), \
             patch.object(router, "existing_open_card", lambda key=None: None):
            res = router.process_tick(monitor="candle-per-symbol-freshness",
                                      healthy=True, findings=[])
        self.assertEqual(res["action"], "silent")
        self.assertEqual(self.created, [])

    def test_breach_route_creates_named_card(self):
        findings = [{"class": "ALERT_FLOOR", "detail": "candles[1D] 10/340"}]
        with patch.object(router, "_run_hermes", self._fake_run), \
             patch.object(router, "_card_status", lambda tid: self.statuses.get(tid, "ready")), \
             patch.object(router, "existing_open_card", lambda key=None: None):
            res = router.process_tick(monitor="candle-per-symbol-freshness",
                                      healthy=False, findings=findings, fp="fp")
        self.assertEqual(res["action"], "created")
        self.assertTrue(res["key"].startswith("sycode-residual-45e0b154b41c"))
        self.assertEqual(len(self.created), 1)
        create_args = self.created[0][0]
        self.assertIn("sycode-trading", create_args)
        self.assertIn("trading-devops", create_args)
        self.assertIn(res["key"], create_args)

    def test_dedupe_then_recovery(self):
        findings = [{"class": "ALERT_LEAK", "detail": "look-ahead"}]
        with patch.object(router, "_run_hermes", self._fake_run), \
             patch.object(router, "_card_status", lambda tid: self.statuses.get(tid, "ready")), \
             patch.object(router, "existing_open_card", lambda key=None: None):
            first = router.process_tick(monitor="pit-context-join", healthy=False,
                                        findings=findings, fp="a")
            second = router.process_tick(monitor="pit-context-join", healthy=False,
                                         findings=findings, fp="a")
            recovered = router.process_tick(monitor="pit-context-join", healthy=True,
                                            findings=[])
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "deduped")
        self.assertEqual(len(self.created), 1)
        self.assertEqual(len(self.commented), 2)  # dedupe comment + resolve comment
        self.assertEqual(recovered["action"], "resolved")
        self.assertEqual(len(self.completed), 1)

    def test_delivery_failure_is_visible(self):
        self.fail_create = True
        with patch.object(router, "_run_hermes", self._fake_run), \
             patch.object(router, "_card_status", lambda tid: None), \
             patch.object(router, "existing_open_card", lambda key=None: None):
            res = router.process_tick(
                monitor="drift-monitor", healthy=False,
                findings=[{"class": "DRIFT", "detail": "psi"}], fp="x")
        self.assertEqual(res["action"], "create_failed")

    def test_fill_rate_is_superseded_not_routed(self):
        res = router.process_tick(
            monitor="signal-fusion-fill-rate-check",
            healthy=False,
            findings=[{"class": "ACCEPTANCE", "detail": "jul-5"}],
        )
        self.assertEqual(res["action"], "superseded")
        self.assertEqual(res["job_id"], "ea20e2bc47c2")
        self.assertEqual(self.created, [])

    def test_cli_selftest(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "sycode_residual_monitor_kanban_router.py"), "--selftest"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SELFTEST_PASS", proc.stdout)

    def test_route_orchestrator_selftest(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "sycode_residual_monitor_route.py"), "--selftest"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ROUTE_SELFTEST_PASS", proc.stdout)


class ProposedAllowlistTests(unittest.TestCase):
    def test_router_catalog_names_real_handoffs(self):
        jobs = {meta["job_id"]: meta for meta in router.MONITORS.values()}
        self.assertEqual(set(jobs), {"45e0b154b41c", "965b5d5d4cb4", "53d45f13ff65"})
        for job_id, meta in jobs.items():
            self.assertIn("hermes kanban --board sycode-trading", meta["handoff"])
            self.assertIn(job_id, router.monitor_key(
                next(k for k, v in router.MONITORS.items() if v["job_id"] == job_id)
            ))
        self.assertEqual(router.SUPERSEDED["signal-fusion-fill-rate-check"]["job_id"], "ea20e2bc47c2")


if __name__ == "__main__":
    unittest.main()
