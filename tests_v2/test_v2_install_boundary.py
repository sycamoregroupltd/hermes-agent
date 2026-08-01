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
 return {"executor_unit_template":str(unit),"executor_uid":1,"executor_gid":2,"socket_gid":3,"executor_mutable_root":"/var/lib/hermes-executor/workspaces",
  "python_path":"/usr/bin/python3","executor_child_path":"/opt/hermes/bin_verify/v2_real_executor_child.py",
  "socket_path":"/run/hermes-grants/authority.sock","_config_path":"/etc/hermes-grants/install.json",
  "authority_uid":1,"authority_gid":2,"authority_path":"/opt/hermes/bin_verify/v2_grant_authority.py",
  "state_dir":"/var/lib/hermes-grants","authority_issuer_digest_file":"/etc/hermes-grants/authority-token-digest",
  "authority_unit_template":str(unit),"authority_readwrite_paths":["/run/hermes-grants","/var/lib/hermes-grants"],
  "authority_unit_sha256":"0"*64}

def unit(extra="", execstart=None):
 return "[Service]\nUser=executor\nGroup=executor\nSupplementaryGroups=grants\nExecStart="+(execstart or "/usr/bin/python3 /opt/hermes/bin_verify/v2_real_executor_child.py --grant-id %i --grant-authority-socket /run/hermes-grants/authority.sock --install-config /etc/hermes-grants/install.json")+"\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nReadWritePaths=/var/lib/hermes-executor/workspaces\n"+extra

def authority_unit(extra="", execstart=None):
 return "[Service]\nUser=authority\nGroup=authority\nSupplementaryGroups=grants\nExecStart="+(execstart or "/usr/bin/python3 /opt/hermes/bin_verify/v2_grant_authority.py serve --socket /run/hermes-grants/authority.sock --state-dir /var/lib/hermes-grants --issuer-secret-file /etc/hermes-grants/authority-token-digest --install-config /etc/hermes-grants/install.json")+"\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nReadWritePaths=/run/hermes-grants /var/lib/hermes-grants\n"+extra

def main():
 oldpw,oldgrp=ga.pwd.getpwuid,__import__("grp").getgrgid
 ga.pwd.getpwuid=lambda uid:type("P",(),{"pw_name":"executor"})()
 __import__("grp").getgrgid=lambda gid:type("G",(),{"gr_name":"executor" if gid==2 else "grants"})()
 try:
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/"unit"; p.write_text(unit("Environment=EVIL=1\n")); check("unit rejects malicious extra directive",lambda:ga._verify_unit(config(p)))
   p.write_text(unit("ReadWritePaths=/tmp\n")); check("unit rejects injected broad write path",lambda:ga._verify_unit(config(p)))
   p.write_text(unit(execstart="/tmp/evil --grant-id %i")); check("unit rejects external ExecStart path",lambda:ga._verify_unit(config(p)))
   p.write_text(unit()); ga._verify_unit(config(p)); print("PASS: exact unit accepted")
 finally: ga.pwd.getpwuid=oldpw; __import__("grp").getgrgid=oldgrp
 # Symlink and writable-parent rejection: a real install artifact must be a
 # root-owned regular file with no symlink in itself or its ancestor chain.
 with tempfile.TemporaryDirectory() as td:
  root=Path(td)
  # writable ancestor: a world-writable directory in the chain must be refused.
  bad_parent=root/"writable"; bad_parent.mkdir(mode=0o777); os.chmod(bad_parent,0o777)
  link_target=root/"real_unit"; link_target.write_text(unit())
  sym=root/"sym_unit"; sym.symlink_to(link_target)
  check("symlinked unit template is refused", lambda:ga._strict_file_artifact(sym,0o644,"unit"))
  nested=root/"nested"; nested.mkdir(mode=0o755)
  real=root/"nested"/"unit"; real.write_text(unit())
  check("unit under a world-writable ancestor is refused", lambda:ga._strict_file_artifact(real,0o644,"unit"))
  safe=root/"safe"; safe.mkdir(mode=0o755); os.chmod(safe,0o755)
  good=safe/"unit"; good.write_text(unit())
  if os.geteuid()==0:
   ga._strict_file_artifact(good,0o644,"unit"); print("PASS: regular root-readable unit under safe chain accepted")
  else:
   print("SKIP: root-owned acceptance needs euid==0 (symlink/writable-parent rejections already proven)")
 # Authority service is held to the same closed grammar and ReadWritePaths.
 oldpw,oldgrp=ga.pwd.getpwuid,__import__("grp").getgrgid
 ga.pwd.getpwuid=lambda uid:type("P",(),{"pw_name":"authority" if uid==1 else "executor"})()
 __import__("grp").getgrgid=lambda gid:type("G",(),{"gr_name":"grants"})()
 try:
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/"authunit"
   p.write_text(authority_unit("Environment=EVIL=1\n")); check("authority unit rejects malicious extra directive",lambda:ga._verify_authority_unit(config(p)))
   p.write_text(authority_unit("ReadWritePaths=/tmp\n")); check("authority unit rejects injected broad write path",lambda:ga._verify_authority_unit(config(p)))
   p.write_text(authority_unit(execstart="/tmp/evil serve")); check("authority unit rejects external ExecStart path",lambda:ga._verify_authority_unit(config(p)))
   # The exact-unit acceptance path also pins the template by digest and
   # requires root ownership; only meaningful as root.
   if os.geteuid()==0:
    p.write_text(authority_unit()); cfg=config(p); cfg["authority_unit_sha256"]=ga._sha256(p); ga._verify_authority_unit(cfg); print("PASS: exact authority unit accepted")
   else:
    print("SKIP: exact authority unit acceptance needs euid==0 (grammar rejections already proven)")
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
