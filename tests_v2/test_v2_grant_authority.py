#!/usr/bin/env python3
"""Fail-closed regressions for the grant authority operating-system boundary."""
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("grant_authority", ROOT / "bin_verify" / "v2_grant_authority.py")
ga = importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)

def check(name, ok):
    print(("PASS" if ok else "FAIL") + ": " + name)
    if not ok: raise SystemExit(1)

def main():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        unsafe=root / "unsafe"; unsafe.mkdir(mode=0o777); os.chmod(unsafe,0o775)
        try: ga._safe_state(unsafe); unsafe_refused=False
        except ga.GrantError: unsafe_refused=True
        check("existing group-writable grant state is refused",unsafe_refused)
        fresh=root / "fresh"; ga._safe_state(fresh)
        check("new grant state is exactly 0700",stat.S_IMODE(fresh.stat().st_mode)==0o700)
        uid=os.geteuid(); gid=os.getegid()
        cfg={"authority_uid":uid,"authority_gid":gid,"gate_uid":uid,"gate_gid":gid,"executor_uid":uid,"executor_gid":gid,"socket_gid":gid,"state_dir":str(fresh),"socket_path":str(root / "missing.sock"),"gate_issuer_token_file":str(root / "token"),"authority_issuer_digest_file":str(root / "digest")}
        (root / "token").write_text("x"*64); os.chmod(root / "token",0o600)
        (root / "digest").write_text("digest"); os.chmod(root / "digest",0o600)
        cp=root / "config.json"; cp.write_text(json.dumps(cfg))
        try: ga.verify_install(cp); same_uid_refused=False
        except ga.GrantError as exc: same_uid_refused="distinct OS accounts" in str(exc)
        check("same-UID authority/gate/executor install is refused",same_uid_refused)
        try: ga._peer_uid(object()); peer_refused=False
        except ga.GrantError: peer_refused=True
        check("missing Unix peer credential support is refused",peer_refused)
        distinct=dict(cfg); distinct.update({"gate_uid":uid+100000,"executor_uid":uid+200000})
        dp=root / "distinct.json"; dp.write_text(json.dumps(distinct))
        original=ga._owned_mode
        ga._owned_mode=lambda *args: None
        try: ga.verify_install(dp); missing_socket_refused=False
        except ga.GrantError as exc: missing_socket_refused="socket missing" in str(exc)
        finally: ga._owned_mode=original
        check("install verifier refuses missing authority socket",missing_socket_refused)
    print("RESULT: GRANT AUTHORITY REGRESSIONS PASS")

if __name__ == "__main__": main()
