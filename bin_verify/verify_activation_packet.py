#!/usr/bin/env python3
"""Task-scoped activation-packet verifier.

The verifier is fail-closed and derives its source/evidence pins from the
packet supplied on this invocation. It never executes, schedules, approves,
or dispatches work. A caller may bind verification to one exact task with
--task-id; a mismatched packet is rejected before any positive verdict.
"""

import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKET = os.path.join(ROOT, "ACTIVATION-PACKET-CLAUDE-WORKER.json")

results = []


def check(name, cond, detail=""):
    results.append({"check": name, "pass": bool(cond), "detail": detail if not cond else ""})
    return cond


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def packet_fingerprint(p):
    clone = {k: v for k, v in p.items() if k != "packet_fingerprint"}
    return "sha256:" + hashlib.sha256(
        json.dumps(clone, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _resolve(root, declared):
    """Resolve packet-declared relative paths under the reviewed source root."""
    path = os.path.realpath(declared if os.path.isabs(declared) else os.path.join(root, declared))
    if not path.startswith(os.path.realpath(root) + os.sep) and path != os.path.realpath(root):
        raise ValueError("packet path escapes reviewed source root")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="verify_activation_packet")
    ap.add_argument("--activation-packet", default=PACKET,
                    help="task-scoped packet; all paths/pins are derived from it")
    ap.add_argument("--task-id", default=None,
                    help="must exactly match packet task_id when supplied")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    as_json = args.json
    try:
        packet_path = os.path.realpath(args.activation_packet)
        p = json.load(open(packet_path, encoding="utf-8"))
        task_id = p.get("task_id")
        check("packet task id present and matches requested task",
              isinstance(task_id, str) and (args.task_id is None or task_id == args.task_id),
              f"packet={task_id!r} requested={args.task_id!r}")
        root = os.path.realpath(p.get("source_root", ROOT))
        check("packet source root exists", os.path.isdir(root), root)
    except (OSError, ValueError) as exc:
        check("activation packet readable", False, repr(exc))
        root, p, task_id = ROOT, {}, None

    fp = p.get("packet_fingerprint")
    check("packet fingerprint stamped and valid",
          fp is not None and fp == packet_fingerprint(p),
          f"stamped={fp!r}")

    for category in ("sources", "review_docs", "evidence"):
        for rel, want in p.get("reviewed_hashes", {}).get(category, {}).items():
            try:
                path = _resolve(root, rel)
                got = sha256_file(path) if os.path.isfile(path) else "MISSING"
                check(f"{category} hash: {rel}", got == want,
                      f"reviewed={str(want)[:16]} current={got[:16]}")
            except ValueError as exc:
                check(f"{category} hash: {rel}", False, str(exc))

    ownership = p.get("ownership", {})
    observed_head = ownership.get("observed_head")
    if observed_head:
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        check("worktree HEAD matches packet pin", head == observed_head, f"head={head}")

    source_keys = list(p.get("reviewed_hashes", {}).get("sources", {}).keys())
    if source_keys:
        st = subprocess.run(["git", "-C", root, "status", "--short", "--", *source_keys],
                            capture_output=True, text=True).stdout.strip()
        check("reviewed source files clean in git", st == "", st)

    stop = p.get("stop_switch", {}).get("kill_file")
    if stop:
        check("stop switch absent", not os.path.exists(stop), "stop switch present")

    caps = p.get("caps", {})
    worker = p.get("worker", {})
    check("exactly one Claude-only worker", worker.get("count_exactly") == 1
          and worker.get("provider") == "claude-code")
    check("one-run cap + no-retry", caps.get("one_run_only") is True
          and caps.get("max_retries") == 0)
    check("packet declares itself non-authority", p.get("executes_actions") is False
          and str(p.get("status", "")).startswith("draft"))

    board_db = (p.get("evidence_and_bus_paths", {}) or {}).get("board_db") or p.get("board_db")
    if board_db and task_id:
        try:
            con = sqlite3.connect(f"file:{board_db}?mode=ro", uri=True)
            row = con.execute("select status, assignee from tasks where id=?", (task_id,)).fetchone()
            runs = con.execute("select count(*) from task_runs where task_id=?", (task_id,)).fetchone()[0]
            con.close()
            check("packet task blocked/unassigned with zero runs", row == ("blocked", None) and runs == 0,
                  f"row={row} runs={runs}")
        except sqlite3.Error as exc:
            check("packet task blocked/unassigned with zero runs", False, repr(exc))

    passed = sum(1 for r in results if r["pass"])
    failed = [r for r in results if not r["pass"]]
    verdict = {"verdict": "ACTIVATION-PREREQUISITES-MET" if not failed else "FAIL-CLOSED",
               "passed": passed, "failed": len(failed), "total": len(results),
               "task_id": task_id, "activation_packet": packet_path, "failures": failed}
    if as_json:
        print(json.dumps({"results": results, **verdict}, indent=2, sort_keys=True))
    else:
        for r in results:
            print(("PASS" if r["pass"] else "FAIL") + f": {r['check']}"
                  + (f" — {r['detail']}" if r["detail"] else ""))
        print(f"\nVERDICT: {verdict['verdict']} ({passed}/{len(results)} checks pass)")
    return 0 if not failed else 4


if __name__ == "__main__":
    sys.exit(main())
