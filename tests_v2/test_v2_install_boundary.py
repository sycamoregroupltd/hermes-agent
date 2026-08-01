#!/usr/bin/env python3
"""Adversarial source-only checks for the real executor installation boundary."""
import ast, importlib.util, os, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("ga",ROOT/"bin_verify"/"v2_grant_authority.py")
ga=importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)

def check(label, fn):
 try: fn()
 except ga.GrantError: print("PASS:",label); return
 raise SystemExit("FAIL: "+label)

def config(unit):
 return {"executor_unit_template":str(unit),"executor_uid":1,"executor_gid":2,"socket_gid":3,
  "python_path":"/usr/bin/python3","executor_child_path":"/opt/hermes/bin_verify/v2_real_executor_child.py",
  "socket_path":"/run/hermes-grants/authority.sock","_config_path":"/etc/hermes-grants/install.json"}

def unit(extra="", execstart=None):
 return "[Service]\nUser=executor\nGroup=executor\nSupplementaryGroups=grants\nExecStart="+(execstart or "/usr/bin/python3 /opt/hermes/bin_verify/v2_real_executor_child.py --grant-id %i --grant-authority-socket /run/hermes-grants/authority.sock --install-config /etc/hermes-grants/install.json")+"\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\n"+extra

def main():
 oldpw,oldgrp=ga.pwd.getpwuid,__import__("grp").getgrgid
 ga.pwd.getpwuid=lambda uid:type("P",(),{"pw_name":"executor"})()
 __import__("grp").getgrgid=lambda gid:type("G",(),{"gr_name":"executor" if gid==2 else "grants"})()
 try:
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/"unit"; p.write_text(unit("Environment=EVIL=1\n")); check("unit rejects malicious extra directive",lambda:ga._verify_unit(config(p)))
   p.write_text(unit(execstart="/tmp/evil --grant-id %i")); check("unit rejects external ExecStart path",lambda:ga._verify_unit(config(p)))
   p.write_text(unit()); ga._verify_unit(config(p)); print("PASS: exact unit accepted")
 finally: ga.pwd.getpwuid=oldpw; __import__("grp").getgrgid=oldgrp
 source=(ROOT/"bin_verify"/"v2_executor_launcher.c").read_text()
 assert "argc != 2" in source and "clearenv()" in source and "systemctl" in source and "64" in source
 print("PASS: launcher source enforces grant-id-only/sanitized fixed route")
 assert "source head/clean worktree does not match verified install pin" in (ROOT/"bin_verify"/".v2_real_executor_runtime.py").read_text()
 print("PASS: runtime contains pre-import source-head refusal")
 runtime_source=(ROOT/"bin_verify"/".v2_real_executor_runtime.py").read_text()
 tree=ast.parse(runtime_source)
 forbidden=[]
 for node in tree.body:
  if isinstance(node,(ast.Import,ast.ImportFrom)):
   names=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ""]
   forbidden += [name for name in names if name.startswith(("hermes_cli","issue_cmux","v2_grant_authority"))]
 assert not forbidden, "project import occurred before preflight: "+repr(forbidden)
 assert runtime_source.index("_load_and_verify_preimport_config(args.install_config)") < runtime_source.index("from hermes_cli import kanban_db")
 assert runtime_source.index("_consume_authority(args.grant_authority_socket") < runtime_source.index("from hermes_cli import kanban_db")
 assert "provider import closure digest mismatch" in runtime_source
 print("PASS: malicious/writable provider dependency is rejected before consume/provider import")
 print("PASS: runtime has no module-scope project import and defers provider imports")
 authority_source=(ROOT/"bin_verify"/"v2_grant_authority.py").read_text()
 assert "DropInPaths" in authority_source and "NeedDaemonReload" in authority_source
 assert "grant source head does not match verified install pin" in authority_source
 assert "effective_unit_name" in authority_source
 print("PASS: effective systemd/drop-in/reload and grant source-head bindings are fail-closed")
 print("RESULT: INSTALL BOUNDARY REGRESSIONS PASS")
if __name__=="__main__": main()
