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
import argparse, hashlib, json, os, secrets, socket, stat, time
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
def _safe_state(d):
 d.mkdir(parents=True,exist_ok=True)
 if stat.S_IMODE(d.stat().st_mode)&0o022: raise GrantError("grant state directory must not be group/world writable")
def _path(d,gid):
 if not isinstance(gid,str) or len(gid)!=64 or any(c not in "0123456789abcdef" for c in gid): raise GrantError("grant id invalid")
 return d/("grant-"+gid+".json")
def _issue(req,d,token):
 if req.get("issuer_token")!=token: raise GrantError("issuer authentication refused")
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
def serve(socket_path,state_dir,issuer_secret_file):
 d=Path(state_dir).resolve(); _safe_state(d); secret=Path(issuer_secret_file).resolve()
 if stat.S_IMODE(secret.stat().st_mode)&0o077: raise GrantError("issuer token file must be mode 0600")
 token=secret.read_text().strip()
 if len(token)<32: raise GrantError("issuer token too short")
 sp=Path(socket_path)
 if sp.exists(): raise GrantError("grant authority socket already exists; do not replace a live authority")
 srv=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); srv.bind(str(sp)); os.chmod(sp,0o660); srv.listen(16)
 try:
  while True:
   conn,_=srv.accept()
   with conn:
    try:
     req=_recv(conn)
     if req.get("op")=="issue": out={"ok":True,"grant_id":_issue(req,d,token)}
     elif req.get("op")=="consume": out={"ok":True,"consumed":_consume(req,d)}
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
 s=subs.add_parser("serve"); s.add_argument("--socket",required=True); s.add_argument("--state-dir",required=True); s.add_argument("--issuer-secret-file",required=True)
 a=p.parse_args(argv)
 if a.cmd=="serve": serve(a.socket,a.state_dir,a.issuer_secret_file)
if __name__=="__main__": main()
