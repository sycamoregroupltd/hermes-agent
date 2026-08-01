#!/usr/bin/env python3
"""Executor-UID-only consumer for an authority-launched real canary.

This file contains no provider imports or lifecycle.  The gate does *not*
execute it.  A root-owned narrow launcher starts the fixed systemd template as
the non-login executor account with only an opaque, one-shot grant id.  The
authority verifies SO_PEERCRED, atomically consumes the armed grant and returns
the canonical payload.  Only then is the private 0700 runtime exec'd.
"""
from __future__ import annotations
import argparse, json, os, pwd, stat, sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
if str(BIN) not in sys.path: sys.path.insert(0, str(BIN))
import v2_grant_authority as grant_authority

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
    cfg=grant_authority._load_install_config(a.install_config)
    # This is intentionally non-mutating and refuses a partial deployment
    # before consuming a grant or importing a provider.
    grant_authority.verify_install(a.install_config)
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
