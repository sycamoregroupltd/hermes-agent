#!/usr/bin/env python3
"""NO-PROVIDER integration checks: CMUX issuer artifact -> v2 consumer."""
from __future__ import annotations
import datetime as dt, hashlib, importlib.util, json, sqlite3, tempfile, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; B=ROOT/'bin_verify'
sys.path.insert(0, str(B))
def load(name):
 s=importlib.util.spec_from_file_location(name,B/(name+'.py')); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
issuer=load('issue_cmux_claude_session_binding'); v2=load('v2_canary_executor')
NOW=dt.datetime.now(dt.timezone.utc); TASK='t_beefcafe'; SESSION='1194f145-bc7d-4fd6-9762-16b4414eb4d1'; WS='9A3E7E93-963F-45AB-9A00-79E218190B5D'; SF='577E1920-C0EE-4140-A649-361647B6B9A5'
def iso(x): return x.isoformat().replace('+00:00','Z')
def put(p,x): p.write_text(json.dumps(x,sort_keys=True))
def setup(r):
 w=r/'wt'; (w/'bin_verify').mkdir(parents=True); (w/'bin_verify'/'mint_cmux_receipt.py').write_text('#'); (w/'bin_verify'/'dispatch_gate_v2.py').write_text('#')
 db=r/'b.db'; c=sqlite3.connect(db); c.execute('create table tasks (id text,status text)'); c.execute('create table task_runs (id integer primary key,task_id text)'); c.execute('insert into tasks values (?,?)',(TASK,'blocked')); c.commit();c.close()
 res={'record_kind':'cmux-manual-seat-reservation','seat':{'cmux_workspace_id':WS,'cmux_surface_id':SF,'cmux_daemon_version':'0.64.20','provider':'claude-code','kind':'cmux-interactive-claude-max','provider_session_uuid':SESSION}}; res['reservation_fingerprint']=issuer.reservation_fingerprint(res); rp=r/'r.json';put(rp,res)
 rec={'receipt_kind':'mac-cmux-reservation-receipt','minted_at_utc':iso(NOW-dt.timedelta(seconds=1)),'expires_at_utc':iso(NOW+dt.timedelta(seconds=300)),'canary_task':TASK,'cmux_workspace_id':WS,'cmux_surface_id':SF,'caller_context':{'surface_id':SF,'workspace_id':WS,'tty':'/dev/ttys012','proof':'nonce-read-screen','nonce_sha256':hashlib.sha256(b'consumer-fixture-nonce').hexdigest()},'control_socket':{'cmux_daemon_version':'0.64.20','bundle_identifier':'com.cmuxterm.app'}};rec['receipt_fingerprint']=issuer.receipt_fingerprint(rec); cp=r/'c.json';put(cp,rec)
 bp=issuer.issue_binding(worktree=w,board_db=db,reservation_path=rp,receipt_path=cp,task_id=TASK,session_id=SESSION,declared_by='test',ttl_seconds=120,now=NOW); return w,db,rp,cp,bp
def valid(bp,db,rp,cp): return v2.load_session_binding(bp,expected_task_id=TASK,board_db=db,cmux_receipt_path=cp,reservation_path=rp,issuer_path=B/'issue_cmux_claude_session_binding.py',now=NOW)
def main():
 with tempfile.TemporaryDirectory() as d:
  w,db,rp,cp,bp=setup(Path(d)); rec=valid(bp,db,rp,cp); assert rec['session_id']==SESSION
  try: valid(bp,Path(d)/'wrong.db',rp,cp); raise AssertionError('board mismatch accepted')
  except v2.DispatchError: pass
  assert v2.retire_session_binding_artifact(bp,rec) is True
  try: valid(bp,db,rp,cp); raise AssertionError('retired artifact accepted')
  except v2.DispatchError: pass
 print('PASS: CMUX-bound issuer artifact revalidated and retired without provider')
 return 0
if __name__=='__main__': raise SystemExit(main())
