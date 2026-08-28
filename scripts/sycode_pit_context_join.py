#!/usr/bin/env python3
"""sycode_pit_context_join.py

Point-in-time context-join validator for residual job 965b5d5d4cb4
(t_dd27733b). Replaces the broken profile-local symlink
`scripts/pit-context-join.sh` -> missing
`profiles/sycode-trading-pm/scripts/pit-context-join.sh`.

The detector is leak-free and paper/read-only:
  a context row is PIT-valid iff context_ts <= event_ts.
  Any look-ahead join (context after the event) is a BREACH.

Live fetch is opt-in (SYCODE_PIT_FETCH_CMD or docker psql). Missing
live data is an operational failure (exit 1), never silent-green.
--self-test / evaluate() need no DB.

Exit 0 healthy, 1 operational error, 2 breach.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def evaluate(rows: list[dict]) -> tuple[list[str], list[tuple]]:
    """rows: {event_id, event_ts, context_id, context_ts} unix seconds or ISO.

    Returns (alerts, result_rows) where result_rows are
    (event_id, event_ts, context_ts, status).
    """
    alerts: list[str] = []
    out: list[tuple] = []
    for row in rows:
        event_id = str(row.get("event_id") or row.get("id") or "?")
        event_ts = _as_epoch(row.get("event_ts"))
        context_ts = _as_epoch(row.get("context_ts"))
        if event_ts is None or context_ts is None:
            status = "ERROR"
            alerts.append(f"  🔴 pit[{event_id}]: unparseable timestamps event={row.get('event_ts')!r} context={row.get('context_ts')!r}")
        elif context_ts > event_ts:
            status = "ALERT_LEAK"
            delta = context_ts - event_ts
            alerts.append(
                f"  🔴 pit[{event_id}]: context_ts {context_ts} > event_ts {event_ts} "
                f"(+{delta:.0f}s look-ahead) — future context joined"
            )
        else:
            status = "OK"
        out.append((event_id, event_ts, context_ts, status))
    return alerts, out


def _as_epoch(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _self_test() -> int:
    healthy = [
        {"event_id": "e1", "event_ts": 1_000, "context_id": "c1", "context_ts": 900},
        {"event_id": "e2", "event_ts": 2_000, "context_id": "c2", "context_ts": 2_000},
    ]
    leak = list(healthy)
    leak.append({"event_id": "e3", "event_ts": 3_000, "context_id": "c3", "context_ts": 3_500})
    a1, _ = evaluate(healthy)
    a2, r2 = evaluate(leak)
    ok = (len(a1) == 0) and (len(a2) == 1) and any(r[-1] == "ALERT_LEAK" for r in r2)
    print("  self-test healthy_alerts=%d leak_alerts=%d" % (len(a1), len(a2)))
    print("SELF-TEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _fetch_live_rows() -> list[dict]:
    """Read-only fetch. Prefer an explicit command so we never invent SQL."""
    cmd = os.environ.get("SYCODE_PIT_FETCH_CMD")
    if cmd:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError("SYCODE_PIT_FETCH_CMD failed rc=%s: %s" % (
                proc.returncode, (proc.stderr or proc.stdout)[:200]))
        data = json.loads(proc.stdout or "[]")
        if not isinstance(data, list):
            raise RuntimeError("SYCODE_PIT_FETCH_CMD must emit a JSON list")
        return data
    fixture = os.environ.get("SYCODE_PIT_FIXTURE")
    if fixture:
        data = json.loads(open(fixture, encoding="utf-8").read())
        if not isinstance(data, list):
            raise RuntimeError("SYCODE_PIT_FIXTURE must be a JSON list")
        return data
    raise RuntimeError(
        "no live PIT rows: set SYCODE_PIT_FETCH_CMD (read-only JSON list) "
        "or SYCODE_PIT_FIXTURE; refusing to invent a production SQL join"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    try:
        rows = _fetch_live_rows()
        alerts, result_rows = evaluate(rows)
    except Exception as exc:
        print("PIT CONTEXT-JOIN — DEGRADED: probe error — %s" % str(exc)[:200])
        return 1
    stamp = datetime.now(timezone.utc).isoformat()
    if args.json:
        print(json.dumps({
            "monitor": "pit-context-join",
            "healthy": not alerts,
            "findings": [{"class": "ALERT_LEAK", "detail": a.strip()} for a in alerts],
            "rows": len(result_rows),
            "stamp": stamp,
        }, sort_keys=True))
    else:
        print("PIT CONTEXT-JOIN @ %s" % stamp)
        for event_id, event_ts, context_ts, status in result_rows:
            print("  [%s] event=%s event_ts=%s context_ts=%s" % (
                "OK" if status == "OK" else "XX", event_id, event_ts, context_ts))
        if alerts:
            print("VERDICT: DEGRADED — %d look-ahead join(s)" % len(alerts))
            print("\n".join(alerts))
        else:
            print("VERDICT: GREEN — all context joins are point-in-time")
    return 2 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
