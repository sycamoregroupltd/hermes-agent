#!/usr/bin/env python3
"""No-provider adversarial protocol checks for executor outcome receipts."""
import importlib.util, json, tempfile, time
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("ga",ROOT/"bin_verify"/"v2_grant_authority.py")
ga=importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)
FAIL=[]
def check(name, ok):
 print(("PASS" if ok else "FAIL")+": "+name)
 if not ok: FAIL.append(name)
def refused(fn):
 try: fn(); return False
 except ga.GrantError: return True
def grant(root):
 lease=root/"lease.json"; lease.write_text("lease")
 g={"task_id":"t_beefcafe","board_db":"/canonical/board.db","workspace_root":"/canonical/workspace",
 "session_binding":"/canonical/binding.json","cmux_receipt":"/canonical/receipt.json","reservation_json":"/canonical/reservation.json",
 "binding_issuer":"/canonical/issuer.py","hermes_home":"/canonical/profile","lease_file":str(lease),"lease_realpath":str(lease.resolve()),
 "lease_sha256":ga._sha256(lease),"source_head":"a"*64,"expires_at":int(time.time())+30}
 gid=ga._issue({"issuer_token":"token","grant":g},root,__import__("hashlib").sha256(b"token").hexdigest())
 ga._arm({"grant_id":gid},root); return gid
def main():
 with tempfile.TemporaryDirectory() as td:
  root=Path(td); gid=grant(root); stored=json.load(open(ga._path(root,gid))); rec=stored["consume_receipt"]
  check("issue stores authority-created receipt bound to all identities",rec["grant_id"]==gid and rec["task_id"]=="t_beefcafe" and rec["lease_sha256"] and rec["session_binding"] and rec["cmux_receipt"] and rec["source_head"]=="a"*64)
  check("launcher acceptance cannot fabricate consume/outcome",refused(lambda: ga._record_outcome({"grant_id":gid,"outcome":{}},root)) and refused(lambda: ga._consume({"grant_id":gid,"expected":{}},root)) and refused(lambda: ga._read_outcome({"grant_id":gid,"consume_receipt_fingerprint":rec["receipt_fingerprint"]},root)))
  consumed=ga._consume({"grant_id":gid,"expected":{"receipt_kind":"v2-executor-grant-consume-receipt","schema_version":1,"grant_id":gid}},root)
  check("consume atomically moves grant and returns same canonical receipt",consumed["consume_receipt"]==rec and not ga._path(root,gid).exists())
  pending=ga._read_outcome({"grant_id":gid,"consume_receipt_fingerprint":rec["receipt_fingerprint"]},root)
  check("consumed without terminal outcome remains pending",pending.get("pending") is True)
  good={"outcome_kind":"v2-executor-terminal-outcome","schema_version":1,"grant_id":gid,"consume_receipt_fingerprint":rec["receipt_fingerprint"],"status":"completed","task_id":"t_beefcafe","source_head":"a"*64,"terminal":{"guarded_lifecycle_done":True,"terminal_write":True,"marker":True}}
  bad=dict(good); bad["consume_receipt_fingerprint"]="sha256:forged"
  check("wrong receipt outcome is refused",refused(lambda:ga._record_outcome({"grant_id":gid,"outcome":bad},root)))
  ga._record_outcome({"grant_id":gid,"outcome":good},root)
  check("only matching guarded terminal success is readable",ga._read_outcome({"grant_id":gid,"consume_receipt_fingerprint":rec["receipt_fingerprint"]},root).get("outcome")==good)
  check("terminal outcome cannot replay or overwrite",refused(lambda:ga._record_outcome({"grant_id":gid,"outcome":good},root)))
  check("read refuses forged receipt fingerprint",refused(lambda:ga._read_outcome({"grant_id":gid,"consume_receipt_fingerprint":"sha256:forged"},root)))
  gate_spec=importlib.util.spec_from_file_location("gate",ROOT/"bin_verify"/"dispatch_gate_v2.py"); gate=importlib.util.module_from_spec(gate_spec); gate_spec.loader.exec_module(gate)
  check("outcome deadline covers required heartbeats and remains bounded", gate.OUTCOME_POLL_SECONDS == gate.HEARTBEAT_INTERVAL_SECONDS * gate.REQUIRED_RUN_BOUND_HEARTBEATS + gate.OUTCOME_TERMINAL_ALLOWANCE_SECONDS and gate.OUTCOME_POLL_SECONDS == 55)

  try:
   gate.observe_executor_outcome("unused", gid, rec, timeout_seconds=15); short_refused=False
  except RuntimeError: short_refused=True
  check("short outcome deadline is refused before any wait", short_refused)
  gid2=grant(root); rec2=json.load(open(ga._path(root,gid2)))["consume_receipt"]
  original_attest=ga._executor_instance_attested; ga._executor_instance_attested=lambda pid,gid,cfg: None
  try:
   consumed2=ga._consume({"grant_id":gid2,"expected":{"receipt_kind":"v2-executor-grant-consume-receipt","schema_version":1,"grant_id":gid2}},root,peer_pid=101,cfg={})
   good2=dict(good); good2["grant_id"]=gid2; good2["consume_receipt_fingerprint"]=rec2["receipt_fingerprint"]
   check("same UID forged outcome without authority capability is refused",refused(lambda:ga._record_outcome({"grant_id":gid2,"outcome":good2},root,peer_pid=101,cfg={})))
   cap=consumed2["outcome_capability"]
   check("same UID replay from a different PID is refused",refused(lambda:ga._record_outcome({"grant_id":gid2,"outcome_capability":cap,"outcome":good2},root,peer_pid=202,cfg={})))
   ga._record_outcome({"grant_id":gid2,"outcome_capability":cap,"outcome":good2},root,peer_pid=101,cfg={})
   check("attested executor PID plus per-launch capability can record once",ga._read_outcome({"grant_id":gid2,"consume_receipt_fingerprint":rec2["receipt_fingerprint"]},root).get("outcome")==good2)
  finally: ga._executor_instance_attested=original_attest
 print("RESULT: "+("PASS" if not FAIL else "FAIL "+repr(FAIL)))
 return 0 if not FAIL else 1
if __name__=="__main__": raise SystemExit(main())
