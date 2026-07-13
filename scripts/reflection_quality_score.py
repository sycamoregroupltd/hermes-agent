#!/usr/bin/env python3
"""Score REFLECTION.md files for non-template evidence quality.

Checks:
- No placeholder sentinel (unless superseded by later evidence-backed entry)
- Cites evidence (task/board/path/SOUL/kanban/audit etc.)
- Has strength/weakness or capability/tuning language
- Records one verified action or evidence-backed no-op

Usage: python reflection_quality_score.py [profile|--all]
Exit 0 on clean real reflection, 1 on placeholder/low-quality, 2 on error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/home/frank/.hermes/profiles")
PLACEHOLDER_SENTINEL = "First scheduled cycle should replace this placeholder"

EVIDENCE_KEYWORDS = [
    "task",
    "kanban",
    "board",
    "SOUL.md",
    "audit",
    "path",
    "evidence",
    "t_",
]

ACTION_KEYWORDS = [
    "verified",
    "action",
    "improvement",
    "patch",
    "no-op",
    "routed",
    "created",
    "updated",
]

TUNING_KEYWORDS = ["strength", "weakness", "capability", "tuning", "improve", "score"]


def score_reflection(profile: str) -> dict:
    ref = ROOT / profile / "REFLECTION.md"
    if not ref.exists():
        return {"profile": profile, "score": 0, "reason": "missing", "exit": 1}
    text = ref.read_text(errors="replace").lower()
    sentinel = PLACEHOLDER_SENTINEL.lower()
    if sentinel in text:
        after = text.split(sentinel, 1)[1] if sentinel in text else ""
        if not any(
            kw in after for kw in ["evidence-backed", "t_", "verified", "probe"]
        ):
            return {"profile": profile, "score": 0, "reason": "placeholder", "exit": 1}
    has_evidence = any(kw.lower() in text for kw in EVIDENCE_KEYWORDS)
    has_tuning = any(kw.lower() in text for kw in TUNING_KEYWORDS)
    has_action = any(kw.lower() in text for kw in ACTION_KEYWORDS)
    score = 0
    reasons = []
    if has_evidence:
        score += 1
        reasons.append("cites-evidence")
    if has_tuning:
        score += 1
        reasons.append("has-strength-weakness-tuning")
    if has_action:
        score += 1
        reasons.append("records-verified-action-or-noop")
    exit_code = 0 if score >= 2 else 1
    return {
        "profile": profile,
        "score": score,
        "reasons": reasons,
        "reason": "ok" if exit_code == 0 else "low-quality",
        "exit": exit_code,
    }


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        profiles = sorted(
            p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
        results = [score_reflection(p) for p in profiles]
        print(json.dumps(results, indent=2))
        bad = [r for r in results if r["exit"] != 0]
        sys.exit(0 if not bad else 1)
    elif len(sys.argv) > 1:
        profile = sys.argv[1]
        res = score_reflection(profile)
        print(json.dumps(res, indent=2))
        sys.exit(res["exit"])
    else:
        print("Usage: reflection_quality_score.py <profile> | --all")
        sys.exit(2)


if __name__ == "__main__":
    main()
