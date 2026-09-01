#!/usr/bin/env python3
"""
Mocked self-test for anomaly_ledger.py -- proves the three flows required by
task t_cd7e8188 without touching the live kanban board or Discord.

Run:  python3 /home/frank/.hermes/scripts/anomaly_ledger_selftest.py
Exits non-zero on any assertion failure.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anomaly_ledger as L


class FakeHarness:
    """In-memory stand-in for KanbanHarness -- records calls, no I/O."""
    def __init__(self):
        self.created = []          # list of (title, body, assignee, priority, board)
        self.comments = []         # list of (task_id, body, board)
        self.completed = []        # list of (task_id, summary, board)
        self.alerts = []           # list of (target, message)
        self._counter = 0
        self._status = {}          # task_id -> status (for _status_fn simulation)

    def create_ticket(self, title, body, assignee, priority=5, board="jarvis-os"):
        self._counter += 1
        tid = f"t_test{self._counter:04d}"
        self.created.append((title, body, assignee, priority, board))
        self._status[tid] = "ready"
        return tid

    def comment(self, task_id, body, board="jarvis-os"):
        self.comments.append((task_id, body, board))

    def complete(self, task_id, summary, board="jarvis-os"):
        self.completed.append((task_id, summary, board))
        self._status[task_id] = "completed"

    def send_alert(self, target, message):
        self.alerts.append((target, message))
        return True

    def status_fn(self, task_id, board="jarvis-os"):
        return self._status.get(task_id, "completed")


PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")
        raise SystemExit(f"ABORT on first failure: {label}")


def ts(n):
    return f"2026-07-11T0{n}:00:00Z"


print("== Flow A: first detection creates a ticket ==")
h = FakeHarness()
ledger = {"version": 1, "entries": {}}
r = L.record_anomaly(
    ledger, report_class="Fusion Calibration", source="calibration_001.log",
    fingerprint="fp1", title="[ACRADR] Calibration win_rate 12% < 20%",
    body="win_rate=12.0% calibration_error=8pp", assignee="os-reviewer",
    channel="discord:#quant-reports", priority=8, board="jarvis-os", harness=h,
    now=ts(1),
)
check(r["action"] == "created", "action == created")
check(len(h.created) == 1, "exactly one ticket created")
check(h.created[0][2] == "os-reviewer", "assignee passed through")
check(h.created[0][3] == 8, "priority 8 passed through")
tid = r["task_id"]
key = L._entry_key("Fusion Calibration", "calibration_001.log")
check(key in ledger["entries"], "ledger entry recorded under (class,source) key")
check(ledger["entries"][key]["task_id"] == tid, "ledger stores task_id under entry")

print("== Flow B: subsequent run dedupes (ledger count, no new comment/ticket) ==")
r2 = L.record_anomaly(
    ledger, report_class="Fusion Calibration", source="calibration_001.log",
    fingerprint="fp2", title="ignored", body="win_rate=10.0%", assignee="os-reviewer",
    channel="discord:#quant-reports", board="jarvis-os", harness=h, now=ts(2),
)
check(r2["action"] == "deduped", "action == deduped")
check(len(h.created) == 1, "no second ticket created (dedupe)")
check(len(h.comments) == 0, "repeat creates no comment")
check(r2["occurrences"] == 2, "occurrence counter incremented to 2")

print("== Flow C: resolution self-heals (comment + complete + clear + alert) ==")
r3 = L.resolve_anomaly(
    ledger, report_class="Fusion Calibration", source="calibration_001.log",
    board="jarvis-os", harness=h, now=ts(3),
    _status_fn=h.status_fn,
)
check(r3["action"] == "resolved", "action == resolved")
check(r3["task_id"] == tid, "resolved the same ticket")
check(len(h.comments) == 1, "one RESOLVED comment")
check(h.comments[0][1].startswith(L.RESOLVED_COMMENT_PREFIX), "RESOLVED comment text correct")
check(ts(3) in h.comments[0][1], "RESOLVED comment carries timestamp")
check(len(h.completed) == 1, "task auto-completed")
check(h.completed[0][0] == tid, "completed the same ticket")
check(len(h.alerts) == 1, "recovery alert sent")
check(h.alerts[0][0] == "discord:#quant-reports", "alert routed to originating channel")
check(L._entry_key("Fusion Calibration", "calibration_001.log") not in ledger["entries"],
      "ledger entry cleared after resolve")

print("== Edge: resolve when no entry -> no-op, no side effects ==")
h2 = FakeHarness()
ledger2 = {"version": 1, "entries": {}}
r4 = L.resolve_anomaly(ledger2, report_class="Ghost", source="ghost.log",
                        harness=h2, now=ts(4), _status_fn=h2.status_fn)
check(r4["action"] == "no_entry", "no_entry for unknown key")
check(len(h2.comments) == 0 and len(h2.completed) == 0 and len(h2.alerts) == 0,
      "no side effects on no_entry")

print("== Edge: resolve leaves task open if already in_progress ==")
h3 = FakeHarness()
ledger3 = {"version": 1, "entries": {}}
L.record_anomaly(ledger3, report_class="Health Canary", source="hc.log",
                 fingerprint="x", title="gw down", body="gateway_running=false",
                 assignee="trading-devops", channel="discord:#critical-alerts",
                 board="jarvis-os", harness=h3, now=ts(1))
# simulate a human picking it up -> in_progress
claimed_tid = ledger3["entries"][L._entry_key("Health Canary", "hc.log")]["task_id"]
h3._status[claimed_tid] = "in_progress"
r5 = L.resolve_anomaly(ledger3, report_class="Health Canary", source="hc.log",
                        harness=h3, now=ts(3), _status_fn=h3.status_fn)
check(r5["action"] == "resolved", "still reports resolved (entry retained)")
check(len(h3.completed) == 0, "did NOT auto-complete an in_progress task")
check(any("not auto-closed" in c[1] for c in h3.comments), "notes it left the task open")
check(L._entry_key("Health Canary", "hc.log") in ledger3["entries"],
      "entry retained for a future retry")
check(len(h3.alerts) == 1, "recovery alert still sent to channel")

print("== Edge: atomic save/load round-trips ==")
import tempfile, os
tmp = Path(tempfile.mkdtemp()) / "detected_anomalies.json"
L.save_ledger(ledger3, tmp)
reloaded = L.load_ledger(tmp)
check(reloaded["entries"] == ledger3["entries"], "save/load round-trips entries")
# corrupt file -> empty ledger, no crash
bad = tmp.with_suffix(".bad")
bad.write_text("{not json")
check(L.load_ledger(bad) == {"version": 2, "entries": {},
                              "source_high_water": {}, "source_guards": {}},
      "corrupt ledger -> empty, no crash")

print("== Entry key derives STABLE cron_job_id from parent dir (t_0596724e) ==")
# Two dated reports from the SAME cron job (f05227128ac2) must collapse to
# ONE dedupe key -- this is the bug fix: previously keyed on the per-run
# filename, so every run spawned a new card (card churn).
p1 = "/home/frank/.hermes/profiles/jarvis/cron/output/f05227128ac2/2026-07-13_00-03-19.md"
p2 = "/home/frank/.hermes/profiles/jarvis/cron/output/f05227128ac2/2026-07-13_06-09-20.md"
check(L._cron_job_id(p1) == "f05227128ac2", "cron job id = parent dir of dated report")
check(L._cron_job_id(p2) == "f05227128ac2", "cron job id stable across runs")
k1 = L._entry_key("fusion_calibration", p1)
k2 = L._entry_key("fusion_calibration", p2)
check(k1 == "fusion_calibration::f05227128ac2", "key = class::jobid")
check(k2 == "fusion_calibration::f05227128ac2",
      "two dated reports of same job share ONE key (no churn)")

print("== Bare source (no parent dir) keeps a stable unique key (fallback) ==")
check(L._cron_job_id("calibration_001.log") == "calibration_001.log",
      "bare source falls back to basename")
check(L._entry_key("Fusion Calibration", "calibration_001.log")
      == "Fusion Calibration::calibration_001.log",
      "legacy selftest key preserved for bare sources")

print(f"\nALL CHECKS PASSED: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
