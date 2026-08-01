#!/usr/bin/env python3
"""Executor-UID-only consumer for an authority-launched real canary.

This file contains no provider imports or lifecycle.  The gate does *not*
execute it.  A root-owned narrow launcher starts the fixed systemd template as
the non-login executor account with only an opaque, one-shot grant id.  The
authority verifies SO_PEERCRED, atomically consumes the armed grant and returns
the canonical payload.  Only then is the private 0700 runtime exec'd.
"""
from __future__ import annotations
import argparse, json, os, stat, sys
from pathlib import Path

class DispatchError(RuntimeError): pass

def _private_runtime(cfg):
    runtime=Path(cfg["runtime_path"]).resolve(); st=runtime.stat()
    if os.geteuid()!=cfg["executor_uid"]:
        raise DispatchError("real executor child must run as configured executor UID")
    if st.st_uid!=0 or stat.S_IMODE(st.st_mode)!=0o755:
        raise DispatchError("private real runtime owner/mode mismatch")
    return runtime

def main(argv=None):
    p=argparse.ArgumentParser(description="authority-launched executor child")
    p.add_argument("--grant-id",required=True)
    p.add_argument("--grant-authority-socket",required=True)
    p.add_argument("--install-config",required=True)
    a=p.parse_args(argv)
    # Do not import the grant authority here: this executor-side trampoline
    # must remain stdlib-only.  The root-digest-pinned runtime performs the
    # complete config/closure proof and authority consume before provider code.
    try: cfg=json.loads(Path(a.install_config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc: raise DispatchError("install config unreadable") from exc
    runtime=_private_runtime(cfg)
    # Do not consume here. The private runtime itself consumes the one-shot
    # grant before it imports the provider. Therefore direct runtime execution
    # without a real authority grant also refuses, rather than inheriting an
    # already-authorized plain argv lifecycle.
    args=[sys.executable,str(runtime),"--grant-id",a.grant_id,"--grant-authority-socket",a.grant_authority_socket,"--install-config",a.install_config]
    os.execv(sys.executable,args)

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"status":"DISPATCH-ERRORED","error_type":type(exc).__name__,"error":str(exc)[:500]})); raise
