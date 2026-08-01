#!/usr/bin/env python3
"""Restricted gate child for the real executor.

This module deliberately contains no provider imports and no provider
lifecycle.  It consumes the one-shot authority grant, then execs the private
runtime file.  The runtime is a deployment-owned executable, not a public
Python module: the provisioning verifier requires it to be mode 0700 and
owned by the dedicated executor account.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
import v2_grant_authority as grant_authority

class DispatchError(RuntimeError): pass

def _read_authority(fd, expected, authority_socket):
    try:
        raw = os.read(fd, 513)
    finally:
        os.close(fd)
    if not raw or len(raw) > 512:
        raise DispatchError("child authority grant missing or oversized")
    try:
        grant_id = json.loads(raw).get("grant_id")
    except (ValueError, AttributeError):
        raise DispatchError("child authority grant malformed")
    if not authority_socket:
        raise DispatchError("child authority socket is required")
    try:
        grant_authority.request(authority_socket, {"op":"consume", "grant_id":grant_id,
                                                   "expected":expected})
    except grant_authority.GrantError as exc:
        raise DispatchError("child authority refused: " + str(exc)) from exc

def main(argv=None):
    parser=argparse.ArgumentParser(description="restricted real-executor grant consumer")
    parser.add_argument("--auth-fd",required=True,type=int)
    parser.add_argument("--board-db",required=True); parser.add_argument("--canary-task",required=True)
    parser.add_argument("--workspace-root",required=True); parser.add_argument("--session-binding",required=True)
    parser.add_argument("--cmux-receipt",required=True); parser.add_argument("--reservation-json",required=True)
    parser.add_argument("--binding-issuer",required=True); parser.add_argument("--hermes-home",required=True)
    parser.add_argument("--lease-file",required=True); parser.add_argument("--grant-authority-socket",required=True)
    args=parser.parse_args(argv)
    expected={"task_id":args.canary_task,"board_db":str(Path(args.board_db).resolve()),
              "workspace_root":str(Path(args.workspace_root).resolve()),"session_binding":str(Path(args.session_binding).resolve()),
              "cmux_receipt":str(Path(args.cmux_receipt).resolve()),"reservation_json":str(Path(args.reservation_json).resolve()),
              "binding_issuer":str(Path(args.binding_issuer).resolve()),"hermes_home":str(Path(args.hermes_home).resolve()),
              "lease_file":str(Path(args.lease_file).resolve())}
    _read_authority(args.auth_fd,expected,args.grant_authority_socket)
    runtime=BIN / ".v2_real_executor_runtime.py"
    # The install verifier requires this to be a private executable owned by
    # the executor account. Refuse an editable/public runtime before exec.
    st=runtime.stat()
    if not os.access(runtime,os.X_OK) or (st.st_mode & 0o077):
        raise DispatchError("private real runtime has unsafe permissions")
    forwarded=[]; skip={"--auth-fd","--grant-authority-socket"}; i=0; raw=list(argv if argv is not None else sys.argv[1:])
    while i < len(raw):
        if raw[i] in skip: i += 2
        else: forwarded.append(raw[i]); i += 1
    os.execv(sys.executable,[sys.executable,str(runtime),*forwarded])

if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"status":"DISPATCH-ERRORED","error_type":type(exc).__name__,"error":str(exc)[:500]}))
        raise
