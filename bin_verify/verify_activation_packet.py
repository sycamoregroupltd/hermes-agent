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
import re
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


CANONICAL_ARTIFACT_ROOT = os.path.realpath(os.path.join(ROOT, "reservation", "task-artifacts"))
CANONICAL_BOARD_ROOT = "/home/frank/.hermes/kanban/boards/jarvis-os/workspaces"

CANONICAL_BOARD_DB = "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db"
MANDATORY_SOURCE_FILES = (
    "bin_verify/verify_activation_packet.py", "bin_verify/dispatch_gate_v2.py",
    "bin_verify/v2_canary_executor.py", "bin_verify/mint_cmux_receipt.py",
    "bin_verify/issue_cmux_claude_session_binding.py", "bin_verify/cmux_dual_anchor_contract.py",
)
A2_AUTHOR = "jarvis-orchestrator"
EXTERNAL_RESERVATION_TOOL = "/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/t_d7e6c034/bin/seat_reservation.py"

A2_REQUIRED_FIELDS = ("by", "canary_task", "provider", "seat", "scope", "cancellation",
                      "packet_fingerprint", "observed_head")


def parse_a2_dispatch_fields(body, *, require_packet_pins=True):
    """Parse exact, duplicate-free A2 key/value fields without substrings."""
    if not isinstance(body, str) or not body.startswith("APPROVAL A2-DISPATCH"):
        return None
    fields = {}
    for key, value in re.findall(r"(?:^|\s)([a-z_]+)=([^\s]+)", body):
        if key in fields:
            return None
        fields[key] = value
    required = A2_REQUIRED_FIELDS if require_packet_pins else A2_REQUIRED_FIELDS[:6]
    if any(not fields.get(key) for key in required):
        return None
    return fields


def validate_task_a2(packet, board_db=CANONICAL_BOARD_DB):
    """Validate external board A2 binding; packet cannot self-authorize."""
    ap = packet.get("approval_anchor", {})
    try:
        cid = int(ap["comment_id"]); task = packet["task_id"]; head = packet["ownership"]["observed_head"]
        con = sqlite3.connect(f"file:{board_db}?mode=ro", uri=True)
        row = con.execute("select task_id,author,body from task_comments where id=?", (cid,)).fetchone(); con.close()
        if not row or row[0] != task or row[1] != A2_AUTHOR: return False, "A2 comment absent/task/author mismatch"
        fields = parse_a2_dispatch_fields(row[2])
        expected = {"by": A2_AUTHOR, "canary_task": task, "provider": "claude-code",
                    "seat": "interactive-subscription", "scope": "no-op",
                    "packet_fingerprint": packet.get("packet_fingerprint"), "observed_head": head}
        if fields is None or any(fields.get(key) != value for key, value in expected.items()):
            return False, "A2 semantic/fingerprint/head contract mismatch"
        return True, ""
    except (KeyError, ValueError, sqlite3.Error) as exc: return False, str(exc)


def canonical_packet_path(task_id):
    if not isinstance(task_id, str) or not __import__("re").fullmatch(r"t_[0-9a-f]{8}", task_id):
        raise ValueError("invalid task id")
    return os.path.join(CANONICAL_ARTIFACT_ROOT, task_id,
                        f"ACTIVATION-PACKET-{task_id}.json")


def _inside(path, parent):
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent + os.sep)


def _task_artifact_path(task_id, declared):
    """Resolve only the canonical task envelope, never a packet-selected root."""
    allowed = os.path.join(CANONICAL_ARTIFACT_ROOT, task_id)
    path = os.path.realpath(declared if os.path.isabs(declared) else os.path.join(ROOT, declared))
    if not _inside(path, allowed):
        raise ValueError("artifact path escapes canonical task envelope")
    return path


def _reservation_path(task_id, declared):
    expected = os.path.realpath(os.path.join(CANONICAL_BOARD_ROOT, task_id,
                                             "reservation", "seat-reservation.json"))
    if os.path.realpath(declared) != expected:
        raise ValueError("reservation path is not the canonical task reservation")
    return expected


def _reset_results():
    results.clear()


def main(argv=None):
    _reset_results()
    ap = argparse.ArgumentParser(prog="verify_activation_packet")
    ap.add_argument("--activation-packet", default=PACKET)
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    as_json = args.json
    packet_path = os.path.realpath(args.activation_packet)
    p, task_id = {}, None
    try:
        p = json.load(open(packet_path, encoding="utf-8"))
        task_id = p.get("task_id")
        expected = canonical_packet_path(task_id)
        check("packet occupies canonical task-scoped envelope", packet_path == expected,
              f"got={packet_path} expected={expected}")
        check("packet task id present and matches requested task",
              isinstance(task_id, str) and (args.task_id is None or task_id == args.task_id),
              f"packet={task_id!r} requested={args.task_id!r}")
    except (OSError, ValueError) as exc:
        check("activation packet readable and canonical", False, repr(exc))

    fp = p.get("packet_fingerprint")
    check("packet fingerprint stamped and valid", fp is not None and fp == packet_fingerprint(p),
          f"stamped={fp!r}")

    schema = p.get("schema_version")
    if schema == 1:
        # Compatibility for the active draft packet. Schema v1 has limits and
        # artifact_hashes rather than source_root/reviewed_hashes. It parses
        # its trusted task-local artifacts, but deliberately cannot become
        # green: source-review pins and required approvals are absent.
        hashes = p.get("artifact_hashes")
        check("schema-v1 packet has nonempty artifact hashes", isinstance(hashes, dict) and bool(hashes))
        for name, spec in (hashes or {}).items():
            try:
                declared = spec["path"]
                path = (_reservation_path(task_id, declared) if name == "reservation"
                        else _task_artifact_path(task_id, declared))
                got = sha256_file(path) if os.path.isfile(path) else "MISSING"
                check(f"artifact hash: {name}", got == spec.get("sha256"),
                      f"expected={str(spec.get('sha256'))[:16]} current={got[:16]}")
            except (KeyError, ValueError) as exc:
                check(f"artifact hash: {name}", False, str(exc))
        check("schema-v1 cannot satisfy current source-review pin requirement", False,
              "migrate to reviewed schema-v2 task packet before G1 can be green")
        limits = p.get("limits", {})
        check("schema-v1 one Claude worker/no-retry caps", limits.get("worker_count_exactly") == 1
              and limits.get("provider_allowlist") == ["claude-code"]
              and limits.get("one_run_only") is True and limits.get("max_retries") == 0)
    elif schema == 2:
        # Schema v2 is the promotable task-packet format. Source root is fixed
        # to the verifier checkout; it is not a packet-selected authority.
        check("schema-v2 source root binds verifier checkout", p.get("source_root") == ROOT,
              f"source_root={p.get('source_root')!r} verifier_root={ROOT!r}")
        reviewed = p.get("reviewed_hashes", {})
        source_hashes = reviewed.get("sources", {})
        evidence_hashes = reviewed.get("evidence", {})
        check("schema-v2 has nonempty reviewed source hashes", isinstance(source_hashes, dict) and bool(source_hashes))
        check("schema-v2 has nonempty reviewed evidence hashes", isinstance(evidence_hashes, dict) and bool(evidence_hashes))
        external = reviewed.get("external_sources", {})
        external_hash = external.get(EXTERNAL_RESERVATION_TOOL)
        check("schema-v2 pins canonical external reservation writer", isinstance(external_hash, str) and len(external_hash) == 64)
        got_external = sha256_file(EXTERNAL_RESERVATION_TOOL) if os.path.isfile(EXTERNAL_RESERVATION_TOOL) else "MISSING"
        check("external reservation writer hash matches immutable manifest pin", got_external == external_hash,
              f"expected={str(external_hash)[:16]} current={got_external[:16]}")
        check("schema-v2 includes every mandatory executor/gate source", set(MANDATORY_SOURCE_FILES).issubset(source_hashes),
              "missing=" + repr(sorted(set(MANDATORY_SOURCE_FILES) - set(source_hashes))))
        for category in ("sources", "review_docs", "evidence"):
            for rel, want in reviewed.get(category, {}).items():
                try:
                    if category == "sources":
                        path = os.path.realpath(os.path.join(ROOT, rel))
                        if not _inside(path, ROOT):
                            raise ValueError("source path escapes verifier checkout")
                    else:
                        path = _task_artifact_path(task_id, rel)
                    got = sha256_file(path) if os.path.isfile(path) else "MISSING"
                    check(f"{category} hash: {rel}", got == want,
                          f"reviewed={str(want)[:16]} current={got[:16]}")
                except ValueError as exc:
                    check(f"{category} hash: {rel}", False, str(exc))
        ownership = p.get("ownership", {})
        expected_head = ownership.get("observed_head")
        check("schema-v2 pins a reviewed git head", isinstance(expected_head, str) and len(expected_head) >= 12)
        if expected_head:
            proc = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True)
            check("worktree HEAD matches packet pin", proc.returncode == 0 and proc.stdout.strip() == expected_head,
                  f"rc={proc.returncode} head={proc.stdout.strip()}")
            proc = subprocess.run(["git", "-C", ROOT, "status", "--short", "--", *source_hashes.keys()],
                                  capture_output=True, text=True)
            check("reviewed source files clean in git", proc.returncode == 0 and proc.stdout.strip() == "",
                  f"rc={proc.returncode} status={proc.stdout.strip()}")
        worker, caps = p.get("worker", {}), p.get("caps", {})
        check("schema-v2 exactly one Claude worker/no-retry caps", worker.get("count_exactly") == 1
              and worker.get("provider") == "claude-code" and caps.get("one_run_only") is True
              and caps.get("max_retries") == 0)
        a2_ok, a2_detail = validate_task_a2(p)
        check("schema-v2 external exact A2 binds packet fingerprint and head", a2_ok, a2_detail)
    else:
        check("packet schema is supported", False, f"schema_version={schema!r}")

    stop = p.get("stop_switch", {}).get("kill_file")
    if stop:
        check("stop switch absent", not os.path.exists(stop), "stop switch present")
    check("packet declares itself non-authority", p.get("executes_actions") is False
          and str(p.get("status", "")).startswith("draft"))

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
