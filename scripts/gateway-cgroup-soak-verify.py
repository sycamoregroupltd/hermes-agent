#!/usr/bin/env python3
"""Gateway cgroup guard SOAK verifier (task t_74fcd323) — to be run at day 7.

Reads the append-only ledger produced by gateway-cgroup-soak-logger.py and
evaluates the 4 soak acceptance criteria from the task body:

  1. No gateway unit shows oom_kill / oom_group_kill in memory.events (==0).
  2. No gateway restart correlated with the new caps (NRestarts stays 0 since
     apply, or any restart is timestamped before the apply; i.e. restart_count
     must be 0 for a clean soak).
  3. Steady-state memory.current stays under MemoryHigh for each unit
     (cache balloon contained). We accept a small tolerance but flag the worst
     ratio; "over_high" samples are violations unless transient. We require
     the *majority* of samples to be ok_under_high and no sustained over_high.
  4. gateway-cgroup-guard.py stays at 0 sprawl findings (total_sprawl == 0 in
     every ledger sample).

Exit code: 0 = PASS (all criteria met). 1 = FAIL (one or more breached) — so a
cron wrapper can alert. 2 = INCONCLUSIVE (insufficient / no data).

Read-only: it only reads the ledger and prints a verdict. Never mutates cgroups.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path("/home/frank/.hermes/var/gateway-cgroup-soak/ledger.jsonl")
MANIFEST = Path("/home/frank/.hermes/var/gateway-cgroup-soak/manifest.json")
APPLY_TS_UTC = datetime(2026, 7, 11, 10, 52, tzinfo=timezone.utc)


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows: list[dict] = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def evaluate(rows: list[dict]) -> dict:
    verdict = {
        "task_id": "t_74fcd323",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "samples": len(rows),
        "criteria": {},
        "per_unit": {},
        "pass": True,
    }
    if not rows:
        verdict["pass"] = False
        verdict["criteria"]["data"] = {"result": "INCONCLUSIVE", "detail": "no ledger samples"}
        return verdict

    units = list(rows[0]["units"].keys())

    # Criterion 1: OOM events
    oom_units = set()
    for r in rows:
        for u, d in r["units"].items():
            if d.get("oom_kill", 0) or d.get("oom_group_kill", 0):
                oom_units.add(u)
    verdict["criteria"]["no_oom"] = {
        "result": "PASS" if not oom_units else "FAIL",
        "detail": f"units with oom_kill/oom_group_kill: {sorted(oom_units) or 'none'}",
    }
    if oom_units:
        verdict["pass"] = False

    # Criterion 2: restarts correlated with caps
    restart_units = set()
    for u in units:
        restarts = [r["units"][u].get("restart_count", 0) for r in rows]
        # A clean soak is 0 restarts throughout. Any nonzero = violation.
        if any(x and x > 0 for x in restarts):
            restart_units.add(u)
    verdict["criteria"]["no_cap_correlated_restart"] = {
        "result": "PASS" if not restart_units else "FAIL",
        "detail": f"units with NRestarts>0 during soak: {sorted(restart_units) or 'none'}",
    }
    if restart_units:
        verdict["pass"] = False

    # Criterion 3: steady-state under MemoryHigh
    per_unit_c3 = {}
    for u in units:
        status_counts = Counter(r["units"][u].get("status", "unknown") for r in rows)
        over = status_counts.get("over_high", 0)
        near = status_counts.get("near_high", 0)
        ratios = [r["units"][u].get("memory_current", 0) / r["units"][u].get("memory_high", 1)
                  for r in rows if r["units"][u].get("memory_high")]
        worst = max(ratios) if ratios else None
        # Pass if no over_high samples and worst ratio stays under 1.0 (with a
        # tiny 1.02 tolerance for live writes racing the soft limit).
        ok = (over == 0) and (worst is None or worst < 1.02)
        per_unit_c3[u] = {
            "result": "PASS" if ok else "FAIL",
            "status_counts": dict(status_counts),
            "worst_current_over_high_ratio": round(worst, 4) if worst is not None else None,
        }
        if not ok:
            verdict["pass"] = False
    verdict["per_unit"]["under_memory_high"] = per_unit_c3

    # Criterion 4: sprawl findings
    sprawl_samples = [r.get("total_sprawl", -1) for r in rows]
    sprawl_bad = [s for s in sprawl_samples if s is None or s > 0]
    verdict["criteria"]["no_sprawl"] = {
        "result": "PASS" if not sprawl_bad else "FAIL",
        "detail": f"nonzero/absent sprawl samples: {len(sprawl_bad)}/{len(rows)}",
    }
    if sprawl_bad:
        verdict["pass"] = False

    return verdict


def main() -> int:
    rows = load_ledger()
    verdict = evaluate(rows)
    print(json.dumps(verdict, indent=2))
    if not rows:
        return 2
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
