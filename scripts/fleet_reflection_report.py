#!/usr/bin/env python3
"""Build a global report from all Hermes profile REFLECTION.md files.

Safe read-only aggregation except for writing /home/frank/uaa-rules/FLEET-REFLECTION-REPORT.md.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/frank/.hermes")
PROFILES = ROOT / "profiles"
OUT = Path("/home/frank/uaa-rules/FLEET-REFLECTION-REPORT.md")
MAX_LOG_LINES = int(os.environ.get("REFLECTION_REPORT_LOG_LINES", "8"))
NOW = datetime.now(timezone.utc)


def extract_section(text: str, heading: str, max_lines: int = 12) -> list[str]:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == heading.lower():
            start = i + 1
            break
    if start is None:
        return []
    out = []
    for line in lines[start:]:
        if line.startswith("## ") and out:
            break
        if line.strip():
            out.append(line.rstrip())
        if len(out) >= max_lines:
            break
    return out


rows = []
for prof in sorted(
    p for p in PROFILES.iterdir() if p.is_dir() and not p.name.startswith(".")
):
    ref = prof / "REFLECTION.md"
    soul = prof / "SOUL.md"
    if not ref.exists():
        rows.append(
            {
                "profile": prof.name,
                "status": "MISSING",
                "age_days": None,
                "purpose": [],
                "log": [],
                "is_placeholder": False,
            }
        )
        continue
    text = ref.read_text(errors="replace")
    age_days = (time.time() - ref.stat().st_mtime) / 86400
    purpose = extract_section(text, "## Purpose", 6)
    log = extract_section(text, "## Reflection log", MAX_LOG_LINES)
    status = "OK" if text.strip() else "EMPTY"
    sentinel = "First scheduled cycle should replace this placeholder"
    is_placeholder = sentinel in text
    if is_placeholder:
        after = text.split(sentinel, 1)[1] if sentinel in text else ""
        if any(kw in after for kw in ["Evidence-backed", "t_", "verified", "probe"]):
            is_placeholder = False
    rows.append(
        {
            "profile": prof.name,
            "status": status,
            "age_days": age_days,
            "purpose": purpose,
            "log": log,
            "is_placeholder": is_placeholder,
        }
    )

missing = [r for r in rows if r["status"] != "OK"]
stale = [r for r in rows if r["age_days"] is not None and r["age_days"] > 7]
placeholders = [r for r in rows if r.get("is_placeholder")]

parts = []
parts.append("# FLEET REFLECTION REPORT")
parts.append("")
parts.append(f"Generated: {NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}")
parts.append(f"Host: {os.uname().nodename}")
parts.append(f"Profiles scanned: {len(rows)}")
parts.append(f"Missing/empty reflection files: {len(missing)}")
parts.append(f"Stale reflection files over 7 days: {len(stale)}")
parts.append(f"Placeholder reflection files (maturity backlog): {len(placeholders)}")
parts.append("")
parts.append("## How to read this")
parts.append("")
parts.append(
    "This is the global reflection ledger for the Hermes fleet. Each profile owns its own `REFLECTION.md`; this report aggregates the latest purpose and reflection log so Frank and the fleet can see what agents are meditating on, improving, and routing."
)
parts.append("")
parts.append("## Attention needed")
parts.append("")
if not missing and not stale and not placeholders:
    parts.append(
        "- None: all profiles currently have reflection artifacts and none are stale by the 7-day threshold."
    )
else:
    for r in missing[:50]:
        parts.append(f"- {r['profile']}: {r['status']}")
    for r in stale[:50]:
        parts.append(f"- {r['profile']}: stale {r['age_days']:.1f} days")
    for r in placeholders[:50]:
        parts.append(
            f"- {r['profile']}: PLACEHOLDER (maturity backlog - replace with evidence-backed reflection)"
        )
parts.append("")
parts.append("## Profile reflections")
parts.append("")
for r in rows:
    parts.append(f"### {r['profile']}")
    age = "unknown" if r["age_days"] is None else f"{r['age_days']:.1f}d old"
    parts.append(f"- Status: {r['status']}; artifact age: {age}")
    if r["purpose"]:
        parts.append("- Purpose:")
        for line in r["purpose"][:6]:
            parts.append(f"  {line}")
    if r["log"]:
        parts.append("- Latest reflection log:")
        for line in r["log"][:MAX_LOG_LINES]:
            parts.append(f"  {line}")
    else:
        parts.append("- Latest reflection log: none found")
    parts.append("")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(parts).rstrip() + "\n")
print(
    f"REFLECTION_REPORT_PASS profiles={len(rows)} missing={len(missing)} stale={len(stale)} placeholders={len(placeholders)} path={OUT}"
)
