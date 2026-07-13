#!/usr/bin/env python3
"""DGX Hermes fleet reflection coverage audit.

Checks that active profiles have REFLECTION.md and SOUL.md reflection invariant,
and reports stale reflection logs. Safe read-only script; intended for cron context.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path("/home/frank/.hermes")
PROFILES = ROOT / "profiles"
NOW = int(time.time())
STALE_DAYS = int(os.environ.get("REFLECTION_STALE_DAYS", "7"))
STALE_SECONDS = STALE_DAYS * 86400

profiles = sorted(
    p for p in PROFILES.iterdir() if p.is_dir() and not p.name.startswith(".")
)
missing_reflection = []
missing_soul_invariant = []
stale = []
placeholder_reflection = []
errors = []

for prof in profiles:
    ref = prof / "REFLECTION.md"
    soul = prof / "SOUL.md"
    if not ref.exists() or not ref.read_text(errors="replace").strip():
        missing_reflection.append(prof.name)
    else:
        ref_text = ref.read_text(errors="replace")
        age = NOW - int(ref.stat().st_mtime)
        if age > STALE_SECONDS:
            stale.append({"profile": prof.name, "age_days": round(age / 86400, 1)})
        sentinel = "First scheduled cycle should replace this placeholder"
        if sentinel in ref_text:
            after = ref_text.split(sentinel, 1)[1] if sentinel in ref_text else ""
            if not any(kw in after for kw in ["Evidence-backed", "t_", "verified", "probe"]):
                placeholder_reflection.append(prof.name)
    if not soul.exists():
        missing_soul_invariant.append(
            {"profile": prof.name, "reason": "SOUL.md missing"}
        )
    else:
        text = soul.read_text(errors="replace")
        if "REFLECTION AS MEDITATION" not in text and "REFLECTION.md" not in text:
            missing_soul_invariant.append(
                {"profile": prof.name, "reason": "no reflection invariant"}
            )

summary = {
    "host": os.uname().nodename,
    "profiles": len(profiles),
    "missing_reflection_count": len(missing_reflection),
    "missing_soul_invariant_count": len(missing_soul_invariant),
    "stale_reflection_count": len(stale),
    "stale_threshold_days": STALE_DAYS,
    "missing_reflection": missing_reflection[:50],
    "missing_soul_invariant": missing_soul_invariant[:50],
    "stale_reflections": stale[:50],
    "placeholder_reflection_count": len(placeholder_reflection),
    "placeholder_reflections": placeholder_reflection[:50],
    "placeholder_reflections_truncated": len(placeholder_reflection) > 50,
    "status": "PASS"
    if not missing_reflection and not missing_soul_invariant
    else "FAIL",
}
print(json.dumps(summary, indent=2, sort_keys=True))
raise SystemExit(0 if summary["status"] == "PASS" else 2)
