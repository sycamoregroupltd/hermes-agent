#!/usr/bin/env python3
"""No-side-effect contract tests for task-scoped activation packet verification."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT, "bin_verify", "verify_activation_packet.py")
FAIL = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + ": " + name + (" — " + detail if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def fp(packet):
    clone = {k: v for k, v in packet.items() if k != "packet_fingerprint"}
    return "sha256:" + hashlib.sha256(json.dumps(clone, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def run(packet, task):
    proc = subprocess.run([sys.executable, VERIFIER, "--json", "--activation-packet", packet,
                           "--task-id", task], capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def main():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "source")
        os.makedirs(src)
        watched = os.path.join(src, "watched.txt")
        open(watched, "w").write("reviewed-source\n")
        digest = hashlib.sha256(open(watched, "rb").read()).hexdigest()
        packet = {
            "task_id": "t_beefcafe", "source_root": src, "status": "draft-only",
            "executes_actions": False,
            "worker": {"count_exactly": 1, "provider": "claude-code"},
            "caps": {"one_run_only": True, "max_retries": 0},
            "reviewed_hashes": {"sources": {"watched.txt": digest}, "review_docs": {}, "evidence": {}},
        }
        packet["packet_fingerprint"] = fp(packet)
        path = os.path.join(td, "packet.json")
        open(path, "w").write(json.dumps(packet, sort_keys=True))
        rc, out = run(path, "t_beefcafe")
        check("task-scoped verifier accepts a matching packet and derives source pins", rc == 0 and out["verdict"] == "ACTIVATION-PREREQUISITES-MET", str(out))
        rc, out = run(path, "t_deadbeef")
        check("task-scoped verifier refuses task-id substitution", rc == 4 and out["verdict"] == "FAIL-CLOSED")
        open(watched, "w").write("drifted\n")
        rc, out = run(path, "t_beefcafe")
        check("task-scoped verifier refuses reviewed source drift", rc == 4 and out["verdict"] == "FAIL-CLOSED")
    print("RESULT: " + ("PASS" if not FAIL else "FAIL " + repr(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
