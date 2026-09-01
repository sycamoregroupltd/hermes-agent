#!/usr/bin/env python3
"""Monitor BOTH governed epics to a world-class standard.

  A. embodiment-memory-convergence  (parent t_0ad65fcb) — research -> ruling -> council -> Frank
  B. refactor-p8-closure            (parent t_22473d55) — remediate -> re-verify -> Frank -> P9

Reports status AND runs cheap quality assertions, because a card reporting `done` is a
claim, not evidence. Flags:
  - done nodes whose artifact is missing or suspiciously thin
  - artifacts lacking verified/claimed tagging where the oracle demanded it
  - the live P8 FAIL conditions (config secrets, SOUL absolute paths, dispatch caps)
  - YAML work-graph edges missing a task_links row (from→to unresolved or unlinked)
  - parent done/archived while the child is still triage
  - parent done/archived with ANY open children (todo/ready/blocked/scheduled/triage/running/review)

Read-only vs boards: never unblock, never specify, never link/unlink.
NEVER auto-unblock H1 t_4b6a207a or H2 t_a6fd38ea (Frank's gates).
Always exits 0; prints ATTENTION / FAIL lines when a human or a repair is needed.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import subprocess
import sys

BOARD = "jarvis-os"
ART = "/home/frank/.hermes/work-graphs/artifacts"
PROFILES = "/home/frank/.hermes/profiles"
KANBAN_DB = "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db"

# Frank gates — report only, never auto-unblock / specify / promote.
NEVER_UNBLOCK = {
    "t_4b6a207a",  # H1 h1-frank-approval
    "t_a6fd38ea",  # H2 h2-frank-ratify
}

EPICS = {
    "CONVERGENCE": ("/tmp/graph_cards.json", "t_0ad65fcb"),
    "P8-CLOSURE": ("/tmp/p8_closure_cards.json", "t_22473d55"),
}
GRAPHS = {
    "CONVERGENCE": "/home/frank/.hermes/work-graphs/embodiment-memory-convergence.yaml",
    "P8-CLOSURE": "/home/frank/.hermes/work-graphs/refactor-p8-closure.yaml",
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

PARENT_DONE = {"done", "completed", "archived"}


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


def yaml_edges(path: str) -> list[tuple[str, str]]:
    import yaml
    try:
        d = yaml.safe_load(open(path)) or {}
    except Exception:
        return []
    edges = d.get("edges") or []
    out: list[tuple[str, str]] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        frm, to = e.get("from"), e.get("to")
        if frm and to:
            out.append((str(frm), str(to)))
    return out


def load_task_links(ids: set[str]) -> set[tuple[str, str]] | None:
    """Read-only task_links rows touching the mapped ids. None on DB error."""
    if not ids or not os.path.exists(KANBAN_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=10)
        try:
            idlist = list(ids)
            q = ",".join("?" * len(idlist))
            rows = con.execute(
                f"SELECT parent_id, child_id FROM task_links "
                f"WHERE parent_id IN ({q}) OR child_id IN ({q})",
                idlist + idlist,
            ).fetchall()
            return {(r[0], r[1]) for r in rows}
        finally:
            con.close()
    except Exception:
        return None


def check_yaml_edges(tasks: dict, attention: list[str]) -> None:
    """Diff each YAML from→to edge against task_links; flag parent_done && child triage.

    Read-only. Never unblocks, specifies, or mutates links. Frank gates stay blocked.
    """
    print("\n=== YAML edges vs task_links ===")
    mapped: dict[str, dict[str, str]] = {}
    all_ids: set[str] = set()
    for label, (cardmap, parent) in EPICS.items():
        ids: dict[str, str] = {}
        if os.path.exists(cardmap):
            try:
                ids = json.load(open(cardmap)) or {}
            except Exception:
                ids = {}
        ids = {str(k): str(v) for k, v in ids.items()}
        ids["epic"] = parent
        mapped[label] = ids
        all_ids.update(ids.values())

    links = load_task_links(all_ids)
    if links is None:
        msg = f"could not read task_links from {KANBAN_DB} — skip edge diff"
        print(f"  FAIL {msg}")
        attention.append(msg)
        return

    missing = 0
    stale_triage = 0
    linked = 0
    unmapped = 0
    for label, (cardmap, parent) in EPICS.items():
        yaml_path = GRAPHS.get(label, "")
        ids = mapped[label]
        edges = yaml_edges(yaml_path) if yaml_path and os.path.exists(yaml_path) else []
        if not edges:
            print(f"  {label}: no YAML edges loaded ({yaml_path or 'missing graph'})")
            continue
        print(f"  {label}: {len(edges)} YAML edges (parent {parent})")
        for frm, to in edges:
            ft, tt = ids.get(frm), ids.get(to)
            if not ft or not tt:
                unmapped += 1
                which = []
                if not ft:
                    which.append(f"from={frm}")
                if not tt:
                    which.append(f"to={to}")
                line = f"{label} unmapped {frm} -> {to} ({', '.join(which)})"
                print(f"  FAIL {line}")
                attention.append(line)
                continue
            if (ft, tt) not in links:
                missing += 1
                line = f"{label} missing_link {frm} -> {to} ({ft} -> {tt})"
                print(f"  FAIL {line}")
                attention.append(line)
            else:
                linked += 1
            pst = (tasks.get(ft) or {}).get("status", "?")
            cst = (tasks.get(tt) or {}).get("status", "?")
            if pst in PARENT_DONE and cst == "triage":
                stale_triage += 1
                gate = ""
                if tt in NEVER_UNBLOCK:
                    gate = " — FRANK GATE, never auto-unblock"
                line = (
                    f"{label} parent_done_child_triage {frm} -> {to} "
                    f"({ft} {pst} -> {tt} {cst}){gate}"
                )
                print(f"  FAIL {line}")
                attention.append(line)
    print(
        f"  summary: linked={linked} missing_link={missing} "
        f"unmapped={unmapped} parent_done_child_triage={stale_triage} "
        f"(read-only; never unblocks H1/H2)"
    )



# Isolation gates — report only, never auto-complete, never unblock.
ISOLATION_GATES = NEVER_UNBLOCK | {
    "t_8b355572",  # RT1
    "t_f2053f84",  # I1
    "t_dc046875",  # I2
    "t_975c4764",  # I3
    "t_6de94730",  # I4
}
OPEN_NOT = PARENT_DONE | {"cancelled"}


def children_of(parent: str) -> list[str] | None:
    """Read-only task_links children. None on DB error."""
    if not os.path.exists(KANBAN_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ?", (parent,)
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()
    except Exception:
        return None


def check_parent_open_children(tasks: dict, attention: list[str]) -> None:
    """Page when epic parent is done/archived but children remain OPEN.

    Tracking-card-complete-early is a real kanban pattern so children can be
    claimed, but status=done still lies to operators. Report only.
    Never complete remaining children. Never unblock Isolation/Frank gates.
    """
    print("\n=== parent-done with open children (report only; never auto-complete) ===")
    for label, (_cardmap, parent) in EPICS.items():
        t = tasks.get(parent) or {}
        pst = t.get("status", "?")
        kids = children_of(parent)
        if kids is None:
            msg = f"{label} {parent} could not read task_links children"
            print(f"  FAIL {msg}")
            attention.append(msg)
            continue
        open_rows = []
        counts: dict[str, int] = {}
        for cid in kids:
            st = (tasks.get(cid) or {}).get("status", "MISSING")
            counts[st] = counts.get(st, 0) + 1
            if st not in OPEN_NOT:
                title = ((tasks.get(cid) or {}).get("title") or "")[:60]
                gate = " ISOLATION/FRANK GATE do-not-unblock" if cid in ISOLATION_GATES else ""
                open_rows.append((cid, st, title, gate))
        print(f"  {label} parent {parent} status={pst} children={len(kids)} open={len(open_rows)} counts={counts}")
        if pst in PARENT_DONE and open_rows:
            summary = (
                f"{label} parent_done_open_children {parent} {pst} "
                f"open={len(open_rows)}/{len(kids)}"
            )
            print(f"  FAIL {summary} (tracking-card-early-complete; operator status lies)")
            attention.append(summary)
            for cid, st, title, gate in open_rows:
                print(f"    open {st:<10} {cid} {title}{gate}")
        elif pst in PARENT_DONE:
            print(f"  ok {label}: parent {pst} and 0 open children")
        else:
            print(f"  skip {label}: parent not done ({pst})")


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

    check_yaml_edges(tasks, attention)

    check_parent_open_children(tasks, attention)

    if attention:
        print("\n=== ATTENTION ===")
        for a in attention:
            print(f"  ! {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
