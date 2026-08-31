#!/usr/bin/env python3
"""Monitor BOTH governed epics to a world-class standard.

  A. embodiment-memory-convergence  (parent t_0ad65fcb) — research -> ruling -> council -> Frank
  B. refactor-p8-closure            (parent t_22473d55) — remediate -> re-verify -> Frank -> P9

Reports status AND runs cheap quality assertions, because a card reporting `done` is a
claim, not evidence. Flags:
  - done nodes whose artifact is missing or suspiciously thin
  - artifacts lacking verified/claimed tagging where the oracle demanded it
  - the live P8 FAIL conditions (config secrets, SOUL absolute paths, dispatch caps)

Read-only. Always exits 0; prints ATTENTION lines when a human or a repair is needed.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

BOARD = "jarvis-os"
ART = "/home/frank/.hermes/work-graphs/artifacts"
PROFILES = "/home/frank/.hermes/profiles"

EPICS = {
    "CONVERGENCE": ("/tmp/graph_cards.json", "t_0ad65fcb"),
    "P8-CLOSURE": ("/tmp/p8_closure_cards.json", "t_22473d55"),
}
# node -> (artifact filename, min bytes considered substantive)
ARTIFACTS = {
    "r0-incumbent": ("r0-incumbent.md", 4000),
    "r1-memory": ("r1-memory.md", 4000),
    "r2-embodiment": ("r2-embodiment.md", 4000),
    "r3-orchestration": ("r3-orchestration.md", 4000),
    "r4-transport": ("r4-transport.md", 4000),
    "r5-github-survey": ("r5-github-survey.md", 4000),
    "s1-synthesis": ("s1-decision-packet.md", 5000),
    "c1-architecture-council": ("c1-architecture-verdict.md", 2000),
    "c2-safety-council": ("c2-safety-verdict.md", 2000),
    "h1-frank-approval": ("h1-approved-set.md", 200),
    "v1-verify": ("v1-verification.md", 2000),
    "r10-toolset-fitness": ("r10-toolset-fitness.md", 2000),
}
NEEDS_TAGGING = {"r0-incumbent", "r1-memory", "r2-embodiment", "r3-orchestration",
                 "r4-transport", "r5-github-survey"}


def board_tasks() -> dict:
    out = subprocess.run(
        ["bash", "-lc",
         f"env -u HERMES_DELEGATED_CHILD_CONTEXT hermes kanban --board {BOARD} list --json 2>/dev/null"],
        capture_output=True, text=True, timeout=150).stdout
    try:
        d = json.loads(out)
    except Exception:
        return {}
    ts = d.get("tasks", d) if isinstance(d, dict) else d
    return {t.get("id"): t for t in ts}


def live_p8_failures() -> list[str]:
    """Re-measure the P8 FAIL rows that are cheap to check."""
    import yaml
    out = []
    secrets, souls = [], []
    for cp in sorted(glob.glob(os.path.join(PROFILES, "*", "config.yaml"))):
        p = os.path.basename(os.path.dirname(cp))
        try:
            d = yaml.safe_load(open(cp)) or {}
        except Exception:
            continue
        for prov in (d.get("custom_providers") or []):
            k = str(prov.get("api_key") or "")
            if k and not k.startswith("${") and len(k) > 12 and "PLACEHOLDER" not in k.upper():
                secrets.append(p)
        sp = os.path.join(PROFILES, p, "SOUL.md")
        if os.path.exists(sp) and re.search(r"/home/frank/[\w./-]+", open(sp).read()):
            souls.append(p)
    if secrets:
        out.append(f"P8 row3: {len(secrets)} configs still hold secret VALUES: {', '.join(sorted(set(secrets)))}")
    if souls:
        out.append(f"P8 row3: {len(souls)} SOULs still hold absolute paths: {', '.join(sorted(set(souls)))}")
    try:
        # R9 (t_b7bd3ea3, done 2026-08-31): root config.yaml belongs to the STOPPED
        # 'default' identity, not the live dispatch path. The live jarvis profile's
        # OWN config.yaml is authoritative for the caps that actually govern dispatch.
        prof = yaml.safe_load(open("/home/frank/.hermes/profiles/jarvis/config.yaml")) or {}
        k = prof.get("kanban", {})
        caps = (k.get("max_in_progress"), k.get("max_in_progress_per_profile"), k.get("max_spawn"))
        if caps != (12, 2, 4):
            out.append(f"P8 row5: dispatch caps live (jarvis profile) {caps[0]}/{caps[1]}/{caps[2]} vs canonical 12/2/4")
    except Exception:
        pass
    return out


def main() -> int:
    tasks = board_tasks()
    if not tasks:
        print("Could not read board.")
        return 0
    attention: list[str] = []

    for label, (cardmap, parent) in EPICS.items():
        if not os.path.exists(cardmap):
            print(f"\n=== {label}: card map missing ({cardmap}) ===")
            continue
        ids = json.load(open(cardmap))
        print(f"\n=== {label} (parent {parent}) ===")
        counts: dict[str, int] = {}
        for node, tid in ids.items():
            t = tasks.get(tid)
            if not t:
                continue
            st = t.get("status", "?")
            counts[st] = counts.get(st, 0) + 1
            note = ""
            spec = ARTIFACTS.get(node)
            if spec:
                fn, minb = spec
                path = os.path.join(ART, fn)
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    note = f"artifact {size}b"
                    if size < minb:
                        note += " THIN"
                        attention.append(f"{node}: {fn} only {size}b (expected >={minb}) — verify depth")
                    if node in NEEDS_TAGGING:
                        body = open(path, encoding="utf-8", errors="replace").read()
                        if "verified" not in body.lower():
                            attention.append(f"{node}: {fn} has no verified/claimed tagging — oracle demanded it")
                elif st in ("done", "completed"):
                    note = "artifact MISSING"
                    attention.append(f"{node}: reports {st} but {fn} does NOT exist — do not trust this completion")
            print(f"  {st:<9} {node:<24} {t.get('assignee',''):<24} {note}")
        print(f"  counts: {counts}")

    print("\n=== live P8 oracle rows (cheap re-measure) ===")
    fails = live_p8_failures()
    if fails:
        for f in fails:
            print(f"  FAIL {f}")
    else:
        print("  all cheap rows PASS")

    if attention:
        print("\n=== ATTENTION ===")
        for a in attention:
            print(f"  ! {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
