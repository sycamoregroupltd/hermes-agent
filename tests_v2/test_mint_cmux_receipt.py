#!/usr/bin/env python3
"""Deterministic NO-PROVIDER tests for the repaired Mac CMUX receipt mint path.

Nothing here touches the real CMUX socket, the real board, real ssh, or any
provider: cmux is a canned-output stub (faithful to the canary-recorded
0.64.20 contract in evidence/cmux-0.64.20-contract-canary.json), ssh is a stub
that executes the mint's own remote-publish program against a temp "remote"
root, and all fixtures live in temp dirs. The real dispatch gate's
validate_cmux_receipt is imported unmodified to prove minted receipts satisfy
G3b (and that stale/mismatched ones still refuse).

Run: python3 tests_v2/test_mint_cmux_receipt.py
"""

import datetime
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINT = os.path.join(ROOT, "bin_verify", "mint_cmux_receipt.py")
GATE = os.path.join(ROOT, "bin_verify", "dispatch_gate_v2.py")

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + f": {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mint_mod = load_module(MINT, "mint_mod")
gate_mod = load_module(GATE, "gate_mod")

RESERVED_WS = "11111111-AAAA-BBBB-CCCC-000000000001"
RESERVED_SURFACE = "22222222-AAAA-BBBB-CCCC-000000000002"
CALLER_SURFACE = "33333333-AAAA-BBBB-CCCC-000000000003"
FOREIGN_WS = "44444444-AAAA-BBBB-CCCC-000000000004"
FOREIGN_SURFACE = "55555555-AAAA-BBBB-CCCC-000000000005"


def make_reservation(td, ws=RESERVED_WS, surface=RESERVED_SURFACE, version="0.64.20",
                     tamper=False, name="seat-reservation.json"):
    res = {
        "record_kind": "cmux-manual-seat-reservation",
        "schema_version": 1,
        "seat": {"cmux_workspace_id": ws, "cmux_surface_id": surface,
                 "cmux_window_id": "66666666-AAAA-BBBB-CCCC-000000000006",
                 "cmux_daemon_version": version, "provider": "claude-code",
                 "kind": "cmux-interactive-claude-max",
                 "provider_session_uuid": "77777777-aaaa-bbbb-cccc-000000000007"},
    }
    res["reservation_fingerprint"] = mint_mod.reservation_fingerprint(res)
    if tamper:
        res["seat"]["cmux_workspace_id"] = "TAMPERED"
    path = os.path.join(td, name)
    with open(path, "w") as fh:
        fh.write(mint_mod.canonical_dumps(res))
    return path


def make_remote_root(td):
    """Temp 'DGX' root holding the canonical worktree layout + reservation."""
    remote = os.path.join(td, "remote")
    wt = os.path.join(remote, "worktree")
    os.makedirs(os.path.join(wt, "bin_verify"))
    open(os.path.join(wt, "bin_verify", "dispatch_gate_v2.py"), "w").write("# marker\n")
    open(os.path.join(wt, "ACTIVATION-PACKET-CLAUDE-WORKER.json"), "w").write("{}\n")
    res_dir = os.path.join(remote, "reservation-store")
    os.makedirs(res_dir)
    res_path = make_reservation(res_dir)
    return remote, wt, res_path


SSH_STUB = r"""#!/usr/bin/env python3
# ssh stub: argv = [host, prog, *prog_args]. Only host {host!r} resolves.
# `cat <path>` serves files under the remote root; `python3 - ...` executes the
# program received on stdin (the mint's real remote-publish snippet) with paths
# left as-is (fixtures already live under the remote root). Every call is
# logged so tests can assert what was and was not attempted.
import os, subprocess, sys
LOG = {log!r}
ROOT = {root!r}
with open(LOG, "a") as fh:
    fh.write(repr(sys.argv[1:]) + "\n")
if sys.argv[1] != {host!r}:
    sys.stderr.write("ssh: Could not resolve hostname %s\n" % sys.argv[1])
    sys.exit(255)
if sys.argv[2] == "cat":
    try:
        sys.stdout.write(open(sys.argv[3]).read())
    except OSError as exc:
        sys.stderr.write("cat: %r\n" % (exc,))
        sys.exit(1)
    sys.exit(0)
if sys.argv[2] == "python3" and sys.argv[3] == "-":
    p = subprocess.run([sys.executable, "-", *sys.argv[4:]], stdin=sys.stdin,
                       capture_output=True, text=True)
    sys.stdout.write(p.stdout); sys.stderr.write(p.stderr)
    sys.exit(p.returncode)
sys.stderr.write("ssh-stub: unsupported command %r\n" % (sys.argv[2:],))
sys.exit(2)
"""


CMUX_STUB = r"""#!/usr/bin/env python3
# cmux stub, faithful to the canary-recorded 0.64.20 contract. Behavior is
# driven by JSON config at $CMUX_STUB_CONFIG; overrides let each test corrupt
# exactly one command's output.
import json, os, sys
cfg = json.load(open(os.environ["CMUX_STUB_CONFIG"]))
args = sys.argv[1:]
if args and args[0] == "--json":
    args = args[1:]
key = None
if args == ["ping"]: key = "ping"
elif args == ["capabilities"]: key = "capabilities"
elif args == ["version"]: key = "version"
elif args == ["rpc", "system.identify"]: key = "identify"
elif args == ["rpc", "system.tree"]: key = "tree"
elif len(args) == 3 and args[0] == "read-screen" and args[1] == "--surface": key = "read-screen"
if key is None:
    sys.stderr.write("cmux: unknown command %r\n" % (args,)); sys.exit(2)
if key in cfg.get("raw_overrides", {}):
    sys.stdout.write(cfg["raw_overrides"][key]); sys.exit(cfg.get("rc_overrides", {}).get(key, 0))
if key == "version":
    print(cfg["version"]); sys.exit(0)
if key == "read-screen":
    surface = args[2]
    screen = dict(cfg["read_screen_base"])
    screen["surface_id"] = surface
    text = ""
    tty_file = cfg.get("screen_tty_file")
    if tty_file and os.path.exists(tty_file):
        text = open(tty_file).read()
    if cfg.get("suppress_nonce"):
        text = "no nonce here"
    screen["text"] = "  some scrollback\n" + text + "\n  prompt"
    print(json.dumps(screen)); sys.exit(0)
print(json.dumps(cfg[key])); sys.exit(0)
"""


def base_stub_config(wt, res_path, tty_file):
    sock = os.path.join(os.path.dirname(tty_file), "cmux-test.sock")
    return {
        "ping": {"pong": True},
        "capabilities": {"access_mode": "cmuxOnly", "protocol": "cmux-socket",
                         "socket_path": sock, "version": 2,
                         "methods": ["system.identify", "system.tree", "surface.read_text",
                                     "system.ping", "workspace.list"]},
        "version": "0.64.20",
        "identify": {"bundle_identifier": "com.cmuxterm.app",
                     "app_bundle_path": wt,          # any locally-existing path
                     "app_cli_path": res_path,       # any locally-existing path
                     "socket_path": sock, "caller": None,
                     "focused": {"surface_id": FOREIGN_SURFACE, "workspace_id": FOREIGN_WS}},
        "tree": {"active": {"surface_id": FOREIGN_SURFACE, "workspace_id": FOREIGN_WS},
                 "caller": None,
                 "windows": [{"id": "W1", "workspaces": [
                     {"id": FOREIGN_WS, "panes": [{"surfaces": [{"id": FOREIGN_SURFACE, "tty": "3"}]}]},
                     {"id": RESERVED_WS, "panes": [
                         {"surfaces": [{"id": RESERVED_SURFACE, "tty": "ttys012"}]},
                         {"surfaces": [{"id": CALLER_SURFACE, "tty": "ttys018"}]}]},
                 ]}]},
        "read_screen_base": {"workspace_id": RESERVED_WS, "window_id": "W1"},
        "screen_tty_file": tty_file,
    }


class Env:
    """One fully-wired fixture environment (stubs, remote root, tty file)."""

    def __init__(self, td, tag):
        self.dir = os.path.join(td, tag)
        os.makedirs(self.dir)
        self.remote, self.wt, self.res_path = make_remote_root(self.dir)
        self.tty_file = os.path.join(self.dir, "caller-tty.txt")
        open(self.tty_file, "w").write("")
        # local unix socket so C4's "socket exists locally" proof can pass
        import socket as socketlib
        sock_path = os.path.join(self.dir, "cmux-test.sock")
        self.sock = socketlib.socket(socketlib.AF_UNIX)
        self.sock.bind(sock_path)
        self.cfg = base_stub_config(self.wt, self.res_path, self.tty_file)
        self.cfg["capabilities"]["socket_path"] = sock_path
        self.cfg["identify"]["socket_path"] = sock_path
        self.cfg_path = os.path.join(self.dir, "stub-config.json")
        self.ssh_log = os.path.join(self.dir, "ssh-calls.log")
        self.cmux_bin = self._script("cmux-stub", CMUX_STUB)
        self.ssh_bin = self._script("ssh-stub", SSH_STUB.format(
            log=self.ssh_log, root=self.remote, host="dgx"))
        self.flush_cfg()

    def _script(self, name, body):
        path = os.path.join(self.dir, name)
        open(path, "w").write(body)
        os.chmod(path, 0o755)
        return path

    def flush_cfg(self):
        with open(self.cfg_path, "w") as fh:
            json.dump(self.cfg, fh)

    def run(self, *extra, platform="Darwin", fail_publish=None, task="t_beefcafe"):
        env = dict(os.environ, CMUX_STUB_CONFIG=self.cfg_path)
        if platform:
            env["MINT_TEST_PLATFORM"] = platform
        else:
            env.pop("MINT_TEST_PLATFORM", None)
        if fail_publish:
            env["MINT_TEST_FAIL_BEFORE_PUBLISH"] = "1" if fail_publish is True else fail_publish
        else:
            env.pop("MINT_TEST_FAIL_BEFORE_PUBLISH", None)
        argv = [sys.executable, MINT, "--json", "--canary-task", task,
                "--caller-surface", RESERVED_SURFACE,
                "--worktree", self.wt,
                "--reservation-path", self.res_path,
                "--dgx-host", "spark-4be3",
                "--dgx-transport", "dgx",
                "--ssh-cmd", self.ssh_bin,
                "--cmux-bin", self.cmux_bin,
                "--caller-tty", self.tty_file,
                *extra]
        p = subprocess.run(argv, capture_output=True, text=True, env=env)
        try:
            rep = json.loads(p.stdout)
        except ValueError:
            rep = {"verdict": "NO-JSON", "stdout": p.stdout, "stderr": p.stderr}
        return p.returncode, rep

    def receipt_path(self):
        return os.path.join(self.wt, "reservation", "cmux-reservation-receipt.json")

    def receipt_path_for(self, rel):
        return os.path.join(self.wt, rel)

    def published(self):
        return os.path.exists(self.receipt_path())

    def close(self):
        self.sock.close()


def refused_at(rep):
    return (rep.get("refused_at") or {}).get("step", "")


def main():
    with tempfile.TemporaryDirectory() as td:
        n = iter(range(100))

        # -- happy path -------------------------------------------------------
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run()
        check("happy path mints and publishes (rc=0)", rc == 0
              and rep.get("verdict") == "MINTED-AND-PUBLISHED", json.dumps(rep)[:400])
        check("receipt published at canonical worktree path", e.published())
        rec = json.load(open(e.receipt_path())) if e.published() else {}
        check("receipt fingerprint self-consistent",
              rec and rec.get("receipt_fingerprint") == gate_mod.receipt_fingerprint(rec))
        check("receipt binds RESERVED seat ids (not caller, not focused)",
              rec.get("cmux_workspace_id") == RESERVED_WS
              and rec.get("cmux_surface_id") == RESERVED_SURFACE)
        check("receipt records proven caller context = EXACT reserved surface",
              rec.get("caller_context", {}).get("surface_id") == RESERVED_SURFACE
              and rec.get("caller_context", {}).get("proof") == "nonce-read-screen")
        ok, detail = gate_mod.validate_cmux_receipt(e.receipt_path(), e.res_path, "t_beefcafe")
        check("minted receipt passes the UNMODIFIED DGX G3b validator", ok, detail)
        check("nonce was actually round-tripped through the tty file",
              "CMUX-CALLER-PROOF-" in open(e.tty_file).read())
        ev_path = os.path.join(e.wt, "evidence", "mac-cmux-mint-evidence.json")
        check("mint evidence published alongside receipt", os.path.exists(ev_path))
        ev = json.load(open(ev_path)) if os.path.exists(ev_path) else {}
        check("published evidence names the published receipt (sha256 + fingerprint)",
              ev.get("receipt_sha256") == hashlib.sha256(
                  open(e.receipt_path(), "rb").read()).hexdigest()
              and ev.get("receipt_fingerprint") == rec.get("receipt_fingerprint"))
        check("no tmp litter in canonical reservation dir",
              [f for f in os.listdir(os.path.dirname(e.receipt_path()))]
              == ["cmux-reservation-receipt.json"])

        # stale + mismatched receipts against the unmodified gate validator
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=9999)
        ok, detail = gate_mod.validate_cmux_receipt(e.receipt_path(), e.res_path, "t_beefcafe",
                                                    now_utc=future)
        check("minted receipt goes STALE after expiry (gate refuses)", not ok and "STALE" in detail)
        other_res = make_reservation(e.dir, ws="99999999-AAAA-BBBB-CCCC-000000000009",
                                     name="other-reservation.json")
        ok, detail = gate_mod.validate_cmux_receipt(e.receipt_path(), other_res, "t_beefcafe")
        check("minted receipt vs different reservation refuses (mismatch)",
              not ok and "workspace" in detail)
        ok, detail = gate_mod.validate_cmux_receipt(e.receipt_path(), e.res_path, "t_00000009")
        check("minted receipt bound to wrong task refuses", not ok and "bound" in detail)
        e.close()

        # Task-scoped outputs are additive; the legacy default above remains
        # source-compatible and is tested separately.
        e = Env(td, f"e{next(n)}")
        scoped_receipt = "reservation/task-artifacts/t_beefcafe/cmux-reservation-receipt.json"
        scoped_evidence = "reservation/task-artifacts/t_beefcafe/mac-cmux-mint-evidence.json"
        rc, rep = e.run("--receipt-rel", scoped_receipt, "--evidence-rel", scoped_evidence)
        check("task-scoped receipt/evidence outputs mint under exact task directory",
              rc == 0 and os.path.exists(e.receipt_path_for(scoped_receipt))
              and os.path.exists(e.receipt_path_for(scoped_evidence)) and not e.published())
        e.close()

        for rel in ("/tmp/receipt.json", "../reservation/task-artifacts/t_beefcafe/x.json",
                    "reservation/task-artifacts/t_deadbeef/x.json",
                    "reservation/task-artifacts/t_beefcafe/../t_beefcafe/x.json",
                    "reservation/task-artifacts/t_beefcafe//x.json"):
            e = Env(td, f"e{next(n)}")
            rc, rep = e.run("--receipt-rel", rel)
            check(f"unsafe receipt override {rel!r} refuses before publication",
                  rc == 2 and rep.get("verdict") == "NO-JSON" and not e.published())
            e.close()

        e = Env(td, f"e{next(n)}")
        scoped_receipt = "reservation/task-artifacts/t_beefcafe/cmux-reservation-receipt.json"
        scoped_evidence = "reservation/task-artifacts/t_beefcafe/mac-cmux-mint-evidence.json"
        rc, rep = e.run("--receipt-rel", scoped_receipt, "--evidence-rel", scoped_evidence,
                        fail_publish="mac-cmux-mint-evidence")
        check("task-scoped evidence failure publishes neither evidence nor receipt",
              rc == 2 and refused_at(rep) == "C11-publish-evidence"
              and not os.path.exists(e.receipt_path_for(scoped_receipt))
              and not os.path.exists(e.receipt_path_for(scoped_evidence)))
        e.close()

        # -- probe-only -------------------------------------------------------
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--probe-only")
        check("probe-only: all checks green, rc=0, NOTHING published",
              rc == 0 and rep.get("verdict") == "PROBE-ONLY-ALL-CHECKS-GREEN"
              and not e.published())
        c7 = next((step for step in rep.get("steps", []) if step.get("step") == "C7-reservation"), {})
        check("pinned spark-4be3 identity retrieves through fixed dgx transport alias",
              c7.get("ok") is True and "spark-4be3 via dgx" in c7.get("detail", "")
              and "'dgx'" in open(e.ssh_log).read())
        e.close()

        # -- platform fail-closed (the live DGX case) -------------------------
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run(platform=None)  # real platform.system() == Linux here
        check("on non-Mac host refuses at C2 (fail-closed), nothing published",
              rc == 2 and refused_at(rep) == "C2-platform" and not e.published())
        check("non-Mac refusal never touched ssh", not os.path.exists(e.ssh_log))
        e.close()

        # -- cmux unavailable / malformed ------------------------------------
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--cmux-bin", os.path.join(e.dir, "no-such-cmux"))
        check("cmux CLI missing refuses (C3), nothing published",
              rc == 2 and refused_at(rep) == "C3-ping" and not e.published())
        e.close()

        e = Env(td, f"e{next(n)}")
        e.cfg["raw_overrides"] = {"ping": "PONG\n"}
        e.flush_cfg()
        rc, rep = e.run("--probe-only")
        check("installed exact plain PONG ping passes C3 probe-only without publication",
              rc == 0 and rep.get("verdict") == "PROBE-ONLY-ALL-CHECKS-GREEN"
              and not e.published()
              and any(step.get("step") == "C3-ping" and step.get("detail") == "plain-PONG"
                      for step in rep.get("steps", [])))
        e.close()

        for raw in ("pong\n", "PONG!\n", '"PONG"\n', "PONG\nextra\n"):
            e = Env(td, f"e{next(n)}")
            e.cfg["raw_overrides"] = {"ping": raw}
            e.flush_cfg()
            rc, rep = e.run()
            check(f"non-contract ping {raw!r} refuses at C3 without publication",
                  rc == 2 and refused_at(rep) == "C3-ping" and not e.published())
            e.close()

        for key, step, name in [
            ("ping", "C3-ping", "malformed ping output refuses"),
            ("capabilities", "C4-capabilities", "malformed capabilities output refuses"),
            ("identify", "C6-identify", "malformed identify output refuses"),
            ("tree", "C8-tree", "malformed tree output refuses"),
        ]:
            e = Env(td, f"e{next(n)}")
            e.cfg["raw_overrides"] = {key: "]]] not json {{{"}
            e.flush_cfg()
            rc, rep = e.run()
            check(name, rc == 2 and refused_at(rep) == step and not e.published(),
                  json.dumps(rep.get("refused_at", {})))
            e.close()

        e = Env(td, f"e{next(n)}")
        e.cfg["ping"] = {"pong": False}
        e.flush_cfg()
        rc, rep = e.run()
        check("wrong ping payload refuses", rc == 2 and refused_at(rep) == "C3-ping")
        e.close()

        e = Env(td, f"e{next(n)}")
        e.cfg["capabilities"]["methods"].remove("system.tree")
        e.flush_cfg()
        rc, rep = e.run()
        check("capabilities missing required method refuses",
              rc == 2 and refused_at(rep) == "C4-capabilities")
        e.close()

        e = Env(td, f"e{next(n)}")
        e.cfg["capabilities"]["socket_path"] = "/Users/frankspencer/.local/state/cmux/cmux-501.sock"
        e.cfg["identify"]["socket_path"] = e.cfg["capabilities"]["socket_path"]
        e.flush_cfg()
        rc, rep = e.run()
        check("relay case: reported control socket absent locally refuses (C4)",
              rc == 2 and refused_at(rep) == "C4-capabilities" and not e.published())
        e.close()

        e = Env(td, f"e{next(n)}")
        e.cfg["version"] = "0.63.1"
        e.flush_cfg()
        rc, rep = e.run()
        check("daemon version != reservation pin refuses (C5)",
              rc == 2 and refused_at(rep) == "C5-version")
        e.close()

        e = Env(td, f"e{next(n)}")
        e.cfg["identify"]["bundle_identifier"] = "com.example.impostor"
        e.flush_cfg()
        rc, rep = e.run()
        check("wrong bundle identifier refuses (C6)", rc == 2 and refused_at(rep) == "C6-identify")
        e.close()

        # -- wrong host / wrong path (ssh side) -------------------------------
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--dgx-host", "wrong-host")
        check("wrong DGX host refuses (C7), nothing published",
              rc == 2 and refused_at(rep) == "C7-reservation" and not e.published())
        e.close()

        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--dgx-transport", "wrong-alias")
        check("wrong DGX transport alias refuses (C7), no fallback/shadow fetch",
              rc == 2 and refused_at(rep) == "C7-reservation" and not e.published()
              and not os.path.exists(e.ssh_log))
        e.close()

        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--reservation-path", e.res_path + ".nope")
        check("wrong reservation path refuses (C7)", rc == 2 and refused_at(rep) == "C7-reservation")
        e.close()

        # -- reservation integrity -------------------------------------------
        e = Env(td, f"e{next(n)}")
        open(e.res_path, "w").write("{ not json")
        rc, rep = e.run()
        check("unparseable reservation refuses", rc == 2 and refused_at(rep) == "C7-reservation")
        e.close()

        e = Env(td, f"e{next(n)}")
        make_reservation(os.path.dirname(e.res_path), tamper=True)
        rc, rep = e.run()
        check("tampered reservation (fingerprint) refuses",
              rc == 2 and refused_at(rep) == "C7-reservation"
              and "fingerprint" in (rep.get("refused_at") or {}).get("detail", ""))
        e.close()

        # -- reserved seat must be live, exact-ID -----------------------------
        e = Env(td, f"e{next(n)}")
        e.cfg["tree"]["windows"][0]["workspaces"] = [
            w for w in e.cfg["tree"]["windows"][0]["workspaces"] if w["id"] != RESERVED_WS]
        e.flush_cfg()
        rc, rep = e.run()
        check("reserved workspace absent from live tree refuses (C8)",
              rc == 2 and refused_at(rep) == "C8-tree")
        e.close()

        e = Env(td, f"e{next(n)}")
        ws = [w for w in e.cfg["tree"]["windows"][0]["workspaces"] if w["id"] == RESERVED_WS][0]
        ws["panes"] = [p for p in ws["panes"]
                       if p["surfaces"][0]["id"] != RESERVED_SURFACE]
        e.flush_cfg()
        rc, rep = e.run()
        check("reserved surface absent from live tree refuses (C8)",
              rc == 2 and refused_at(rep) == "C8-tree")
        e.close()

        # -- caller context ---------------------------------------------------
        e = Env(td, f"e{next(n)}")
        argv = [sys.executable, MINT, "--json", "--canary-task", "t_beefcafe",
                "--worktree", e.wt, "--reservation-path", e.res_path,
                "--dgx-host", "spark-4be3", "--dgx-transport", "dgx", "--ssh-cmd", e.ssh_bin,
                "--cmux-bin", e.cmux_bin, "--caller-tty", e.tty_file]
        p = subprocess.run(argv, capture_output=True, text=True,
                           env=dict(os.environ, CMUX_STUB_CONFIG=e.cfg_path,
                                    MINT_TEST_PLATFORM="Darwin"))
        rep = json.loads(p.stdout)
        check("no --caller-surface claim refuses (never inferred)",
              p.returncode == 2 and refused_at(rep) == "C9-caller")
        e.close()

        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--caller-surface", "00000000-0000-0000-0000-000000000000")
        check("unknown caller surface refuses (C9)", rc == 2 and refused_at(rep) == "C9-caller")
        e.close()

        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--caller-surface", FOREIGN_SURFACE)
        check("caller surface outside reserved workspace refuses (C9)",
              rc == 2 and refused_at(rep) == "C9-caller"
              and "reserved" in (rep.get("refused_at") or {}).get("detail", ""))
        e.close()

        # t_f0321a11 finding 1: a DIFFERENT surface in the SAME reserved
        # workspace must also refuse — only the exact reserved surface mints.
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--caller-surface", CALLER_SURFACE)
        check("same-workspace OTHER-surface caller refuses (C9, exact reserved surface required)",
              rc == 2 and refused_at(rep) == "C9-caller"
              and "EXACT reserved surface" in (rep.get("refused_at") or {}).get("detail", "")
              and not e.published())
        e.close()

        e = Env(td, f"e{next(n)}")
        e.cfg["suppress_nonce"] = True
        e.flush_cfg()
        rc, rep = e.run()
        check("nonce not visible on claimed surface refuses (caller unproven)",
              rc == 2 and refused_at(rep) == "C9-caller" and not e.published())
        e.close()

        # -- ttl bounds -------------------------------------------------------
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run("--ttl", "20")
        check("ttl below 30s refuses", rc == 2 and refused_at(rep) == "C1-ttl")
        rc, rep = e.run("--ttl", "700")
        check("ttl above 600s refuses", rc == 2 and refused_at(rep) == "C1-ttl")
        e.close()

        # -- atomic publication ----------------------------------------------
        e = Env(td, f"e{next(n)}")
        os.makedirs(os.path.dirname(e.receipt_path()))
        stale_body = '{"receipt_kind": "stale-preexisting"}\n'
        open(e.receipt_path(), "w").write(stale_body)
        rc, rep = e.run(fail_publish=True)
        check("publish failure leaves prior receipt byte-identical (atomic)",
              rc == 2 and open(e.receipt_path()).read() == stale_body)
        check("publish failure leaves no tmp litter",
              os.listdir(os.path.dirname(e.receipt_path())) == ["cmux-reservation-receipt.json"])
        rc, rep = e.run()
        check("subsequent good publish atomically replaces prior receipt",
              rc == 0 and json.load(open(e.receipt_path())).get("receipt_kind")
              == "mac-cmux-reservation-receipt")
        e.close()

        # t_f0321a11 finding 2: evidence is published BEFORE the receipt, so no
        # failure ordering can leave a usable receipt behind a success exit.
        e = Env(td, f"e{next(n)}")
        rc, rep = e.run(fail_publish="mac-cmux-mint-evidence")
        check("evidence-publish failure refuses (C11) with NO receipt and NO evidence",
              rc == 2 and refused_at(rep) == "C11-publish-evidence" and not e.published()
              and not os.path.exists(os.path.join(e.wt, "evidence", "mac-cmux-mint-evidence.json")))
        e.close()

        e = Env(td, f"e{next(n)}")
        rc, rep = e.run(fail_publish="cmux-reservation-receipt")
        ev_path = os.path.join(e.wt, "evidence", "mac-cmux-mint-evidence.json")
        check("receipt-publish failure after evidence refuses (C12), receipt ABSENT",
              rc == 2 and refused_at(rep) == "C12-publish-receipt"
              and rep.get("verdict") == "REFUSE" and not e.published())
        check("evidence published before failed receipt marks it pending (no usable receipt)",
              os.path.exists(ev_path)
              and json.load(open(ev_path)).get("receipt_publish_pending") is True)
        check("failed receipt publish leaves no tmp litter in reservation dir",
              not os.path.isdir(os.path.dirname(e.receipt_path()))
              or os.listdir(os.path.dirname(e.receipt_path())) == [])
        e.close()

        # -- remote-side canonical worktree enforcement -----------------------
        e = Env(td, f"e{next(n)}")
        os.remove(os.path.join(e.wt, "ACTIVATION-PACKET-CLAUDE-WORKER.json"))
        rc, rep = e.run()
        check("remote publish refuses off-canonical worktree (marker missing)",
              rc == 2 and refused_at(rep) == "C11-publish-evidence" and not e.published())
        e.close()

    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: ALL MINT PATH CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
