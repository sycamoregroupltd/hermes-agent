#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""sycode_residual_monitor_route.py

Run one residual Sycode detector and hand the verdict to the already-configured
sycode-trading kanban incident consumer (t_dd27733b).

  python3 sycode_residual_monitor_route.py --monitor candle-per-symbol-freshness
  python3 sycode_residual_monitor_route.py --monitor pit-context-join
  python3 sycode_residual_monitor_route.py --monitor drift-monitor
  python3 sycode_residual_monitor_route.py --monitor signal-fusion-fill-rate-check

Healthy ticks stay silent (router action=silent). Breaches create/dedupe a
kanban card. Detector operational errors and router delivery failures exit
non-zero (fail-visible). Fill-rate is superseded and never routed.

--selftest exercises detector evaluate() reruns + router contract without
hermes CLI, DB, or live board writes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROUTER = SCRIPT_DIR / "sycode_residual_monitor_kanban_router.py"

MONITOR_DETECTORS = {
    "candle-per-symbol-freshness": [sys.executable, str(SCRIPT_DIR / "sycode_candle_per_symbol_freshness.py")],
    "pit-context-join": [sys.executable, str(SCRIPT_DIR / "sycode_pit_context_join.py")],
    "drift-monitor": ["bash", str(SCRIPT_DIR / "sycode-drift-monitor.sh")],
}


def _run(cmd: list[str], env: dict | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env or os.environ.copy())


def route_verdict(monitor: str, healthy: bool, findings: list[dict],
                  dry_run: bool = False) -> tuple[int, dict]:
    payload = json.dumps({
        "monitor": monitor,
        "healthy": healthy,
        "findings": findings,
    })
    env = os.environ.copy()
    env["SYCODE_RESIDUAL_MONITOR"] = monitor
    env["SYCODE_RESIDUAL_HEALTHY"] = "1" if healthy else "0"
    cmd = [sys.executable, str(ROUTER)]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, env=env, timeout=60)
    result: dict = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("SYCODE_RESIDUAL_KANBAN_ROUTER "):
            try:
                result = json.loads(line.split(" ", 1)[1])
            except Exception:
                result = {"raw": line}
    if proc.returncode != 0 and not result:
        result = {"action": "router_failed", "stderr": (proc.stderr or "")[:300]}
    return proc.returncode, result


def run_detector(monitor: str) -> tuple[int, list[dict], str]:
    if monitor == "signal-fusion-fill-rate-check":
        return 0, [], "superseded"
    cmd = MONITOR_DETECTORS.get(monitor)
    if not cmd:
        return 1, [{"class": "ERROR", "detail": f"unknown monitor {monitor}"}], "unknown"
    if monitor == "drift-monitor" and not Path(cmd[-1]).exists():
        return 1, [{"class": "ERROR", "detail": "sycode-drift-monitor.sh missing"}], "missing"
    try:
        proc = _run(cmd)
    except Exception as exc:
        return 1, [{"class": "ERROR", "detail": str(exc)[:200]}], "error"
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    findings: list[dict] = []
    if proc.returncode == 2 or (proc.returncode != 0 and proc.returncode != 1):
        for line in text.splitlines():
            if "ALERT" in line or "🔴" in line or "DRIFT" in line.upper() or "VERDICT: DEGRADED" in line:
                findings.append({"class": "BREACH", "detail": line.strip()})
        if not findings and text:
            findings.append({"class": "BREACH", "detail": text[:500]})
        return 2, findings, text
    if proc.returncode == 1:
        findings.append({"class": "ERROR", "detail": text[:500] or "detector operational error"})
        return 1, findings, text
    return 0, [], text


def _selftest() -> int:
    failures: list[str] = []
    # Exact detector reruns (no DB).
    c1 = _run([sys.executable, str(SCRIPT_DIR / "sycode_candle_per_symbol_freshness.py"), "--self-test"])
    c2 = _run([sys.executable, str(SCRIPT_DIR / "sycode_candle_per_symbol_freshness.py"), "--self-test"])
    if c1.returncode != 0 or c2.returncode != 0 or c1.stdout != c2.stdout:
        failures.append("candle freshness --self-test must be deterministic and green")
    p1 = _run([sys.executable, str(SCRIPT_DIR / "sycode_pit_context_join.py"), "--self-test"])
    p2 = _run([sys.executable, str(SCRIPT_DIR / "sycode_pit_context_join.py"), "--self-test"])
    if p1.returncode != 0 or p2.returncode != 0 or p1.stdout != p2.stdout:
        failures.append("PIT --self-test must be deterministic and green")

    # Router contract via FakeHarness --selftest.
    r = _run([sys.executable, str(ROUTER), "--selftest"])
    if r.returncode != 0:
        failures.append("router --selftest failed: %s" % ((r.stdout or "") + (r.stderr or ""))[:300])

    # Route fill-rate as superseded (no card).
    rc, result = route_verdict("signal-fusion-fill-rate-check", healthy=False,
                               findings=[{"class": "ACCEPTANCE", "detail": "old"}],
                               dry_run=True)
    if result.get("action") != "superseded":
        # dry-run still hits SUPERSEDED before dry_run branch
        rc2, result = route_verdict("signal-fusion-fill-rate-check", healthy=False,
                                    findings=[{"class": "ACCEPTANCE", "detail": "old"}])
        if result.get("action") != "superseded" or rc2 != 0:
            failures.append("fill-rate route must be superseded, got %s rc=%s" % (result, rc2))

    # Healthy dry-run is silent-equivalent (no live writes).
    rc, result = route_verdict("candle-per-symbol-freshness", healthy=True, findings=[], dry_run=True)
    if rc != 0 or result.get("action") != "dry_run_healthy":
        failures.append("healthy dry-run should be dry_run_healthy, got %s rc=%s" % (result, rc))

    # Breach dry-run names the kanban handoff.
    rc, result = route_verdict(
        "pit-context-join", healthy=False,
        findings=[{"class": "ALERT_LEAK", "detail": "look-ahead"}],
        dry_run=True,
    )
    handoff = str(result.get("handoff") or "")
    if rc != 0 or result.get("action") != "dry_run_breach" or "sycode-trading" not in handoff:
        failures.append("breach dry-run must name sycode-trading handoff, got %s" % result)

    if failures:
        print("ROUTE_SELFTEST_FAIL")
        for fl in failures:
            print(" -", fl)
        return 1
    print("ROUTE_SELFTEST_PASS detector_rerun router_selftest "
          "healthy_silent fill_rate_superseded breach_handoff")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    monitor = None
    dry_run = "--dry-run" in argv
    if "--monitor" in argv:
        monitor = argv[argv.index("--monitor") + 1]
    if not monitor:
        print("usage: sycode_residual_monitor_route.py --monitor <name> [--dry-run|--selftest]",
              file=sys.stderr)
        return 2
    detect_rc, findings, text = run_detector(monitor)
    if detect_rc == 1:
        # Operational failure must be visible even if router is healthy-silent.
        print(text or "detector operational error", file=sys.stderr)
        route_rc, result = route_verdict(monitor, healthy=False, findings=findings, dry_run=dry_run)
        if result.get("action") == "superseded":
            print(json.dumps(result, sort_keys=True))
            return 0
        print(json.dumps({"detector_rc": detect_rc, "router": result}, sort_keys=True))
        return 1 if route_rc == 0 else route_rc
    healthy = detect_rc == 0
    route_rc, result = route_verdict(monitor, healthy=healthy, findings=findings, dry_run=dry_run)
    if result.get("action") == "silent" and healthy:
        # Keep healthy runs silent: no extra human payload.
        return 0
    print(json.dumps({"detector_rc": detect_rc, "router": result}, sort_keys=True))
    if not healthy and result.get("action") not in {"created", "deduped", "dry_run_breach", "superseded"}:
        return 2 if route_rc == 0 else route_rc
    return route_rc


if __name__ == "__main__":
    sys.exit(main())
