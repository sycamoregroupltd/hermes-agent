#!/usr/bin/env python3
"""mint_cmux_receipt — MAC CONTROL-TERMINAL receipt mint for the v2 G3b gate.

Repaired per jarvis-os/t_21fbb6f6 (review verdict on t_f2df1159). This tool
runs ONLY on the Mac that owns the CMUX control socket, inside a terminal
surface of the reserved seat workspace. It never runs on DGX: every DGX-side
consumer (dispatch_gate_v2.py G3b) validates the published receipt as data and
never calls a CMUX socket.

Verified installed CMUX 0.64.20 command contract (canary-proven 2026-07-31,
recorded in evidence/cmux-0.64.20-contract-canary.json):
  cmux ping                                   -> {"pong": true}
  cmux capabilities                           -> {protocol:"cmux-socket", socket_path, methods[...], ...}
  cmux version                                -> plain text "0.64.20"
  cmux rpc system.identify                    -> {bundle_identifier:"com.cmuxterm.app",
                                                  app_bundle_path, app_cli_path, socket_path,
                                                  caller, focused{...}}
  cmux --json rpc system.tree                 -> full window/workspace/pane/surface tree
  cmux --json read-screen --surface <UUID>    -> {surface_id, workspace_id, text, ...}
Notes proven by canary: per-command "--help" is NOT supported; "--json" is a
GLOBAL flag; "read-screen" with no --surface reads the FOCUSED surface and
"current-workspace" reports the ACTIVE workspace — both are accidental
context and are never used here. Reserved seat and caller are resolved by
EXACT ID lookup in system.tree plus a nonce challenge, never by focus,
never by "first listed surface".

Fail-closed steps (any failure refuses, rc=2, nothing published):
  C1  --ttl within 30..600 s
  C2  platform is macOS (Darwin). On DGX/Linux this refuses immediately.
  C3  cmux CLI resolvable and `ping` returns exactly {"pong": true}
  C4  `capabilities`: protocol "cmux-socket", required methods present, and the
      reported control socket_path EXISTS LOCALLY as a socket — impossible over
      the DGX relay, so this proves we sit on the socket-owning Mac
  C5  `version` equals the reservation's pinned cmux_daemon_version
  C6  `rpc system.identify`: bundle_identifier com.cmuxterm.app, app bundle and
      CLI paths exist locally, socket_path consistent with capabilities
  C7  canonical DGX reservation read THROUGH SSH
      (ssh <dgx-host> cat <canonical seat-reservation.json>): strict JSON,
      record_kind, reservation_fingerprint recomputed and matching
  C8  `--json rpc system.tree`: reserved workspace AND reserved surface found
      by exact ID (surface inside that workspace)
  C9  caller context PROVEN: --caller-surface (explicit claim, required) must
      exist in the tree and must EQUAL the reservation's exact
      cmux_surface_id — any other surface, including another pane/terminal in
      the SAME reserved workspace, refuses (t_f0321a11 finding 1). A random
      nonce written to this process's controlling tty must appear in
      `--json read-screen --surface <claimed>` output whose surface_id echoes
      the claim. No tty, wrong surface, or missing nonce refuses.
  C10 receipt minted: short-lived, bound to --canary-task and to the RESERVED
      workspace/surface ids (receipt_kind unchanged so DGX G3b validates it
      byte-for-byte the same way; caller/control-socket evidence is additive)
  C11 mint EVIDENCE published FIRST, atomically through SSH, to the canonical
      <worktree>/evidence/mac-cmux-mint-evidence.json. The evidence names the
      pending receipt's sha256 and fingerprint. Publishing evidence before the
      receipt makes the pair activation-safe (t_f0321a11 finding 2): a receipt
      can only exist if its evidence is already published, and a failure at
      ANY point leaves no usable receipt behind a success exit.
  C12 atomic receipt publication THROUGH SSH to the canonical DGX path
      <worktree>/reservation/cmux-reservation-receipt.json: a remote python
      snippet verifies the canonical worktree markers, writes a temp file in
      the destination directory, fsyncs, os.replace()s into place, fsyncs the
      directory, and prints the published sha256 which is verified against the
      local content. Partial writes can never appear at the canonical path.
      If this step fails, the mint exits REFUSE (rc=2) and no receipt exists;
      the already-published evidence records the failed attempt as pending.

EXACT COMMAND on the Mac control terminal (a terminal surface inside the
reserved seat workspace; fetch the script first because it lives on DGX):

  scp frank@spark-4be3:/home/frank/.hermes-worktrees/hermes-native-broker-slice/bin_verify/mint_cmux_receipt.py /tmp/mint_cmux_receipt.py
  python3 /tmp/mint_cmux_receipt.py \
      --canary-task t_XXXXXXXX \
      --caller-surface <UUID of the terminal surface you are typing in;
                        this MUST be the reservation's exact cmux_surface_id —
                        minting is only valid from the reserved seat surface>

This session (jarvis-os/t_21fbb6f6 repair seat) runs on DGX spark-4be3, so a
true Mac control terminal cannot be proven from here; the live run recorded in
evidence/mint-live-fail-closed.json shows this tool refusing at C2 exactly as
designed.

Test seams (deterministic no-provider tests only): --cmux-bin, --ssh-cmd,
--dgx-host, --caller-tty, --reservation-path, --worktree, --local-evidence,
--probe-only, and the env vars MINT_TEST_PLATFORM (platform override) and
MINT_TEST_FAIL_BEFORE_PUBLISH (remote-side crash injection: "1" fails every
publish, any other value fails only destinations containing it as a
substring). Production defaults point at the real canonical artifacts.
"""

import argparse
import datetime
import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
import sys
import time

CANONICAL_WORKTREE = "/home/frank/.hermes-worktrees/hermes-native-broker-slice"
CANONICAL_RESERVATION = ("/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/"
                         "t_d7e6c034/reservation/seat-reservation.json")
CANONICAL_DGX_HOST = "spark-4be3"
RECEIPT_REL = "reservation/cmux-reservation-receipt.json"
EVIDENCE_REL = "evidence/mac-cmux-mint-evidence.json"
WORKTREE_MARKERS = ("bin_verify/dispatch_gate_v2.py", "ACTIVATION-PACKET-CLAUDE-WORKER.json")
EXPECTED_BUNDLE_ID = "com.cmuxterm.app"
REQUIRED_METHODS = ("system.identify", "system.tree", "surface.read_text")

RC_OK = 0
RC_REFUSE = 2

# Runs on the DGX side of the ssh pipe: stdin is the exact receipt/evidence
# bytes, argv[1] is the worktree, argv[2] the worktree-relative destination.
# Refuses off the canonical worktree; the rename makes publication atomic.
REMOTE_PUBLISH_SNIPPET = r"""
import hashlib, json, os, sys
worktree, rel = sys.argv[1], sys.argv[2]
dest = os.path.realpath(os.path.join(worktree, rel))
if not dest.startswith(os.path.realpath(worktree) + os.sep):
    print("REMOTE-REFUSE: destination escapes worktree"); sys.exit(3)
for marker in %(markers)s:
    if not os.path.isfile(os.path.join(worktree, marker)):
        print("REMOTE-REFUSE: canonical worktree marker missing: " + marker); sys.exit(3)
data = sys.stdin.buffer.read()
try:
    json.loads(data.decode("utf-8"))
except ValueError:
    print("REMOTE-REFUSE: payload is not valid JSON"); sys.exit(3)
os.makedirs(os.path.dirname(dest), exist_ok=True)
tmp = dest + ".tmp.%%d" %% os.getpid()
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    flag = os.environ.get("MINT_TEST_FAIL_BEFORE_PUBLISH")
    if flag and (flag == "1" or flag in rel):
        raise RuntimeError("injected failure before publish (test only)")
    os.replace(tmp, dest)
    dfd = os.open(os.path.dirname(dest), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
except BaseException as exc:
    if os.path.exists(tmp):
        os.unlink(tmp)
    print("REMOTE-REFUSE: publish failed, canonical path untouched: %%r" %% (exc,))
    sys.exit(3)
print("PUBLISHED sha256:" + hashlib.sha256(data).hexdigest())
""" % {"markers": repr(list(WORKTREE_MARKERS))}


def canonical_dumps(obj):
    """Byte-identical to seat_reservation.py's canonical form."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def reservation_fingerprint(res):
    clone = {k: v for k, v in res.items() if k != "reservation_fingerprint"}
    return "sha256:" + hashlib.sha256(canonical_dumps(clone).encode("utf-8")).hexdigest()


def receipt_fingerprint(rec):
    clone = {k: v for k, v in rec.items() if k != "receipt_fingerprint"}
    return "sha256:" + hashlib.sha256(
        json.dumps(clone, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Refuse(Exception):
    def __init__(self, step, detail):
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


class Mint:
    def __init__(self, args):
        self.args = args
        self.steps = []
        self.evidence = {}

    def ok(self, step, detail=""):
        self.steps.append({"step": step, "ok": True, "detail": detail})

    def run_cmd(self, argv, timeout=15):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"command failed to run: {exc!r}"
        if p.returncode != 0:
            return None, f"rc={p.returncode} stderr={p.stderr.strip()[:300]!r}"
        return p.stdout, ""

    def cmux_json(self, step, *cmd_args, timeout=15):
        argv = [self.args.cmux_bin, *cmd_args]
        out, err = self.run_cmd(argv, timeout=timeout)
        if out is None:
            raise Refuse(step, f"cmux unavailable/errored: {err}")
        try:
            parsed = json.loads(out)
        except ValueError:
            raise Refuse(step, f"malformed (non-JSON) cmux output from {' '.join(cmd_args)!r}: "
                               f"{out.strip()[:200]!r}")
        if not isinstance(parsed, dict):
            raise Refuse(step, f"unexpected cmux output shape from {' '.join(cmd_args)!r}")
        self.evidence[step] = {"argv": argv, "stdout_sha256": hashlib.sha256(out.encode()).hexdigest()}
        return parsed

    # ---- fail-closed steps -------------------------------------------------
    def c1_ttl(self):
        if not 30 <= self.args.ttl <= 600:
            raise Refuse("C1-ttl", f"ttl {self.args.ttl}s outside 30..600 — receipts are short-lived")
        self.ok("C1-ttl", f"{self.args.ttl}s")

    def c2_platform(self):
        system = os.environ.get("MINT_TEST_PLATFORM") or platform.system()
        if system != "Darwin":
            raise Refuse("C2-platform",
                         f"host platform is {system}, not Darwin — this tool runs ONLY on the Mac "
                         "control terminal; DGX validates receipts as data and never mints them")
        self.ok("C2-platform", system)

    def c3_ping(self):
        if shutil.which(self.args.cmux_bin) is None and not os.path.exists(self.args.cmux_bin):
            raise Refuse("C3-ping", f"cmux CLI not found at {self.args.cmux_bin!r} — not a CMUX "
                                    "control terminal")
        pong = self.cmux_json("C3-ping", "ping")
        if pong != {"pong": True}:
            raise Refuse("C3-ping", f"unexpected ping response {pong!r}")
        self.ok("C3-ping", "pong")

    def c4_capabilities(self):
        caps = self.cmux_json("C4-capabilities", "capabilities")
        if caps.get("protocol") != "cmux-socket":
            raise Refuse("C4-capabilities", f"protocol {caps.get('protocol')!r} != 'cmux-socket'")
        methods = caps.get("methods") or []
        missing = [m for m in REQUIRED_METHODS if m not in methods]
        if missing:
            raise Refuse("C4-capabilities", f"required methods missing: {missing}")
        sock = caps.get("socket_path") or ""
        try:
            if not stat.S_ISSOCK(os.stat(sock).st_mode):
                raise Refuse("C4-capabilities",
                             f"reported control socket {sock!r} exists but is not a socket")
        except OSError:
            raise Refuse("C4-capabilities",
                         f"reported control socket {sock!r} does not exist on THIS host — "
                         "we are talking to a relayed socket, not sitting on the control Mac")
        self.socket_path = sock
        self.ok("C4-capabilities", f"protocol=cmux-socket local socket {sock}")

    def c5_version(self):
        out, err = self.run_cmd([self.args.cmux_bin, "version"])
        if out is None:
            raise Refuse("C5-version", f"cmux version unavailable: {err}")
        version = out.strip()
        expected = self.reservation["seat"]["cmux_daemon_version"]
        if version != expected:
            raise Refuse("C5-version", f"installed cmux {version!r} != reserved {expected!r}")
        self.evidence["C5-version"] = {"argv": [self.args.cmux_bin, "version"], "version": version}
        self.ok("C5-version", version)

    def c6_identify(self):
        ident = self.cmux_json("C6-identify", "rpc", "system.identify")
        if ident.get("bundle_identifier") != EXPECTED_BUNDLE_ID:
            raise Refuse("C6-identify",
                         f"bundle_identifier {ident.get('bundle_identifier')!r} != {EXPECTED_BUNDLE_ID!r}")
        for key in ("app_bundle_path", "app_cli_path"):
            path = ident.get(key) or ""
            if not os.path.exists(path):
                raise Refuse("C6-identify", f"{key} {path!r} does not exist locally — not the "
                                            "socket-owning Mac")
        if ident.get("socket_path") != self.socket_path:
            raise Refuse("C6-identify", f"identify socket {ident.get('socket_path')!r} != "
                                        f"capabilities socket {self.socket_path!r}")
        self.ok("C6-identify", EXPECTED_BUNDLE_ID)

    def c7_reservation_via_ssh(self):
        argv = [*self.args.ssh_cmd.split(), self.args.dgx_host, "cat", self.args.reservation_path]
        out, err = self.run_cmd(argv, timeout=30)
        if out is None:
            raise Refuse("C7-reservation", f"cannot read canonical reservation over ssh from "
                                           f"{self.args.dgx_host}:{self.args.reservation_path} — "
                                           f"wrong host or path? {err}")
        try:
            res = json.loads(out)
        except ValueError:
            raise Refuse("C7-reservation", "reservation is not valid JSON — refuse")
        if res.get("record_kind") != "cmux-manual-seat-reservation":
            raise Refuse("C7-reservation", f"wrong record_kind {res.get('record_kind')!r}")
        if res.get("reservation_fingerprint") != reservation_fingerprint(res):
            raise Refuse("C7-reservation", "reservation fingerprint mismatch (tampered/corrupt)")
        try:
            seat = res["seat"]
            self.reserved_ws = seat["cmux_workspace_id"]
            self.reserved_surface = seat["cmux_surface_id"]
        except (KeyError, TypeError):
            raise Refuse("C7-reservation", "reservation missing seat workspace/surface ids")
        self.reservation = res
        self.evidence["C7-reservation"] = {
            "argv": argv, "sha256": hashlib.sha256(out.encode()).hexdigest(),
            "reserved_workspace": self.reserved_ws, "reserved_surface": self.reserved_surface}
        self.ok("C7-reservation", f"ws={self.reserved_ws} surface={self.reserved_surface}")

    def c8_reserved_seat_live(self):
        tree = self.cmux_json("C8-tree", "--json", "rpc", "system.tree", timeout=30)
        self.surface_workspace = {}
        for window in tree.get("windows") or []:
            for ws in window.get("workspaces") or []:
                for pane in ws.get("panes") or []:
                    for surface in pane.get("surfaces") or []:
                        self.surface_workspace[surface.get("id")] = ws.get("id")
        if self.reserved_ws not in set(self.surface_workspace.values()):
            raise Refuse("C8-tree", f"reserved workspace {self.reserved_ws} not live in system.tree")
        if self.surface_workspace.get(self.reserved_surface) != self.reserved_ws:
            raise Refuse("C8-tree", f"reserved surface {self.reserved_surface} not live inside "
                                    f"reserved workspace {self.reserved_ws}")
        self.ok("C8-tree", "reserved workspace+surface live (exact-ID lookup, not focus)")

    def c9_caller_proven(self):
        claimed = self.args.caller_surface
        if not claimed:
            raise Refuse("C9-caller", "--caller-surface is required: caller context must be "
                                      "claimed explicitly and is then verified, never inferred")
        caller_ws = self.surface_workspace.get(claimed)
        if caller_ws is None:
            raise Refuse("C9-caller", f"claimed caller surface {claimed} not present in system.tree")
        if claimed != self.reserved_surface:
            raise Refuse("C9-caller", f"caller surface {claimed} (workspace {caller_ws}) is not the "
                                      f"reserved seat surface {self.reserved_surface} — minting is "
                                      "only valid from the EXACT reserved surface; another "
                                      "pane/terminal in the same reserved workspace also refuses")
        tty_path = self.args.caller_tty
        if tty_path is None:
            try:
                tty_path = os.ttyname(sys.stdin.fileno())
            except OSError:
                raise Refuse("C9-caller", "no controlling tty — cannot prove caller context")
        nonce = "CMUX-CALLER-PROOF-" + secrets.token_hex(16)
        try:
            with open(tty_path, "w") as fh:
                fh.write(f"\n{nonce}\n")
                fh.flush()
        except OSError as exc:
            raise Refuse("C9-caller", f"cannot write nonce to caller tty {tty_path!r}: {exc!r}")
        screen = None
        for attempt in (1, 2):
            screen = self.cmux_json("C9-read-screen", "--json", "read-screen",
                                    "--surface", claimed)
            if nonce in (screen.get("text") or ""):
                break
            if attempt == 1:
                time.sleep(0.5)
        if screen.get("surface_id") != claimed:
            raise Refuse("C9-caller", f"read-screen answered for {screen.get('surface_id')!r}, "
                                      f"not the claimed surface {claimed!r}")
        if nonce not in (screen.get("text") or ""):
            raise Refuse("C9-caller", "nonce not visible on the claimed surface — caller context "
                                      "cannot be proven, refuse")
        self.caller_surface, self.caller_tty = claimed, tty_path
        self.evidence["C9-caller"] = {"nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
                                      "caller_surface": claimed, "caller_tty": tty_path}
        self.ok("C9-caller", f"nonce round-trip proven on {claimed}")

    def c10_mint(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        rec = {
            "receipt_kind": "mac-cmux-reservation-receipt",
            "schema_version": 2,
            "minted_on": "mac-cmux-control-socket",
            "minted_at_utc": now.isoformat().replace("+00:00", "Z"),
            "expires_at_utc": (now + datetime.timedelta(seconds=self.args.ttl))
                .isoformat().replace("+00:00", "Z"),
            "canary_task": self.args.canary_task,
            "cmux_workspace_id": self.reserved_ws,
            "cmux_surface_id": self.reserved_surface,
            "caller_context": {
                "surface_id": self.caller_surface,
                "workspace_id": self.reserved_ws,
                "tty": self.caller_tty,
                "proof": "nonce-read-screen",
                "nonce_sha256": self.evidence["C9-caller"]["nonce_sha256"],
            },
            "control_socket": {
                "socket_path": self.socket_path,
                "bundle_identifier": EXPECTED_BUNDLE_ID,
                "cmux_daemon_version": self.reservation["seat"]["cmux_daemon_version"],
            },
        }
        rec["receipt_fingerprint"] = receipt_fingerprint(rec)
        self.receipt = rec
        self.ok("C10-mint", f"receipt for {self.args.canary_task}, expires {rec['expires_at_utc']}")

    def _publish(self, rel, payload_bytes, step):
        # `ssh <host> python3 - <worktree> <rel> <payload-b64>`: stdin carries
        # the publish program, the payload travels base64-encoded in argv.
        import base64
        argv = [*self.args.ssh_cmd.split(), self.args.dgx_host, "python3", "-",
                self.args.worktree, rel, base64.b64encode(payload_bytes).decode()]
        try:
            p = subprocess.run(argv, input=REMOTE_PUBLISH_SNIPPET, capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refuse(step, f"ssh publish failed to run: {exc!r}")
        out = (p.stdout or "").strip()
        if p.returncode != 0 or not out.startswith("PUBLISHED sha256:"):
            raise Refuse(step, f"remote publish refused (canonical path untouched): rc={p.returncode} "
                               f"out={out[:300]!r} err={(p.stderr or '').strip()[:200]!r}")
        remote_sha = out.split("PUBLISHED sha256:", 1)[1].strip()
        local_sha = hashlib.sha256(payload_bytes).hexdigest()
        if remote_sha != local_sha:
            raise Refuse(step, f"published sha {remote_sha} != local {local_sha} — round-trip "
                               "verification failed")
        self.ok(step, f"{self.args.dgx_host}:{os.path.join(self.args.worktree, rel)} sha256:{local_sha}")

    def c11_publish_evidence(self):
        """Evidence goes FIRST: a receipt may only exist with evidence already
        published, so no failure ordering can leave a usable receipt behind a
        success exit (t_f0321a11 finding 2)."""
        self.receipt_payload = (json.dumps(self.receipt, indent=2, sort_keys=True) + "\n").encode()
        pre = self.report("EVIDENCE-BEFORE-RECEIPT")
        pre["receipt_sha256"] = hashlib.sha256(self.receipt_payload).hexdigest()
        pre["receipt_fingerprint"] = self.receipt["receipt_fingerprint"]
        pre["receipt_publish_pending"] = True
        payload = (json.dumps(pre, indent=2, sort_keys=True) + "\n").encode()
        self._publish(EVIDENCE_REL, payload, "C11-publish-evidence")

    def c12_publish_receipt(self):
        self._publish(RECEIPT_REL, self.receipt_payload, "C12-publish-receipt")

    def report(self, verdict, refusal=None):
        rep = {"tool": "mint_cmux_receipt", "verdict": verdict,
               "canary_task": self.args.canary_task, "probe_only": self.args.probe_only,
               "steps": self.steps, "evidence": self.evidence}
        if refusal is not None:
            rep["refused_at"] = {"step": refusal.step, "detail": refusal.detail}
        return rep


# The remote payload travels base64-encoded in argv (stdin carries the program
# itself), so the snippet decodes argv[3] instead of reading stdin.
REMOTE_PUBLISH_SNIPPET = REMOTE_PUBLISH_SNIPPET.replace(
    'data = sys.stdin.buffer.read()',
    'import base64\ndata = base64.b64decode(sys.argv[3])')


def main(argv):
    ap = argparse.ArgumentParser(prog="mint_cmux_receipt",
                                 description="Mac control-terminal CMUX receipt mint (fail-closed)")
    ap.add_argument("--canary-task", required=True)
    ap.add_argument("--caller-surface", default=None,
                    help="UUID of the terminal surface this command is typed in (verified by nonce)")
    ap.add_argument("--ttl", type=int, default=300)
    ap.add_argument("--worktree", default=CANONICAL_WORKTREE)
    ap.add_argument("--reservation-path", default=CANONICAL_RESERVATION)
    ap.add_argument("--dgx-host", default=CANONICAL_DGX_HOST)
    ap.add_argument("--ssh-cmd", default="ssh")
    ap.add_argument("--cmux-bin", default="cmux")
    ap.add_argument("--caller-tty", default=None)
    ap.add_argument("--probe-only", action="store_true",
                    help="run every check but publish nothing")
    ap.add_argument("--local-evidence", default=None,
                    help="also write the step report to this LOCAL path (works even on refusal)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    m = Mint(args)
    refusal = None
    try:
        m.c1_ttl()
        m.c2_platform()
        m.c3_ping()
        m.c4_capabilities()
        m.c7_reservation_via_ssh()   # needs ssh before version pin comparison
        m.c5_version()
        m.c6_identify()
        m.c8_reserved_seat_live()
        m.c9_caller_proven()
        if not args.probe_only:
            m.c10_mint()
            m.c11_publish_evidence()
            m.c12_publish_receipt()
        verdict = "PROBE-ONLY-ALL-CHECKS-GREEN" if args.probe_only else "MINTED-AND-PUBLISHED"
        rc = RC_OK
    except Refuse as exc:
        refusal = exc
        m.steps.append({"step": exc.step, "ok": False, "detail": exc.detail})
        verdict, rc = "REFUSE", RC_REFUSE

    rep = m.report(verdict, refusal)
    if args.local_evidence:
        with open(args.local_evidence, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, sort_keys=True)
            fh.write("\n")
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        for s in rep["steps"]:
            print(("PASS" if s["ok"] else "REFUSE") + f": {s['step']}"
                  + (f" — {s['detail']}" if s["detail"] else ""))
        print(f"\nVERDICT: {rep['verdict']}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
