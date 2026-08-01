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
import argparse, hashlib, json, os, pwd, secrets, socket, stat, time
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
 required={"authority_uid","authority_gid","gate_uid","gate_gid","executor_uid","executor_gid","socket_gid","state_dir","socket_path","gate_issuer_token_file","authority_issuer_digest_file"}
 if set(cfg)!=required: raise GrantError("grant install configuration fields are not exact")
 nums={"authority_uid","authority_gid","gate_uid","gate_gid","executor_uid","executor_gid","socket_gid"}
 if not all(isinstance(cfg[k],int) for k in nums): raise GrantError("grant install numeric identities invalid")
 if not all(isinstance(cfg[k],str) and cfg[k] for k in required-nums): raise GrantError("grant install paths invalid")
 if len({cfg["authority_uid"],cfg["gate_uid"],cfg["executor_uid"]})!=3: raise GrantError("authority, gate and executor must be distinct OS accounts")
 return cfg

def _owned_mode(path,uid,mode,label):
 try: st=Path(path).stat()
 except OSError as exc: raise GrantError(f"{label} missing") from exc
 if st.st_uid!=uid or _mode(Path(path))!=mode: raise GrantError(f"{label} owner/mode mismatch")

def verify_install(config_path):
 """Non-mutating provision verifier. Provisioning users, files, socket and
 systemd units is explicitly external; any missing OS boundary fails closed."""
 cfg=_load_install_config(config_path); state=Path(cfg["state_dir"]); sock=Path(cfg["socket_path"])
 _owned_mode(state,cfg["authority_uid"],0o700,"authority state directory")
 if state.stat().st_gid!=cfg["authority_gid"]: raise GrantError("authority state group mismatch")
 _owned_mode(cfg["gate_issuer_token_file"],cfg["gate_uid"],0o600,"gate issuer token")
 _owned_mode(cfg["authority_issuer_digest_file"],cfg["authority_uid"],0o600,"authority issuer digest")
 parent=sock.parent
 if not parent.is_dir() or _mode(parent)&0o022: raise GrantError("authority socket parent unsafe")
 try: st=sock.stat()
 except OSError as exc: raise GrantError("authority socket missing") from exc
 if not stat.S_ISSOCK(st.st_mode) or st.st_uid!=cfg["authority_uid"] or st.st_gid!=cfg["socket_gid"] or _mode(sock)!=0o660: raise GrantError("authority socket owner/group/mode mismatch")
 for uid,gid,label in ((cfg["authority_uid"],cfg["socket_gid"],"authority"),(cfg["gate_uid"],cfg["socket_gid"],"gate"),(cfg["executor_uid"],cfg["socket_gid"],"executor")):
  try: groups=os.getgrouplist(pwd.getpwuid(uid).pw_name,pwd.getpwuid(uid).pw_gid)
  except KeyError as exc: raise GrantError(f"{label} account missing") from exc
  if gid not in groups: raise GrantError(f"{label} lacks grant socket group")
 return {"ok":True,"authority_uid":cfg["authority_uid"],"gate_uid":cfg["gate_uid"],"executor_uid":cfg["executor_uid"],"socket":str(sock)}
def _path(d,gid):
 if not isinstance(gid,str) or len(gid)!=64 or any(c not in "0123456789abcdef" for c in gid): raise GrantError("grant id invalid")
 return d/("grant-"+gid+".json")
def _issue(req,d,token_digest):
 token=req.get("issuer_token")
 if not isinstance(token,str) or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),token_digest): raise GrantError("issuer authentication refused")
 g=req.get("grant"); need={"task_id","board_db","workspace_root","session_binding","cmux_receipt","reservation_json","binding_issuer","hermes_home","lease_file","lease_realpath","lease_sha256","source_head","expires_at"}
 if not isinstance(g,dict) or set(g)!=need: raise GrantError("grant fields are not exact")
 if not isinstance(g["expires_at"],int) or not int(time.time())<g["expires_at"]<=int(time.time())+60: raise GrantError("grant expiry invalid")
 lease=Path(g["lease_file"]).resolve()
 if str(lease)!=g["lease_realpath"] or not lease.is_file(): raise GrantError("canonical consumed lease missing")
 if hashlib.sha256(lease.read_bytes()).hexdigest()!=g["lease_sha256"]: raise GrantError("lease hash mismatch")
 while True:
  gid=secrets.token_hex(32); p=_path(d,gid)
  try:
   fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
   with os.fdopen(fd,"w") as f: json.dump(g,f,sort_keys=True)
   return gid
  except FileExistsError: pass
def _consume(req,d):
 gid=req.get("grant_id"); expected=req.get("expected")
 if not isinstance(expected,dict): raise GrantError("grant consume expected fields missing")
 p=_path(d,gid); used=p.with_suffix(".consumed")
 try: os.replace(p,used)
 except FileNotFoundError as e: raise GrantError("grant absent or already consumed") from e
 g=json.load(open(used))
 if any(g.get(k)!=v for k,v in expected.items()): raise GrantError("grant binding mismatch")
 if g.get("expires_at",0)<=int(time.time()): raise GrantError("grant expired")
 return True
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
 srv=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); srv.bind(str(sp)); os.chmod(sp,0o660); srv.listen(16)
 try:
  while True:
   conn,_=srv.accept()
   with conn:
    try:
     req=_recv(conn)
     peer=_peer_uid(conn) if cfg else None
     if req.get("op")=="issue":
      if cfg and peer!=cfg["gate_uid"]: raise GrantError("grant issue peer identity refused")
      out={"ok":True,"grant_id":_issue(req,d,token_digest)}
     elif req.get("op")=="consume":
      if cfg and peer!=cfg["executor_uid"]: raise GrantError("grant consume peer identity refused")
      out={"ok":True,"consumed":_consume(req,d)}
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
