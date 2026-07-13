#!/usr/bin/env python3
"""
Native Hermes closed-loop Sense collector (no-agent, deterministic).

Aggregates compact state from key sources into uaa-rules/closed-loop/state/latest.json.
Run via cron (no LLM). Outputs only on material change or always-compact digest.

Usage (via hermes cron or direct):
  python /home/frank/.hermes/scripts/closed_loop_sense.py

Follows the target architecture: cheap collector, compact JSON + digest only.
"""

import json
import os
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone

STATE_DIR = Path("/home/frank/uaa-rules/closed-loop/state")
OUT_FILE = STATE_DIR / "latest.json"
LEDGER = Path("/home/frank/uaa-rules/closed-loop/ACTION-LEDGER.jsonl")

SOURCES = [
    Path("/home/frank/uaa-rules/FLEET-STATUS.md"),
    Path("/home/frank/uaa-rules/PROFILE-CATALOG.md"),
    Path("/home/frank/uaa-rules/report-retention/latest.json"),
    Path("/home/frank/uaa-rules/HERMES_BASELINE_2026-07-04.md"),
]

def compact_digest(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        content = path.read_text(errors="ignore")[:2000]  # compact
        h = hashlib.sha256(content.encode()).hexdigest()[:16]
        lines = len(content.splitlines())
        return {"path": str(path), "exists": True, "sha16": h, "lines": lines, "size": path.stat().st_size}
    except Exception as e:
        return {"path": str(path), "error": str(e)[:100]}

def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()

    data = {
        "generated": now,
        "sources": [compact_digest(p) for p in SOURCES],
        "cron_summary": {
            "note": "Run 'hermes -p jarvis cron list --all' for full native view",
            "active_no_agent_examples": 40,  # approx from baseline
        },
        "kanban_note": "Use 'hermes kanban' + boards for dispatch state",
        "ledger_tail_note": "See ACTION-LEDGER.jsonl for recent distillations",
    }

    # Simple material change detection
    old = ""
    if OUT_FILE.exists():
        old = OUT_FILE.read_text()

    new = json.dumps(data, indent=2, sort_keys=True)
    if new != old:
        OUT_FILE.write_text(new + "\n")
        print(f"Updated {OUT_FILE}")
    else:
        print("No material change; compact state unchanged.")

    # Always output a tiny digest for piping to agents/ledger
    digest = hashlib.sha256(new.encode()).hexdigest()[:12]
    print(json.dumps({"digest": digest, "ts": now, "sources_checked": len(SOURCES)}))

if __name__ == "__main__":
    main()
