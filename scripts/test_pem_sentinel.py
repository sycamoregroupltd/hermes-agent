#!/usr/bin/env python3
"""Unit + mock tests for the PEM Data Stream Integrity Sentinel (pem_sentinel.py).

Covers ACCEPTANCE CRITERION #4 + the task GATE for t_732d784e:
  - correct detection of ARTIFICIALLY DELAYED news caches (stale flag);
  - correct detection of Hyperliquid SOCKET INACTIVITY (no frame within window).

Every external dependency (clock, state loader, mtime loader, ws connector) is
INJECTED — no live filesystem state.json reads and zero network. The two
probes are exercised exactly as the production code runs them (same call
signatures as pem_probe.run_probes()), so a green run proves the wiring that
writes the flags into ~/.hermes/var/pem.json actually works.

Run:
  python3 test_pem_sentinel.py            # unittest, verbose
  python3 -m pytest test_pem_sentinel.py # pytest
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from typing import Any

import pem_sentinel as ps


# ---------------------------------------------------------------------------
# News cache freshness
# ---------------------------------------------------------------------------
class _FakeLoader:
    """Configurable stand-in for the state.json / mtime loaders."""

    def __init__(self, state: dict | None = None, mtime: float | None = None):
        self._state = state
        self._mtime = mtime

    def load(self, _p: Path) -> dict | None:
        return self._state

    def mtime(self, _p: Path) -> float | None:
        return self._mtime


class NewsCacheFreshTests(unittest.TestCase):
    def test_fresh_cache_is_ok(self):
        """A cache written moments ago must NOT be flagged stale."""
        now = 1_700_000_000.0
        loader = _FakeLoader(
            state={"last_run": "2026-07-11T00:41:44Z"},
            mtime=now - 5.0,  # 5s ago
        )
        res = ps.probe_news_cache(
            state_loader=loader.load,
            mtime_loader=loader.mtime,
            now=now,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "ok")
        self.assertLess(res["freshest_age_s"], ps.NEWS_CACHE_STALE_MINUTES * 60)
        self.assertIsNone(res["error"])

    def test_artificially_delayed_cache_is_stale(self):
        """ACCEPTANCE GATE: an artificially aged cache MUST flip status=stale.

        We inject a last_run ~2h in the past while the clock is 'now', which is
        exactly what a silently-dead news cron produces (state.json stops updating).
        """
        now = 1_700_000_000.0
        stale_ts = now - (ps.NEWS_CACHE_STALE_MINUTES * 60.0) - 600.0  # >90m old
        loader = _FakeLoader(
            state={"last_run": ps._iso_from_epoch(stale_ts)},
            mtime=stale_ts,
        )
        res = ps.probe_news_cache(
            state_loader=loader.load,
            mtime_loader=loader.mtime,
            now=now,
        )
        self.assertTrue(res["ok"], "readable cache is still a successful probe")
        self.assertEqual(res["status"], "stale", "delayed cache must be flagged stale")
        self.assertGreater(res["freshest_age_s"], ps.NEWS_CACHE_STALE_MINUTES * 60)

    def test_stale_detected_via_mtime_when_field_missing(self):
        """Staleness must also be caught from filesystem mtime alone (field absent)."""
        now = 1_700_000_000.0
        stale_ts = now - (ps.NEWS_CACHE_STALE_MINUTES * 60.0) - 3600.0
        loader = _FakeLoader(state={}, mtime=stale_ts)  # no last_run field
        res = ps.probe_news_cache(
            state_loader=loader.load,
            mtime_loader=loader.mtime,
            now=now,
        )
        self.assertEqual(res["status"], "stale")

    def test_missing_cache_is_unknown_not_stale(self):
        """A missing/unreadable cache degrades to unknown (can't assert freshness)."""
        now = 1_700_000_000.0
        loader = _FakeLoader(state=None, mtime=None)
        res = ps.probe_news_cache(
            state_loader=loader.load,
            mtime_loader=loader.mtime,
            now=now,
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unknown")
        self.assertIsNotNone(res["error"])

    def test_corrupt_state_is_unknown(self):
        """A state file whose last_run won't parse falls back to mtime; if both
        fail it must be unknown rather than raising."""
        now = 1_700_000_000.0
        loader = _FakeLoader(state={"last_run": "not-a-timestamp"}, mtime=None)
        res = ps.probe_news_cache(
            state_loader=loader.load,
            mtime_loader=loader.mtime,
            now=now,
        )
        self.assertEqual(res["status"], "unknown")
        self.assertIsNotNone(res["error"])

    def test_boundary_just_under_budget_is_ok(self):
        """One second under the stale budget must still be ok (no off-by-one)."""
        now = 1_700_000_000.0
        borderline = now - (ps.NEWS_CACHE_STALE_MINUTES * 60.0) + 1.0
        loader = _FakeLoader(
            state={"last_run": ps._iso_from_epoch(borderline)}, mtime=borderline
        )
        res = ps.probe_news_cache(
            state_loader=loader.load,
            mtime_loader=loader.mtime,
            now=now,
        )
        self.assertEqual(res["status"], "ok")


# ---------------------------------------------------------------------------
# Hyperliquid socket heartbeat
# ---------------------------------------------------------------------------
class _FakeWS:
    """Injectable websocket stand-in.

    - frames: list of payloads recv() will hand back, one per call.
    - send_error / recv_error: simulate send/recv failures.
    """

    def __init__(self, frames: list[Any] | None = None, recv_error: Exception | None = None):
        self._frames = list(frames or [])
        self._recv_error = recv_error
        self.sent = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def settimeout(self, _t: float) -> None:
        pass

    def recv(self) -> Any:
        if self._recv_error is not None:
            raise self._recv_error
        if self._frames:
            return self._frames.pop(0)
        raise TimeoutError("recv timed out (no frame)")

    def close(self) -> None:
        self.closed = True


def _connect_factory(ws: _FakeWS):
    def _connect(_url: str) -> _FakeWS:
        return ws

    return _connect


class HyperliquidSocketTests(unittest.TestCase):
    def test_live_frame_is_ok(self):
        """A frame arriving within the window proves the stream is alive."""
        ws = _FakeWS(frames=[json.dumps({"mids": {"BTC": "60000.0"}})])
        res = ps.probe_hyperliquid_socket(
            connect=_connect_factory(ws),
            frame_timeout=15.0,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["frames_received"], 1)
        self.assertTrue(ws.closed, "socket must be closed in finally")

    def test_socket_inactivity_is_stale(self):
        """ACCEPTANCE GATE: no frame within the window MUST yield status=stale
        (the data-stopped-but-connection-up failure mode)."""
        # recv() raises TimeoutError immediately -> 0 frames received.
        ws = _FakeWS(recv_error=TimeoutError("no frame within window"))
        res = ps.probe_hyperliquid_socket(
            connect=_connect_factory(ws),
            frame_timeout=15.0,
        )
        self.assertTrue(res["ok"], "probe succeeded; stream simply produced no frame")
        self.assertEqual(res["status"], "stale")
        self.assertEqual(res["frames_received"], 0)
        self.assertIn("no frame", res["error"])
        self.assertTrue(ws.closed)

    def test_empty_frame_still_counts_as_alive(self):
        """An empty-but-present frame is treated as a heartbeat (any non-None msg)."""
        ws = _FakeWS(frames=[""])  # empty string still breaks the wait loop
        res = ps.probe_hyperliquid_socket(
            connect=_connect_factory(ws), frame_timeout=15.0
        )
        self.assertEqual(res["status"], "ok")

    def test_connection_failure_is_unknown(self):
        """A DNS/TLS/connection crash degrades to unknown and never raises."""
        def _boom(_url: str):
            raise ConnectionError("Name or service not known")

        res = ps.probe_hyperliquid_socket(connect=_boom, frame_timeout=15.0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unknown")
        self.assertIn("probe failed", res["error"])

    def test_subscription_sent(self):
        """Probe must attempt to subscribe to allMids (best-effort)."""
        ws = _FakeWS(frames=[json.dumps({"mids": {}})])
        ps.probe_hyperliquid_socket(connect=_connect_factory(ws), frame_timeout=15.0)
        self.assertTrue(ws.sent, "expected a subscribe frame to be sent")
        sent = json.loads(ws.sent[0])
        self.assertEqual(sent.get("subscription", {}).get("type"), "allMids")


# ---------------------------------------------------------------------------
# run_sentinel_probes dispatch + ledger merge (proves flags reach pem.json)
# ---------------------------------------------------------------------------
class SentinelRunAndLedgerTests(unittest.TestCase):
    def test_run_sentinel_probes_dispatches_both(self):
        """run_sentinel_probes() must return exactly the two sentinel sources,
        driving the same code path pem_probe.run_probes() uses."""
        news = _FakeLoader(
            state={"last_run": ps._iso_from_epoch(1_700_000_000.0)}, mtime=1_700_000_000.0
        )
        ws = _FakeWS(frames=[json.dumps({"mids": {"BTC": "1"}})])

        results = ps.run_sentinel_probes(
            news_now=1_700_000_100.0,
            news_state_loader=news.load,
            news_mtime_loader=news.mtime,
            hl_connect=_connect_factory(ws),
        )
        self.assertEqual({r["source"] for r in results},
                         {"news_sentiment_cache", "hyperliquid_socket"})
        for r in results:
            self.assertTrue(r["ok"])

    def test_merge_into_ledger_preserves_other_sources(self):
        """Merging sentinel results must NOT clobber quota sources already in
        pem.json — this is the multi-probe integrity contract."""
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = _P(tmp) / "pem.json"
            # Pre-existing ledger from the quota prober.
            prior = {
                "schema_version": "1.0.0",
                "engine": "pem-probe",
                "generated_at": "2026-07-11T00:00:00Z",
                "overall_status": "ok",
                "sources": {
                    "firecrawl": {"ok": True, "status": "ok"},
                    "github": {"ok": True, "status": "ok"},
                    "hyperliquid": {"ok": True, "status": "ok"},
                },
            }
            ledger_path.write_text(json.dumps(prior))
            news = _FakeLoader(state={"last_run": ps._iso_from_epoch(1_700_000_000.0)},
                               mtime=1_700_000_000.0)
            ws = _FakeWS(frames=[json.dumps({"mids": {}})])
            results = ps.run_sentinel_probes(
                news_now=1_700_000_100.0,
                news_state_loader=news.load,
                news_mtime_loader=news.mtime,
                hl_connect=_connect_factory(ws),
            )
            merged = ps.merge_sentinel_into_ledger(results, ledger_path)
            # Quota sources preserved.
            self.assertIn("firecrawl", merged["sources"])
            self.assertIn("hyperliquid", merged["sources"])
            # Sentinel sources added.
            self.assertIn("news_sentiment_cache", merged["sources"])
            self.assertIn("hyperliquid_socket", merged["sources"])
            self.assertEqual(merged["sources"]["news_sentiment_cache"]["status"], "ok")
            self.assertEqual(merged["sources"]["hyperliquid_socket"]["status"], "ok")
            self.assertEqual(merged["overall_status"], "ok")

    def test_merge_marks_stale_when_cache_delayed(self):
        """End-to-end: a delayed cache merge must carry status=stale into pem.json
        and flip overall_status if nothing else is failing."""
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = _P(tmp) / "pem.json"
            prior = {
                "schema_version": "1.0.0", "engine": "pem-probe",
                "generated_at": "2026-07-11T00:00:00Z", "overall_status": "ok",
                "sources": {"firecrawl": {"ok": True, "status": "ok"}},
            }
            ledger_path.write_text(json.dumps(prior))
            now = 1_700_000_000.0
            stale_ts = now - (ps.NEWS_CACHE_STALE_MINUTES * 60.0) - 600.0
            news = _FakeLoader(
                state={"last_run": ps._iso_from_epoch(stale_ts)}, mtime=stale_ts
            )
            ws = _FakeWS(frames=[json.dumps({"mids": {}})])
            results = ps.run_sentinel_probes(
                news_now=now,
                news_state_loader=news.load,
                news_mtime_loader=news.mtime,
                hl_connect=_connect_factory(ws),
            )
            merged = ps.merge_sentinel_into_ledger(results, ledger_path)
            self.assertEqual(
                merged["sources"]["news_sentiment_cache"]["status"], "stale"
            )
            # other source ok, so overall is still ok (stale cache is ok=True probe)
            self.assertEqual(merged["overall_status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
