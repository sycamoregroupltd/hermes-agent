#!/usr/bin/env python3
"""Unit tests for the PEM API Quota Prober (pem_probe.py).

Focus: correct ledger schema and correct value parsing for each source.
All external calls are injected — no network / no CLI is touched during tests.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pem_probe as pp


class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- Firecrawl --------------------------------------------------------------
class FirecrawlProbeTests(unittest.TestCase):
    def _runner(self, payload_json: str, returncode: int = 0):
        def _run(cmd, **_kw):
            return _FakeProc(returncode, stdout=payload_json, stderr="")
        return _run

    def test_parse_good_balance(self):
        body = json.dumps({"success": True, "data": {
            "remainingCredits": 250, "planCredits": 1000,
            "billingPeriodStart": "2026-06-23T06:51:12Z",
            "billingPeriodEnd": "2026-07-23T06:51:12Z"}})
        res = pp.probe_firecrawl(runner=self._runner(body))
        self.assertTrue(res["ok"])
        self.assertEqual(res["remaining_credits"], 250.0)
        self.assertEqual(res["plan_credits"], 1000.0)
        self.assertEqual(res["used_credits"], 750.0)
        self.assertAlmostEqual(res["credit_pct"], 25.0)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["billing_period_end"], "2026-07-23T06:51:12Z")

    def test_exhausted_balance(self):
        body = json.dumps({"success": True, "data": {
            "remainingCredits": 0, "planCredits": 1000}})
        res = pp.probe_firecrawl(runner=self._runner(body))
        self.assertTrue(res["ok"])
        self.assertEqual(res["remaining_credits"], 0.0)
        self.assertEqual(res["status"], "exhausted")

    def test_warning_balance(self):
        body = json.dumps({"success": True, "data": {
            "remainingCredits": 10, "planCredits": 1000}})
        res = pp.probe_firecrawl(runner=self._runner(body))
        self.assertAlmostEqual(res["credit_pct"], 1.0)
        self.assertEqual(res["status"], "warning")

    def test_cli_error_sets_error_and_unknown(self):
        res = pp.probe_firecrawl(runner=self._runner("boom", returncode=1))
        self.assertFalse(res["ok"])
        self.assertIsNotNone(res["error"])
        self.assertEqual(res["status"], "unknown")
        self.assertIsNone(res["remaining_credits"])

    def test_invalid_json(self):
        res = pp.probe_firecrawl(runner=self._runner("{not json"))
        self.assertFalse(res["ok"])
        self.assertIn("invalid JSON", res["error"])

    def test_runner_exception_is_safe(self):
        def _boom(cmd, **_kw):
            raise FileNotFoundError("firecrawl not on PATH")
        res = pp.probe_firecrawl(runner=_boom)
        self.assertFalse(res["ok"])
        self.assertIn("invocation failed", res["error"])


# --- GitHub -----------------------------------------------------------------
class GitHubProbeTests(unittest.TestCase):
    def _runner(self, payload_json: str, returncode: int = 0):
        def _run(cmd, **_kw):
            return _FakeProc(returncode, stdout=payload_json, stderr="")
        return _run

    def _good_payload(self, core_remaining: int = 4991) -> str:
        return json.dumps({
            "resources": {
                "core": {"limit": 5000, "used": 9, "remaining": core_remaining,
                         "reset": 1783732840},
                "search": {"limit": 30, "used": 0, "remaining": 30, "reset": 1783730758},
            },
            "rate": {"limit": 5000, "used": 9, "remaining": core_remaining,
                     "reset": 1783732840},
        })

    def test_parse_good(self):
        res = pp.probe_github(runner=self._runner(self._good_payload()))
        self.assertTrue(res["ok"])
        self.assertEqual(res["remaining"], 4991.0)
        self.assertEqual(res["limit"], 5000.0)
        self.assertEqual(res["used"], 9.0)
        self.assertEqual(res["reset_epoch"], 1783732840.0)
        self.assertEqual(res["status"], "ok")
        # per-resource summary captured
        self.assertIn("search", res["resources"])
        self.assertEqual(res["resources"]["search"]["remaining"], 30.0)

    def test_warning_when_core_low(self):
        res = pp.probe_github(runner=self._runner(self._good_payload(core_remaining=42)))
        self.assertEqual(res["status"], "warning")
        self.assertEqual(res["resources"]["core"]["status"], "warning")

    def test_cli_error(self):
        res = pp.probe_github(runner=self._runner("nope", returncode=1))
        self.assertFalse(res["ok"])
        self.assertIsNotNone(res["error"])
        self.assertEqual(res["status"], "unknown")

    def test_invalid_json(self):
        res = pp.probe_github(runner=self._runner("garbage"))
        self.assertFalse(res["ok"])
        self.assertIn("invalid JSON", res["error"])

    def test_runner_exception_is_safe(self):
        def _boom(cmd, **_kw):
            raise FileNotFoundError("gh not on PATH")
        res = pp.probe_github(runner=_boom)
        self.assertFalse(res["ok"])
        self.assertIn("invocation failed", res["error"])


# --- Hyperliquid ------------------------------------------------------------
class HyperliquidProbeTests(unittest.TestCase):
    def test_healthy(self):
        def _post(url, payload):
            self.assertEqual(payload, {"type": "exchangeStatus"})
            return {"specialStatuses": None, "time": 1783730719629}
        res = pp.probe_hyperliquid(post_json=_post)
        self.assertTrue(res["ok"])
        # http_status is populated by the real transport only; injected doubles
        # return None for it.
        self.assertIsNone(res["http_status"])
        self.assertIsNone(res["special_statuses"])
        self.assertEqual(res["server_time_epoch_ms"], 1783730719629.0)
        self.assertEqual(res["status"], "ok")

    def test_degraded_on_special_statuses(self):
        def _post(url, payload):
            return {"specialStatuses": {"maintenance": True}, "time": 1}
        res = pp.probe_hyperliquid(post_json=_post)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "degraded")
        self.assertIsNotNone(res["special_statuses"])

    def test_failure_is_safe(self):
        def _post(url, payload):
            raise RuntimeError("connection reset")
        res = pp.probe_hyperliquid(post_json=_post)
        self.assertFalse(res["ok"])
        self.assertIn("probe failed", res["error"])
        self.assertEqual(res["status"], "unknown")

    def test_non_dict_body(self):
        def _post(url, payload) -> "Any":  # noqa: ANN401 - deliberately wrong for error branch
            return ["unexpected", "list"]
        res = pp.probe_hyperliquid(post_json=_post)
        self.assertFalse(res["ok"])
        self.assertIn("unexpected", res["error"])


# --- Engine + ledger --------------------------------------------------------
class LedgerTests(unittest.TestCase):
    def _sample_results(self) -> list[dict]:
        return [
            {"source": "firecrawl", "ok": True, "status": "ok",
             "remaining_credits": 250.0, "plan_credits": 1000.0},
            {"source": "github", "ok": True, "status": "ok", "remaining": 4991.0},
            {"source": "hyperliquid", "ok": True, "status": "ok"},
        ]

    def test_ledger_schema(self):
        ledger = pp.build_ledger(self._sample_results())
        for key in ("schema_version", "generated_at", "engine",
                    "overall_status", "sources"):
            self.assertIn(key, ledger)
        self.assertEqual(ledger["schema_version"], pp.LEDGER_SCHEMA_VERSION)
        self.assertEqual(ledger["engine"], "pem-probe")
        self.assertEqual(ledger["overall_status"], "ok")
        self.assertEqual(set(ledger["sources"].keys()),
                         {"firecrawl", "github", "hyperliquid"})

    def test_overall_degraded_when_any_error(self):
        results = self._sample_results()
        results[1] = {"source": "github", "ok": False, "error": "boom",
                      "status": "unknown"}
        ledger = pp.build_ledger(results)
        self.assertEqual(ledger["overall_status"], "degraded")

    def test_overall_ok_only_if_all_ok(self):
        # overall_status is "ok" only when every probed source succeeded.
        results = [
            {"source": "firecrawl", "ok": False, "error": "x", "status": "unknown"},
            {"source": "github", "ok": True, "status": "ok"},
            {"source": "hyperliquid", "ok": False, "error": "y", "status": "unknown"},
        ]
        ledger = pp.build_ledger(results)
        self.assertEqual(ledger["overall_status"], "degraded")

        # All-ok case flips to "ok".
        all_ok = [
            {"source": "firecrawl", "ok": True, "status": "ok"},
            {"source": "github", "ok": True, "status": "ok"},
            {"source": "hyperliquid", "ok": True, "status": "ok"},
        ]
        self.assertEqual(pp.build_ledger(all_ok)["overall_status"], "ok")

    def test_write_ledger_atomic_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "pem.json"
            written = pp.write_ledger(pp.build_ledger(self._sample_results()), target)
            self.assertTrue(written.exists())
            # temp file must be gone
            self.assertFalse((target.parent / "pem.json.tmp").exists())
            parsed = json.loads(written.read_text())
            self.assertEqual(parsed["engine"], "pem-probe")
            self.assertEqual(set(parsed["sources"].keys()),
                             {"firecrawl", "github", "hyperliquid"})

    def test_run_probes_unknown_source(self):
        results = pp.run_probes(["bogus"], runner=lambda *a, **k: _FakeProc(0))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "bogus")
        self.assertFalse(results[0]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
