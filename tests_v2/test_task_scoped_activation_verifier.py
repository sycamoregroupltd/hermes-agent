#!/usr/bin/env python3
"""No-side-effect trust-boundary tests for task-scoped activation verification."""
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(ROOT, "bin_verify", "verify_activation_packet.py")
ARTIFACT_ROOT = os.path.join(ROOT, "reservation", "task-artifacts")
ACTIVE_V1 = "/home/frank/.hermes-worktrees/hermes-native-broker-slice/reservation/task-artifacts/t_8fa09a42/ACTIVATION-PACKET-t_8fa09a42.json"
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


def failures(out):
    return "\n".join(r["check"] + " " + r.get("detail", "") for r in out.get("failures", []))


def main():
    task = "t_beefcafe"
    task_dir = os.path.join(ARTIFACT_ROOT, task)
    os.makedirs(task_dir, exist_ok=True)
    evidence = os.path.join(task_dir, "test-evidence.json")
    packet_path = os.path.join(task_dir, "ACTIVATION-PACKET-t_beefcafe.json")
    try:
        open(evidence, "w").write("evidence\n")
        source_rel = "bin_verify/cmux_dual_anchor_contract.py"
        source_sha = hashlib.sha256(open(os.path.join(ROOT, source_rel), "rb").read()).hexdigest()
        evidence_rel = "reservation/task-artifacts/t_beefcafe/test-evidence.json"
        evidence_sha = hashlib.sha256(open(evidence, "rb").read()).hexdigest()
        external_path = "/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/t_d7e6c034/bin/seat_reservation.py"
        external_sha = hashlib.sha256(open(external_path, "rb").read()).hexdigest()
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        packet = {
            "schema_version": 2, "task_id": task, "status": "draft-preflight-only",
            "executes_actions": False,
            "worker": {"count_exactly": 1, "provider": "claude-code"},
            "caps": {"one_run_only": True, "max_retries": 0},
            "ownership": {"observed_head": head},
            "reviewed_hashes": {"sources": {source_rel: source_sha}, "review_docs": {},
                                "evidence": {evidence_rel: evidence_sha},
                                "external_sources": {external_path: external_sha}},
        }
        packet["packet_fingerprint"] = fp(packet)
        open(packet_path, "w").write(json.dumps(packet, sort_keys=True))
        rc, out = run(packet_path, task)
        check("canonical schema-v2 packet without external A2/complete manifest fails closed",
              rc == 4 and "mandatory executor/gate source" in failures(out), failures(out))

        import importlib.util, sqlite3
        spec = importlib.util.spec_from_file_location("verifier_mod", VERIFIER)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        db = os.path.join(task_dir, "a2.db")
        con = sqlite3.connect(db); con.execute("create table task_comments (id integer primary key, task_id text, author text, body text)")
        packet["ownership"] = {"observed_head": head}; packet["packet_fingerprint"] = fp(packet)
        body = (f"APPROVAL A2-DISPATCH by=jarvis-orchestrator canary_task={task} provider=claude-code seat=interactive-subscription scope=no-op cancellation=stop "
                f"packet_fingerprint={packet['packet_fingerprint']} observed_head={head}")
        con.execute("insert into task_comments values (1,?,?,?)", (task, "jarvis-orchestrator", body)); con.commit(); con.close()
        packet["approval_anchor"] = {"comment_id": 1}
        packet["packet_fingerprint"] = fp(packet)
        # packet fingerprint changes, so update external approval body with final pin.
        con = sqlite3.connect(db); con.execute("update task_comments set body=? where id=1", (body.replace(body.split("packet_fingerprint=")[1].split()[0], packet["packet_fingerprint"]),)); con.commit(); con.close()
        ok, detail = mod.validate_task_a2(packet, db)
        check("external A2 validates exact task/author/semantic fingerprint/head", ok, detail)
        tampered = dict(packet); tampered["packet_fingerprint"] = "sha256:forged"
        ok, _ = mod.validate_task_a2(tampered, db)
        check("packet replacement after valid A2 is refused", not ok)
        con = sqlite3.connect(db); con.execute("update task_comments set body=? where id=1", (body.replace("APPROVAL A2-DISPATCH", "APPROVAL A2-DISPATCH by=wrong"),)); con.commit(); con.close()
        ok, _ = mod.validate_task_a2(packet, db)
        check("missing required A2 by=jarvis-orchestrator marker refuses", not ok)
        con = sqlite3.connect(db); con.execute("update task_comments set body=? where id=1", (body,)); con.commit(); con.close()

        bad_external = dict(packet); bad_external["reviewed_hashes"] = dict(packet["reviewed_hashes"])
        bad_external["reviewed_hashes"]["external_sources"] = {external_path: "0" * 64}; bad_external["packet_fingerprint"] = fp(bad_external)
        open(packet_path, "w").write(json.dumps(bad_external))
        rc, out = run(packet_path, task)
        check("altered external reservation writer pin refuses", rc == 4 and "external reservation writer hash" in failures(out), failures(out))
        open(packet_path, "w").write(json.dumps(packet, sort_keys=True))

        forged = dict(packet); forged["source_root"] = "/tmp/forged-root"; forged["packet_fingerprint"] = fp(forged)
        forged_path = "/tmp/forged-activation-packet.json"
        open(forged_path, "w").write(json.dumps(forged))
        rc, out = run(forged_path, task)
        check("self-fingerprinted off-root packet refuses", rc == 4 and out["verdict"] == "FAIL-CLOSED"
              and "canonical task-scoped envelope" in failures(out), failures(out))
        os.remove(forged_path)

        swapped = dict(packet); swapped["task_id"] = "t_deadbeef"; swapped["packet_fingerprint"] = fp(swapped)
        open(packet_path, "w").write(json.dumps(swapped))
        rc, out = run(packet_path, task)
        check("task swap refuses", rc == 4 and "matches requested task" in failures(out), failures(out))
        open(packet_path, "w").write(json.dumps(packet, sort_keys=True))

        open(os.path.join(ROOT, source_rel), "rb").read()  # source remains read-only
        bad = dict(packet); bad["reviewed_hashes"] = dict(packet["reviewed_hashes"])
        bad["reviewed_hashes"]["evidence"] = {"../../escape": evidence_sha}; bad["packet_fingerprint"] = fp(bad)
        open(packet_path, "w").write(json.dumps(bad))
        rc, out = run(packet_path, task)
        check("evidence path escape refuses", rc == 4 and "escapes canonical task envelope" in failures(out), failures(out))
        open(packet_path, "w").write(json.dumps(packet, sort_keys=True))

        # Read the actual active schema-v1 draft as-is. In this isolated
        # checkout it must fail closed, but recognition of the v1 source-pin
        # migration gate proves it was parsed rather than treated as unknown.
        rc, out = run(ACTIVE_V1, "t_8fa09a42")
        check("active schema-v1 packet is parsed and fails only through explicit migration gates",
              rc == 4 and "schema-v1 cannot satisfy current source-review pin requirement" in failures(out), failures(out))
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
    print("RESULT: " + ("PASS" if not FAIL else "FAIL " + repr(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
