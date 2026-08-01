#!/usr/bin/env python3
"""Verifier-owned one-shot grant authority for the real v2 executor.

It is a separate process from both gate and Claude child. The child receives
only an opaque grant id; it never receives an issuer/signing key. The authority
records and atomically consumes the grant before provider import.

Production must run serve under a dedicated hermes-grant-authority account.
Its Unix socket is writable only by the governed gate account and the issuer
token file only by that account. A same-UID process able to read the issuer
token is not a security boundary; this module does not claim otherwise.
"""
from __future__ import annotations
import argparse, hashlib, json, os, pwd, secrets, shlex, socket, stat, time
from pathlib import Path
MAX=8192
class GrantError(RuntimeError): pass
def _canon(v): return json.dumps(v,sort_keys=True,separators=(",",":"))
def _recv(c):
 raw=c.recv(MAX+1)
 if not raw or len(raw)>MAX: raise GrantError("grant request missing or oversized")
 try: return json.loads(raw.decode())
 except ValueError as e: raise GrantError("grant request malformed") from e
def _send(c,v): c.sendall(_canon(v).encode())
def _mode(path): return stat.S_IMODE(path.stat().st_mode)
def _safe_state(d, *, expected_uid=None):
 """Create state as 0700 or reject it; never silently repair a pre-existing
 insecure directory, because that directory is an authority boundary."""
 try:
  d.mkdir(parents=False, mode=0o700)
  os.chmod(d, 0o700)  # mkdir is subject to umask; we own a newly-created path.
 except FileExistsError:
  pass
 st=d.stat()
 if not stat.S_ISDIR(st.st_mode) or _mode(d)!=0o700: raise GrantError("grant state directory must be mode 0700")
 if expected_uid is not None and st.st_uid != expected_uid: raise GrantError("grant state directory owner mismatch")

def _peer_uid(conn):
 """Refuse if the platform cannot attest the Unix peer identity."""
 if not hasattr(socket,"SO_PEERCRED"): raise GrantError("Unix peer credential verification unavailable")
 try:
  import struct
  return struct.unpack("3i",conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[0]
 except (AttributeError, OSError) as exc: raise GrantError("Unix peer credential verification failed") from exc

def _load_install_config(path):
 try: cfg=json.loads(Path(path).read_text())
 except (OSError,ValueError) as exc: raise GrantError("grant install configuration unreadable") from exc
 required={"authority_uid","authority_gid","gate_uid","gate_gid","executor_uid","executor_gid","socket_gid","state_dir","socket_path","gate_issuer_token_file","authority_issuer_digest_file","runtime_path","authority_path","executor_child_path","executor_launcher","executor_unit_template","python_path","source_root","source_head","provider_import_closure","runtime_sha256","authority_sha256","executor_child_sha256","launcher_sha256","unit_sha256","effective_unit_name"}
 if set(cfg)!=required: raise GrantError("grant install configuration fields are not exact")
 nums={"authority_uid","authority_gid","gate_uid","gate_gid","executor_uid","executor_gid","socket_gid"}
 if not all(isinstance(cfg[k],int) for k in nums): raise GrantError("grant install numeric identities invalid")
 if not all(isinstance(cfg[k],str) and cfg[k] for k in required-nums): raise GrantError("grant install paths invalid")
 if len({cfg["authority_uid"],cfg["gate_uid"],cfg["executor_uid"]})!=3: raise GrantError("authority, gate and executor must be distinct OS accounts")
 if not __import__("re").fullmatch(r"[0-9a-f]{64}",cfg["source_head"]): raise GrantError("source head must be canonical 64-hex")
 if not isinstance(cfg["provider_import_closure"],dict) or not cfg["provider_import_closure"]: raise GrantError("provider import closure missing")
 if cfg["effective_unit_name"] != "hermes-real-executor@.service": raise GrantError("effective unit name is not canonical")
 return cfg

def _sha256(path):
 h=hashlib.sha256()
 with open(path,"rb") as f:
  for block in iter(lambda:f.read(65536),b""): h.update(block)
 return h.hexdigest()

def _root_regular_digest(path, mode, digest, label):
 path=Path(path)
 try: st=path.stat()
 except OSError as exc: raise GrantError(f"{label} missing") from exc
 if st.st_uid!=0 or not stat.S_ISREG(st.st_mode) or _mode(path)!=mode: raise GrantError(f"{label} owner/mode mismatch")
 if not isinstance(digest,str) or len(digest)!=64 or not secrets.compare_digest(_sha256(path),digest): raise GrantError(f"{label} digest mismatch")

def _verify_unit(cfg):
 """Parse the installed unit as a closed grammar, never by substring."""
 unit=Path(cfg["executor_unit_template"])
 try: lines=unit.read_text(encoding="utf-8").splitlines()
 except OSError as exc: raise GrantError("executor unit template missing") from exc
 section=None; values={}; allowed={"User","Group","SupplementaryGroups","ExecStart","NoNewPrivileges","PrivateTmp","ProtectSystem"}
 for raw in lines:
  line=raw.strip()
  if not line or line.startswith(("#",";")): continue
  if line.startswith("[") and line.endswith("]"): section=line[1:-1]; continue
  if section!="Service" or "=" not in line: raise GrantError("executor unit has non-Service or malformed directive")
  key,value=line.split("=",1)
  if key not in allowed or key in values: raise GrantError("executor unit has unsafe or duplicate directive")
  values[key]=value
 executor=pwd.getpwuid(cfg["executor_uid"]).pw_name
 group=__import__("grp").getgrgid(cfg["executor_gid"]).gr_name
 socket_group=__import__("grp").getgrgid(cfg["socket_gid"]).gr_name
 expected=[cfg["python_path"],cfg["executor_child_path"],"--grant-id","%i","--grant-authority-socket",cfg["socket_path"],"--install-config",cfg["_config_path"]]
 try: argv=shlex.split(values.get("ExecStart",""),posix=True)
 except ValueError as exc: raise GrantError("executor ExecStart is malformed") from exc
 if argv!=expected: raise GrantError("executor ExecStart is not the exact fixed child invocation")
 required={"User":executor,"Group":group,"SupplementaryGroups":socket_group,"NoNewPrivileges":"yes","PrivateTmp":"yes","ProtectSystem":"strict"}
 if any(values.get(k)!=v for k,v in required.items()) or set(values)!={*required,"ExecStart"}: raise GrantError("executor unit identity or hardening contract mismatch")

def _verify_effective_systemd_unit(cfg):
 """Refuse manager indirection: resolved FragmentPath must be our template,
 no drop-ins may exist, and manager must say a daemon reload is not pending."""
 import subprocess
 unit=cfg["effective_unit_name"]; template=str(Path(cfg["executor_unit_template"]).resolve())
 try:
  shown=subprocess.run(["/usr/bin/systemctl","show",unit,"--property=FragmentPath,DropInPaths,NeedDaemonReload","--value"],capture_output=True,text=True,check=True).stdout.splitlines()
 except (OSError,subprocess.SubprocessError) as exc: raise GrantError("effective systemd unit cannot be resolved") from exc
 if len(shown)!=3 or shown[0].strip()!=template or shown[1].strip() or shown[2].strip().lower() not in ("no","false","0"):
  raise GrantError("effective systemd unit differs, has drop-ins, or daemon reload is pending")

def _owned_mode(path,uid,mode,label):
 try: st=Path(path).stat()
 except OSError as exc: raise GrantError(f"{label} missing") from exc
 if st.st_uid!=uid or _mode(Path(path))!=mode: raise GrantError(f"{label} owner/mode mismatch")

def _safe_root_chain(path, label):
 """Every component is a real root-owned non-writable directory; no symlink."""
 p=Path(path)
 if not p.is_absolute(): raise GrantError(f"{label} path is not absolute")
 for component in reversed((p, *p.parents)):
  try: st=os.lstat(component)
  except OSError as exc: raise GrantError(f"{label} parent missing") from exc
  if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or stat.S_IMODE(st.st_mode)&0o022:
   raise GrantError(f"{label} unsafe path component")

def _verify_import_closure(cfg):
 root=Path(cfg["source_root"])
 _safe_root_chain(root,"source root")
 if root.resolve()!=Path(cfg["runtime_path"]).resolve().parents[1]: raise GrantError("source root is not canonical runtime root")
 for rel,want in cfg["provider_import_closure"].items():
  if not isinstance(rel,str) or not isinstance(want,str) or not __import__("re").fullmatch(r"[0-9a-f]{64}",want): raise GrantError("provider import closure entry malformed")
  target=(root/rel)
  if target.resolve().parent != (root/rel).parent.resolve() or not str(target.resolve()).startswith(str(root.resolve())+os.sep): raise GrantError("provider import closure path escapes root")
  _root_regular_digest(target,0o755,want,"provider import closure member")

def verify_install(config_path):
 """Non-mutating provision verifier. Provisioning users, files, socket and
 systemd units is explicitly external; any missing OS boundary fails closed."""
 cfg=_load_install_config(config_path); state=Path(cfg["state_dir"]); sock=Path(cfg["socket_path"])
 _owned_mode(state,cfg["authority_uid"],0o700,"authority state directory")
 if state.stat().st_gid!=cfg["authority_gid"]: raise GrantError("authority state group mismatch")
 _owned_mode(cfg["gate_issuer_token_file"],cfg["gate_uid"],0o600,"gate issuer token")
 _owned_mode(cfg["authority_issuer_digest_file"],cfg["authority_uid"],0o600,"authority issuer digest")
 _root_regular_digest(cfg["runtime_path"],0o755,cfg["runtime_sha256"],"private executor runtime")
 _root_regular_digest(cfg["authority_path"],0o755,cfg["authority_sha256"],"grant authority source")
 _root_regular_digest(cfg["executor_child_path"],0o755,cfg["executor_child_sha256"],"executor child source")
 launcher=Path(cfg["executor_launcher"]); _root_regular_digest(launcher,0o4750,cfg["launcher_sha256"],"executor launcher")
 _root_regular_digest(cfg["executor_unit_template"],0o644,cfg["unit_sha256"],"executor unit template")
 _verify_import_closure(cfg)
 cfg["_config_path"]=str(Path(config_path).resolve()); _verify_unit(cfg)
 _verify_effective_systemd_unit(cfg)
 parent=sock.parent
 if not parent.is_dir() or _mode(parent)&0o022: raise GrantError("authority socket parent unsafe")
 try: st=sock.stat()
 except OSError as exc: raise GrantError("authority socket missing") from exc
 if not stat.S_ISSOCK(st.st_mode) or st.st_uid!=cfg["authority_uid"] or st.st_gid!=cfg["socket_gid"] or _mode(sock)!=0o660: raise GrantError("authority socket owner/group/mode mismatch")
 for uid,gid,label in ((cfg["authority_uid"],cfg["socket_gid"],"authority"),(cfg["gate_uid"],cfg["socket_gid"],"gate"),(cfg["executor_uid"],cfg["socket_gid"],"executor")):
  try: groups=os.getgrouplist(pwd.getpwuid(uid).pw_name,pwd.getpwuid(uid).pw_gid)
  except KeyError as exc: raise GrantError(f"{label} account missing") from exc
  if gid not in groups: raise GrantError(f"{label} lacks grant socket group")
 try: epw=pwd.getpwuid(cfg["executor_uid"])
 except KeyError as exc: raise GrantError("executor account missing") from exc
 if epw.pw_shell not in ("/usr/sbin/nologin","/sbin/nologin","/bin/false"):
  raise GrantError("executor account must be non-login")
 return {"ok":True,"authority_uid":cfg["authority_uid"],"gate_uid":cfg["gate_uid"],"executor_uid":cfg["executor_uid"],"socket":str(sock),"runtime":str(cfg["runtime_path"]),"launcher":str(launcher),"source_head":cfg["source_head"]}
def _path(d,gid):
 if not isinstance(gid,str) or len(gid)!=64 or any(c not in "0123456789abcdef" for c in gid): raise GrantError("grant id invalid")
 return d/("grant-"+gid+".json")
def _receipt_fingerprint(receipt):
 clone={k:v for k,v in receipt.items() if k!="receipt_fingerprint"}
 return "sha256:"+hashlib.sha256(_canon(clone).encode()).hexdigest()
def _outcome_path(d,gid): return d/("grant-"+gid+".outcome.json")
def _consume_receipt(gid,g):
 # This is minted by the authority before the grant becomes visible.  It is
 # copied verbatim into the consumed record and is the identity every later
 # terminal outcome must carry; callers cannot select a receipt namespace.
 receipt={"receipt_kind":"v2-executor-grant-consume-receipt","schema_version":1,
  "grant_id":gid,"task_id":g["task_id"],"board_db":g["board_db"],
  "workspace_root":g["workspace_root"],"session_binding":g["session_binding"],
  "cmux_receipt":g["cmux_receipt"],"reservation_json":g["reservation_json"],
  "binding_issuer":g["binding_issuer"],"hermes_home":g["hermes_home"],
  "lease_realpath":g["lease_realpath"],"lease_sha256":g["lease_sha256"],
  "source_head":g["source_head"]}
 receipt["receipt_fingerprint"]=_receipt_fingerprint(receipt)
 return receipt
def _issue(req,d,token_digest,cfg=None):
 token=req.get("issuer_token")
 if not isinstance(token,str) or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),token_digest): raise GrantError("issuer authentication refused")
 g=req.get("grant"); need={"task_id","board_db","workspace_root","session_binding","cmux_receipt","reservation_json","binding_issuer","hermes_home","lease_file","lease_realpath","lease_sha256","source_head","expires_at"}
 if not isinstance(g,dict) or set(g)!=need: raise GrantError("grant fields are not exact")
 if cfg is not None and g["source_head"] != cfg["source_head"]: raise GrantError("grant source head does not match verified install pin")
 if not isinstance(g["expires_at"],int) or not int(time.time())<g["expires_at"]<=int(time.time())+60: raise GrantError("grant expiry invalid")
 lease=Path(g["lease_file"]).resolve()
 if str(lease)!=g["lease_realpath"] or not lease.is_file(): raise GrantError("canonical consumed lease missing")
 if hashlib.sha256(lease.read_bytes()).hexdigest()!=g["lease_sha256"]: raise GrantError("lease hash mismatch")
 while True:
  gid=secrets.token_hex(32); p=_path(d,gid)
  try:
   fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
   g["launch_armed"] = False
   g["consume_receipt"]=_consume_receipt(gid,g)
   with os.fdopen(fd,"w") as f: json.dump(g,f,sort_keys=True)
   return gid
  except FileExistsError: pass
def _arm(req,d):
 gid=req.get("grant_id"); p=_path(d,gid)
 try: g=json.load(open(p))
 except (OSError, ValueError) as exc: raise GrantError("grant absent or malformed") from exc
 if g.get("launch_armed") is not False: raise GrantError("grant already armed")
 g["launch_armed"] = True
 tmp=p.with_suffix(".arming")
 try:
  with open(tmp,"x") as fh: json.dump(g,fh,sort_keys=True)
  os.replace(tmp,p)
 finally:
  try: tmp.unlink()
  except FileNotFoundError: pass
 return True
def _consume(req,d):
 gid=req.get("grant_id"); expected=req.get("expected")
 if not isinstance(expected,dict): raise GrantError("grant consume expected fields missing")
 p=_path(d,gid); used=p.with_suffix(".consumed")
 # Validate the non-secret immutable request before consuming.  An invalid
 # request must not burn a one-shot grant (while the subsequent rename still
 # supplies the authoritative race boundary for a valid request).
 try: preview=json.load(open(p))
 except (OSError,ValueError) as exc: raise GrantError("grant absent or malformed") from exc
 header={"receipt_kind":"v2-executor-grant-consume-receipt","schema_version":1,"grant_id":gid}
 receipt_preview=preview.get("consume_receipt")
 if not isinstance(receipt_preview,dict) or receipt_preview.get("receipt_fingerprint")!=_receipt_fingerprint(receipt_preview): raise GrantError("grant consume receipt missing or corrupt")
 if expected != header: raise GrantError("grant consume receipt binding mismatch")
 try: os.replace(p,used)
 except FileNotFoundError as e: raise GrantError("grant absent or already consumed") from e
 g=json.load(open(used))
 if g.get("launch_armed") is not True: raise GrantError("grant was not armed by gate launcher")
 receipt=g.get("consume_receipt")
 if not isinstance(receipt,dict) or receipt.get("receipt_fingerprint")!=_receipt_fingerprint(receipt): raise GrantError("grant consume receipt missing or corrupt")
 # The executor can know only the deterministic receipt header before the
 # authority reveals the opaque fingerprint.  Refuse wildcards, extras, and
 # any non-exact header; the returned receipt remains the authority's full
 # binding used by terminal persistence/readback.
 if expected != header: raise GrantError("grant consume receipt binding mismatch")
 if g.get("expires_at",0)<=int(time.time()): raise GrantError("grant expired")
 return g
def _record_outcome(req,d):
 gid=req.get("grant_id"); outcome=req.get("outcome")
 if not isinstance(outcome,dict): raise GrantError("terminal outcome missing")
 try: g=json.load(open(_path(d,gid).with_suffix(".consumed")))
 except (OSError,ValueError) as exc: raise GrantError("consumed grant absent") from exc
 receipt=g.get("consume_receipt")
 if not isinstance(receipt,dict) or outcome.get("consume_receipt_fingerprint")!=receipt.get("receipt_fingerprint"): raise GrantError("terminal outcome receipt mismatch")
 required={"outcome_kind","schema_version","grant_id","consume_receipt_fingerprint","status","task_id","source_head","terminal"}
 if set(outcome)!=required or outcome.get("outcome_kind")!="v2-executor-terminal-outcome" or outcome.get("schema_version")!=1 or outcome.get("grant_id")!=gid or outcome.get("task_id")!=receipt.get("task_id") or outcome.get("source_head")!=receipt.get("source_head") or outcome.get("status") not in ("completed","errored") or not isinstance(outcome.get("terminal"),dict): raise GrantError("terminal outcome fields invalid")
 if outcome["status"]=="completed" and outcome["terminal"]!={"guarded_lifecycle_done":True,"terminal_write":True,"marker":True}: raise GrantError("successful terminal outcome is not guarded-lifecycle complete")
 if outcome["status"]=="errored" and outcome["terminal"]!={"guarded_lifecycle_done":False,"terminal_write":False,"marker":False}: raise GrantError("errored terminal outcome shape invalid")
 target=_outcome_path(d,gid)
 try:
  fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 except FileExistsError as exc: raise GrantError("terminal outcome already recorded") from exc
 with os.fdopen(fd,"w") as fh: json.dump(outcome,fh,sort_keys=True); fh.flush(); os.fsync(fh.fileno())
 return outcome
def _read_outcome(req,d):
 gid=req.get("grant_id"); fingerprint=req.get("consume_receipt_fingerprint")
 try: g=json.load(open(_path(d,gid).with_suffix(".consumed")))
 except (OSError,ValueError) as exc: raise GrantError("consumed grant absent") from exc
 receipt=g.get("consume_receipt")
 if not isinstance(receipt,dict) or fingerprint!=receipt.get("receipt_fingerprint"): raise GrantError("outcome read receipt mismatch")
 try: outcome=json.load(open(_outcome_path(d,gid)))
 except FileNotFoundError: return {"pending":True,"consume_receipt":receipt}
 except (OSError,ValueError) as exc: raise GrantError("terminal outcome unreadable") from exc
 return {"pending":False,"consume_receipt":receipt,"outcome":outcome}
def serve(socket_path,state_dir,issuer_secret_file, *, install_config=None):
 cfg=_load_install_config(install_config) if install_config else None
 if cfg:
  if os.geteuid()!=cfg["authority_uid"]: raise GrantError("authority process identity mismatch")
  if Path(socket_path).resolve()!=Path(cfg["socket_path"]).resolve() or Path(state_dir).resolve()!=Path(cfg["state_dir"]).resolve(): raise GrantError("authority paths differ from verified install configuration")
 d=Path(state_dir).resolve(); _safe_state(d,expected_uid=None if not cfg else cfg["authority_uid"]); secret=Path(issuer_secret_file).resolve()
 if stat.S_IMODE(secret.stat().st_mode)&0o077: raise GrantError("issuer token file must be mode 0600")
 token_digest=secret.read_text().strip()
 if len(token_digest)!=64 or any(c not in "0123456789abcdef" for c in token_digest): raise GrantError("issuer token digest invalid")
 sp=Path(socket_path)
 if sp.exists(): raise GrantError("grant authority socket already exists; do not replace a live authority")
 srv=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); srv.bind(str(sp))
 if cfg:
  # Authority owns the socket and explicitly selects the configured shared
  # socket group; bind+chmod alone would leave an installation dependent on
  # the daemon's primary gid/umask.
  os.chown(sp,cfg["authority_uid"],cfg["socket_gid"])
 os.chmod(sp,0o660)
 if cfg:
  st=sp.stat()
  if st.st_uid!=cfg["authority_uid"] or st.st_gid!=cfg["socket_gid"] or _mode(sp)!=0o660:
   raise GrantError("authority socket ownership/mode setup failed")
 srv.listen(16)
 try:
  while True:
   conn,_=srv.accept()
   with conn:
    try:
     req=_recv(conn)
     peer=_peer_uid(conn) if cfg else None
     if req.get("op")=="issue":
      if cfg and peer!=cfg["gate_uid"]: raise GrantError("grant issue peer identity refused")
      gid=_issue(req,d,token_digest,cfg)
      out={"ok":True,"grant_id":gid,"consume_receipt":json.load(open(_path(d,gid))).get("consume_receipt")}
     elif req.get("op")=="arm":
      if cfg and peer!=cfg["gate_uid"]: raise GrantError("grant arm peer identity refused")
      out={"ok":True,"armed":_arm(req,d)}
     elif req.get("op")=="consume":
      if cfg and peer!=cfg["executor_uid"]: raise GrantError("grant consume peer identity refused")
      out={"ok":True,"grant":_consume(req,d)}
     elif req.get("op")=="record_outcome":
      if cfg and peer!=cfg["executor_uid"]: raise GrantError("terminal outcome peer identity refused")
      out={"ok":True,"outcome":_record_outcome(req,d)}
     elif req.get("op")=="read_outcome":
      if cfg and peer!=cfg["gate_uid"]: raise GrantError("terminal outcome read peer identity refused")
      out={"ok":True,**_read_outcome(req,d)}
     else: raise GrantError("unknown grant operation")
    except Exception as e: out={"ok":False,"error":str(e)[:300]}
    _send(conn,out)
 finally: srv.close()
def request(socket_path,payload):
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
  s.connect(socket_path); _send(s,payload); out=_recv(s)
 if not out.get("ok"): raise GrantError(out.get("error","grant authority refused"))
 return out
def main(argv=None):
 p=argparse.ArgumentParser(); subs=p.add_subparsers(dest="cmd",required=True)
 s=subs.add_parser("serve"); s.add_argument("--socket",required=True); s.add_argument("--state-dir",required=True); s.add_argument("--issuer-secret-file",required=True); s.add_argument("--install-config")
 v=subs.add_parser("verify-install"); v.add_argument("--config",required=True)
 a=p.parse_args(argv)
 if a.cmd=="serve": serve(a.socket,a.state_dir,a.issuer_secret_file,install_config=a.install_config)
 elif a.cmd=="verify-install": print(_canon(verify_install(a.config)))
if __name__=="__main__": main()
