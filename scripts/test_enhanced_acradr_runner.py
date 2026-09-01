"""Integration tests for enhanced_acradr_runner.py — Enhanced ACRADR Phase 3.

Covers the full routing + ledger lifecycle with a MOCK harness (no real kanban
or Discord I/O):

  1. Multi-channel routing:
       - critical (gateway/fusion)        -> discord:critical-alerts
       - fusion_calibration (warning)     -> discord:quant-reports
       - freshness / news (warning)       -> discord:fleet-reports
  2. Metadata injection: git_context + system_metrics present in the created
     kanban ticket body.
  3. Self-healing closure loop:
       - run 1 (anomaly present)  -> 1 ticket created + 1 Discord alert
       - run 2 (still present)    -> 0 new tickets, ledger-only count update
       - run 3 (cleared)          -> ticket auto-completed + recovery alert
  4. Watchdog: stale heartbeat -> alert to #jarvis-os-governance; fresh -> silent.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Load the runner from this checkout's profile path, never the live Jarvis home.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER_PATH = _REPO_ROOT / "profiles/jarvis/scripts/enhanced_acradr_runner.py"
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("enhanced_acradr_runner", _RUNNER_PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

import dgx_report_anomaly_detector as det  # noqa: E402
import anomaly_ledger as ledger  # noqa: E402


class MockHarness:
    """Records kanban + Discord calls, performs no I/O."""

    def __init__(self):
        self.created = []
        self.comments = []
        self.completed = []
        self.alerts = []
        self.open_acradr = []

    def create_ticket(self, title, body, assignee, priority=5, board=None):
        tid = f"t_mock{len(self.created)}"
        self.created.append({"id": tid, "title": title, "assignee": assignee,
                             "body": body})
        return tid

    def comment(self, task_id, body, board=None):
        self.comments.append({"id": task_id, "body": body})

    def complete(self, task_id, summary, board=None):
        self.completed.append({"id": task_id, "summary": summary})

    def send_alert(self, target, message):
        self.alerts.append({"target": target, "message": message})
        return True

    def list_open_acradr(self, board=None):
        return list(self.open_acradr)

    def status(self, task_id, board=None):
        return "ready"


def _anom(report_class, rule_id, severity, snippet, **kw):
    return det.Anomaly(report_class, rule_id, severity, kw.get("source", "x"),
                       kw.get("line"), snippet,
                       git_context=kw.get("git", ["(commit)"]),
                       system_metrics=kw.get("metrics", {"cpu_percent": 10}))


def _load_watchdog():
    wspec = importlib.util.spec_from_file_location(
        "enhanced_acradr_watchdog",
        _REPO_ROOT / "profiles/jarvis/scripts/enhanced_acradr_watchdog.py")
    wd = importlib.util.module_from_spec(wspec)
    wspec.loader.exec_module(wd)
    return wd


# ── 1. Multi-channel routing ────────────────────────────────────────────────
def test_channel_routing_critical():
    a = _anom("health_canary", "health.gateway_down", "critical", "gw down")
    assert runner.channel_for(a) == "discord:critical-alerts"
    b = _anom("fusion_engine", "fusion.database_error", "critical", "db err")
    assert runner.channel_for(b) == "discord:critical-alerts"


def test_channel_routing_calibration_warning():
    a = _anom("fusion_calibration", "calibration.win_rate_low", "warning", "12%")
    assert runner.channel_for(a) == "discord:quant-reports"


def test_channel_routing_routine_warning():
    fresh = _anom("health_canary", "freshness.pipeline_stale", "warning", "stale")
    assert runner.channel_for(fresh) == "discord:fleet-reports"
    news = _anom("news_catalyst", "news.timeout", "warning", "timeout")
    assert runner.channel_for(news) == "discord:fleet-reports"


# ── 2. Metadata injection in ticket body ────────────────────────────────────
def test_metadata_injected_into_ticket_body():
    a = _anom("fusion_engine", "fusion.database_error", "critical", "db err",
              git=["abc123 fix db pool"],
              metrics={"cpu_percent": 42, "mem_percent": 70,
                       "mem_used_gb": 10, "mem_total_gb": 64})
    body = runner._ticket_body(a)
    assert "abc123 fix db pool" in body        # git context present
    assert "cpu=42%" in body                    # system metrics present
    assert "mem=70%" in body


# ── 3. Self-healing closure loop (mock ledger on disk) ──────────────────────
def test_self_heal_create_dedupe_resolve(tmp_path):
    ledger_path = tmp_path / "detected_anomalies.json"
    h = MockHarness()

    # RUN 1: one critical anomaly present.
    # The guard must inspect a real, fresh source file.
    source = tmp_path / "health_canary.jsonl"
    source.write_text('{"gateway_running": false}\n')
    anoms1 = [_anom("health_canary", "health.gateway_down", "critical",
                    "gateway_running: false", source=str(source))]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: anoms1)
        runner.core_run(ledger_path=ledger_path, harness=h, quiet=True)
    assert len(h.created) == 1, "expected 1 ticket created on first detection"
    assert len(h.alerts) == 1
    assert h.alerts[0]["target"] == "discord:critical-alerts"
    created_id = h.created[0]["id"]
    assert h.created[0]["assignee"] == "trading-devops"

    # A new source revision with the same stable anomaly key is a repeat.
    source.write_text('{"gateway_running": false}\nupdated\n')
    h2 = MockHarness()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: anoms1)
        runner.core_run(ledger_path=ledger_path, harness=h2, quiet=True)
    assert len(h2.created) == 0, "dedupe must not create a new ticket"
    assert len(h2.comments) == 0, "repeat must not create a new comment"
    book2 = json.loads(ledger_path.read_text())
    key2 = next(iter(book2["entries"]))
    assert book2["entries"][key2]["occurrences"] == 2

    # RUN 3: anomaly cleared -> self-heal: complete + recovery alert.
    h3 = MockHarness()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: [])
        runner.core_run(ledger_path=ledger_path, harness=h3, quiet=True)
    assert len(h3.completed) == 1, "resolved ticket must be auto-completed"
    assert h3.completed[0]["id"] == created_id
    assert len(h3.alerts) == 1, "recovery alert must be sent"
    book = json.loads(ledger_path.read_text())
    assert book["entries"] == {}


# ── 3b. Multi-class routing across one scan ────────────────────────────────
def test_multi_class_routing_in_one_run(tmp_path):
    ledger_path = tmp_path / "detected_anomalies.json"
    sources = {
        "fusion_engine": tmp_path / "run-signal-fusion.md",
        "fusion_calibration": tmp_path / "fusion-calibration-report.md",
        "news_catalyst": tmp_path / "news-sentiment-catalyst.md",
    }
    for path in sources.values():
        path.write_text("fresh source\n")
    anoms = [
        _anom("fusion_engine", "fusion.database_error", "critical", "db err",
              source=str(sources["fusion_engine"])),
        _anom("fusion_calibration", "calibration.win_rate_low", "warning", "12%",
              source=str(sources["fusion_calibration"])),
        _anom("news_catalyst", "news.timeout", "warning", "timeout",
              source=str(sources["news_catalyst"])),
    ]
    h = MockHarness()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: anoms)
        runner.core_run(ledger_path=ledger_path, harness=h, quiet=True)
    assert len(h.created) == 3
    assert len(h.alerts) == 3
    targets = {a["target"] for a in h.alerts}
    assert "discord:critical-alerts" in targets
    assert "discord:quant-reports" in targets
    assert "discord:fleet-reports" in targets
    assignees = {c["assignee"] for c in h.created}
    assert "os-architect" in assignees       # fusion_engine
    assert "os-reviewer" in assignees         # fusion_calibration
    assert "integration-builder" in assignees  # news_catalyst


# ── 3c. Dedupe-key fix (t_0596724e): 2+ dated reports, ONE cron job -> 1 entry ─
def test_dedupe_keys_on_cron_job_not_filename(tmp_path):
    """Regression for ACRADR card churn.

    Two dated calibration reports from the SAME cron-job directory must
    collapse onto ONE ledger entry (occurrences accumulate), not one per
    run. Builds a minimal report tree, runs the real detector + ledger via
    core_run (mock harness), and asserts exactly one entry under the
    stable key class::<job_id>.
    """
    job_dir = tmp_path / "fusion-calibration-report" / "f05227128ac2"
    job_dir.mkdir(parents=True)
    header = "# Cron Job: fusion-calibration-report\n"
    body = ("**Job ID:** f05227128ac2\n"
            "Sample-weighted MCE: 21.81pp\n")
    (job_dir / "2026-07-13_00-03-19.md").write_text(header + body)
    (job_dir / "2026-07-13_06-09-20.md").write_text(header + body)

    ledger_path = tmp_path / "detected_anomalies.json"
    h = MockHarness()
    # Real detection over the fixture tree; only the ledger harness is mocked.
    runner.core_run(scan_root=tmp_path, ledger_path=ledger_path,
                    harness=h, quiet=True)
    assert len(h.created) == 1, "exactly ONE ticket for the two dated reports"
    book = json.loads(ledger_path.read_text())
    entries = book["entries"]
    assert len(entries) == 1, "exactly ONE ledger entry"
    only_key = next(iter(entries))
    assert only_key == "fusion_calibration::calibration.mce_high_v2::f05227128ac2", (
        f"keyed on stable rule + job id, got {only_key!r}")
    # Both dated files in one scan: first CREATES (#1), second DEDUPES (#2).
    assert entries[only_key]["occurrences"] == 2, "first creates, second dedupes"

    # Second run: detector re-scans the whole job dir (now 3 dated files),
    # each dedupes onto the SAME entry -> still exactly ONE ticket/entry,
    # no new card churn. (Comment count is an artifact of re-scanning the
    # full tree; only the single-entry invariant matters.)
    (job_dir / "2026-07-13_12-00-00.md").write_text(header + body)
    h2 = MockHarness()
    runner.core_run(scan_root=tmp_path, ledger_path=ledger_path,
                    harness=h2, quiet=True)
    assert len(h2.created) == 0, "dedupe: no new ticket created on later run"
    book2 = json.loads(ledger_path.read_text())
    assert len(book2["entries"]) == 1, "still exactly ONE entry after later run"
    assert book2["entries"][only_key]["occurrences"] > 2, "occurrences accumulated"


# ── 3d. Defect-c (t_36d0acad): resolve-on-green closes blocked cards, keeps the
#        pointer for unclosable (in_progress) ones, and reconciles lost orphans ─
class _StatusHarness:
    """Mock harness with a per-task status map for resolve/reconcile tests."""

    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.completed = []
        self.comments = []

    def status(self, task_id, board=None):
        return self.statuses.get(task_id, "ready")

    def complete(self, task_id, summary, board=None):
        self.completed.append({"id": task_id, "summary": summary})

    def comment(self, task_id, body, board=None):
        self.comments.append({"id": task_id, "body": body})

    def send_alert(self, target, message):
        return True


def _entry(ledger, key, task_id):
    ledger["entries"][key] = {"task_id": task_id, "report_class": key.split("::")[0],
                              "source": "x", "channel": "discord:fleet-reports"}


def test_resolve_closes_blocked_card_on_green():
    """Defect-c: a green report must close a stale BLOCKED card (the cited hole)."""
    book = {"version": 1, "entries": {}}
    _entry(book, "health_canary::output", "t_cited1")
    h = _StatusHarness(statuses={"t_cited1": "blocked"})
    r = ledger.resolve_anomaly(book, report_class="health_canary",
                               source="output", harness=h)
    assert r["action"] == "resolved" and r["cleared"] is True
    assert [c["id"] for c in h.completed] == ["t_cited1"], "blocked card must be completed"
    assert "health_canary::output" not in book["entries"], "entry cleared"


def test_resolve_keeps_pointer_for_in_progress_then_retries():
    """Defect-c: an unclosable (in_progress) card keeps its pointer; a later green
    run retries and closes it — never orphaned."""
    book = {"version": 1, "entries": {}}
    _entry(book, "health_canary::output", "t_working")
    h = _StatusHarness(statuses={"t_working": "in_progress"})
    r = ledger.resolve_anomaly(book, report_class="health_canary",
                               source="output", harness=h)
    assert r["cleared"] is False
    assert "health_canary::output" in book["entries"], "pointer KEPT (not orphaned)"
    assert book["entries"]["health_canary::output"].get("resolved_pending") is True
    assert h.completed == [], "in_progress card NOT auto-completed"

    # Card is now available again (a worker released it) -> next green run closes it.
    h.statuses["t_working"] = "ready"
    h.completed = []
    r2 = ledger.resolve_anomaly(book, report_class="health_canary",
                                source="output", harness=h)
    assert r2["cleared"] is True
    assert [c["id"] for c in h.completed] == ["t_working"]
    assert "health_canary::output" not in book["entries"]


def test_reconcile_closes_orphaned_green_acradr_cards():
    """Defect-c: cards the ledger has LOST (no entry) are re-located on the board
    and closed when their condition is green — reproduces the 3 cited orphans."""
    cards = [
        {"task_id": "t_bbfdfb9b", "status": "blocked",
         "title": "[ACRADR] WARNING health_canary: freshness.stale_overall",
         "body": "**ACRADR Anomaly**\nSource: `.../output/health_canary.jsonl`"},
        {"task_id": "t_505eb890", "status": "blocked",
         "title": "[ACRADR] WARNING health_canary: freshness.stale_overall",
         "body": "**ACRADR Anomaly**\nSource: `.../output/health_canary.jsonl`"},
        {"task_id": "t_f88c0f48", "status": "blocked",
         "title": "[ACRADR] WARNING health_canary: freshness.stale_overall",
         "body": "**ACRADR Anomaly**\nSource: `.../output/health_canary.jsonl`"},
    ]
    h = _StatusHarness()
    h.list_open_acradr = lambda board=None: cards
    # Current scan is GREEN for health_canary (key NOT in current_keys) but still
    # anomalous for fusion_calibration -> the orphan cards close, the active one stays.
    closed = ledger.reconcile_orphan_acradr(
        {"fusion_calibration::f05227128ac2"}, harness=h)
    closed_ids = {c["task_id"] for c in closed}
    assert closed_ids == {"t_bbfdfb9b", "t_505eb890", "t_f88c0f48"}
    assert all(c["status"] == "blocked" for c in closed)
    assert len(h.completed) == 3
    assert all("defect-c t_36d0acad" in c["summary"] for c in h.completed)


def test_reconcile_leaves_active_anomaly_cards_open():
    """Reconcile must NOT close a card whose condition is still anomalous."""
    cards = [
        {"task_id": "t_active", "status": "blocked",
         "title": "[ACRADR] WARNING health_canary: freshness.stale_overall",
         "body": "**ACRADR Anomaly**\nSource: `.../output/health_canary.jsonl`"},
    ]
    h = _StatusHarness()
    h.list_open_acradr = lambda board=None: cards
    closed = ledger.reconcile_orphan_acradr(
        {"health_canary::freshness.stale_overall::health_canary.jsonl"}, harness=h)
    assert closed == [], "anomaly still present -> card must stay open"
    assert h.completed == []


def test_reconcile_never_touches_in_progress():
    """Reconcile lists only ready/todo/blocked; an in_progress card is never listed
    and therefore never auto-closed even if its condition is green."""
    h = _StatusHarness()
    h.list_open_acradr = lambda board=None: [
        {"task_id": "t_live", "status": "in_progress",
         "title": "[ACRADR] WARNING health_canary: freshness.stale_overall",
         "body": "**ACRADR Anomaly**\nSource: `.../output/health_canary.jsonl`"},
    ]
    closed = ledger.reconcile_orphan_acradr(set(), harness=h)
    assert closed == [] and h.completed == []


# ── 3e. Stable rule identity + persisted source guard ───────────────────────
def test_distinct_rule_keys_emit_distinct_findings(tmp_path):
    source = tmp_path / "calibration.md"
    source.write_text("fresh")
    book = {"version": 2, "entries": {}}
    h = MockHarness()
    common = dict(ledger=book, report_class="fusion_calibration", source=str(source),
                  fingerprint="fp", title="[ACRADR] WARNING fusion_calibration: rule",
                  body="body", assignee="os-reviewer", channel="discord:quant",
                  harness=h)
    first = ledger.record_anomaly(rule_id="calibration.win_rate_low", **common)
    second = ledger.record_anomaly(rule_id="calibration.mce_high_v2", **common)
    repeat = ledger.record_anomaly(rule_id="calibration.win_rate_low", **common)
    assert first["action"] == "created"
    assert second["action"] == "created"
    assert repeat["action"] == "deduped" and repeat["occurrences"] == 2
    assert len(h.created) == 2 and not h.comments
    assert set(book["entries"]) == {
        "fusion_calibration::calibration.win_rate_low::calibration.md",
        "fusion_calibration::calibration.mce_high_v2::calibration.md",
    }


def test_matching_legacy_entry_migrates_without_new_ticket():
    source = "/tmp/f05227128ac2/old-report.md"
    old_key = "fusion_calibration::f05227128ac2"
    book = {"version": 1, "entries": {old_key: {
        "task_id": "t_existing", "report_class": "fusion_calibration", "source": source,
        "first_fingerprint": "calibration.mce_high_v2:old", "occurrences": 4,
    }}}
    h = MockHarness()
    result = ledger.record_anomaly(
        book, report_class="fusion_calibration", source=source,
        rule_id="calibration.mce_high_v2", fingerprint="new", title="title", body="body",
        assignee="os-reviewer", channel="discord:quant", harness=h)
    new_key = "fusion_calibration::calibration.mce_high_v2::f05227128ac2"
    assert result["action"] == "deduped" and result["task_id"] == "t_existing"
    assert not h.created and old_key not in book["entries"]
    assert book["entries"][new_key]["occurrences"] == 5


def test_stale_source_is_suppressed_with_audit_reason(tmp_path):
    source = tmp_path / "old.md"
    source.write_text("# Cron Job: fusion-calibration-report\nSample-weighted MCE: 99pp\n")
    old = time.time() - (72 * 3600)
    os.utime(source, (old, old))
    book = {"version": 2, "entries": {}}
    decision = ledger.admit_source(
        book, str(source), now=datetime.now(timezone.utc), max_age_hours=24)
    assert decision["accepted"] is False
    assert decision["reason"] == "stale_source"
    assert book["source_guards"]["old.md"]["last_reason"] == "stale_source"


def test_core_run_suppresses_stale_source_without_side_effect(tmp_path):
    source = tmp_path / "frozen.md"
    source.write_text("stale")
    old = time.time() - (72 * 3600)
    os.utime(source, (old, old))
    anomaly = _anom("fusion_calibration", "calibration.mce_high_v2", "warning",
                    "MCE 99pp", source=str(source))
    h = MockHarness()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: [anomaly])
        summary = runner.core_run(
            ledger_path=tmp_path / "ledger.json", heartbeat_file=tmp_path / "hb.txt",
            harness=h, quiet=True, source_max_age_hours=24)
    assert not h.created and not h.comments and summary["suppressed_sources"]
    assert summary["suppressed_sources"][0]["reason"] == "stale_source"


def test_high_water_suppresses_same_revision_after_restart(tmp_path):
    source = tmp_path / "current.md"
    source.write_text("fresh")
    book = {"version": 2, "entries": {}}
    first = ledger.admit_source(book, str(source), max_age_hours=24)
    assert first["accepted"] is True
    path = tmp_path / "ledger.json"
    ledger.save_ledger(book, path)
    restarted = ledger.load_ledger(path)
    second = ledger.admit_source(restarted, str(source), max_age_hours=24)
    assert second["accepted"] is False
    assert second["reason"] == "high_water_mark"
    assert restarted["source_high_water"]["current.md"]["sha256"] == first["revision"]["sha256"]


def test_stale_source_preserves_existing_ledger_and_card(tmp_path):
    source = tmp_path / "stale.md"
    source.write_text("stale")
    old = time.time() - (72 * 3600)
    os.utime(source, (old, old))
    key = ledger._entry_key("fusion_calibration", str(source), "calibration.mce_high_v2")
    book = {"version": 2, "entries": {key: {
        "task_id": "t_stale", "report_class": "fusion_calibration", "rule_id": "calibration.mce_high_v2",
        "source": str(source), "channel": "discord:quant-reports", "occurrences": 3,
    }}}
    card = {"task_id": "t_stale", "status": "blocked",
            "title": "[ACRADR] WARNING fusion_calibration: calibration.mce_high_v2",
            "body": f"Source: `{source}`"}
    h = MockHarness()
    h.open_acradr = [card]
    ledger_path = tmp_path / "ledger.json"
    ledger.save_ledger(book, ledger_path)
    anomaly = _anom("fusion_calibration", "calibration.mce_high_v2", "warning",
                    "MCE 99pp", source=str(source))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: [anomaly])
        summary = runner.core_run(
            ledger_path=ledger_path, heartbeat_file=tmp_path / "hb.txt",
            harness=h, quiet=True, source_max_age_hours=24)
    persisted = ledger.load_ledger(ledger_path)
    assert summary["suppressed_sources"][0]["reason"] == "stale_source"
    assert persisted["entries"][key]["task_id"] == "t_stale"
    assert not h.comments and not h.completed and not h.alerts
    assert h.open_acradr == [card]


def test_high_water_preserves_existing_ledger_and_card(tmp_path):
    source = tmp_path / "current.md"
    source.write_text("fresh")
    anomaly = _anom("fusion_calibration", "calibration.mce_high_v2", "warning",
                    "MCE 12pp", source=str(source))
    ledger_path = tmp_path / "ledger.json"
    first_harness = MockHarness()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: [anomaly])
        first = runner.core_run(
            ledger_path=ledger_path, heartbeat_file=tmp_path / "hb1.txt",
            harness=first_harness, quiet=True, source_max_age_hours=24)
    assert first["created_tickets"] == ["t_mock0"]
    book = ledger.load_ledger(ledger_path)
    card = {"task_id": "t_mock0", "status": "ready",
            "title": first_harness.created[0]["title"],
            "body": first_harness.created[0]["body"]}
    second_harness = MockHarness()
    second_harness.open_acradr = [card]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "run_detection", lambda *a, **k: [anomaly])
        summary = runner.core_run(
            ledger_path=ledger_path, heartbeat_file=tmp_path / "hb2.txt",
            harness=second_harness, quiet=True, source_max_age_hours=24)
    persisted = ledger.load_ledger(ledger_path)
    assert summary["suppressed_sources"][0]["reason"] == "high_water_mark"
    assert persisted["entries"]["fusion_calibration::calibration.mce_high_v2::current.md"]["task_id"] == "t_mock0"
    assert not second_harness.comments and not second_harness.completed and not second_harness.alerts
    assert second_harness.open_acradr == [card]


# ── 4. Watchdog ──────────────────────────────────────────────────────────────
def test_profile_runner_is_loaded_from_profile_execution_path():
    assert runner._THIS == _RUNNER_PATH.resolve()
    assert runner._ROOT_SCRIPTS_CANDIDATES[1].endswith("/scripts")


def test_profile_runner_cli_dry_run_executes_exact_checkout_copy(tmp_path):
    """Exercise the real profile-path CLI with only sandboxed state.

    This is intentionally a subprocess rather than an import-path assertion:
    it proves the exact copy the Jarvis cron row executes can resolve the
    checkout's canonical root scripts and complete a dry-run without a live
    kanban/Discord call.
    """
    scan_root = tmp_path / "scan"
    report_dir = scan_root / "f05227128ac2"
    report_dir.mkdir(parents=True)
    (report_dir / "report.md").write_text(
        "# Cron Job: fusion-calibration-report\n"
        "Sample-weighted MCE: 99pp\n"
    )
    ledger_path = tmp_path / "ledger.json"
    heartbeat_path = tmp_path / "heartbeat.txt"
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "ACRADR_ROOT_SCRIPTS": str(_REPO_ROOT / "scripts"),
        "ACRADR_SOURCE_MAX_AGE_HOURS": "24",
    })
    # Preserve the worker's board isolation even if this test runs in a worker.
    for name in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACE",
    ):
        env.pop(name, None)

    command = [
        sys.executable, str(_RUNNER_PATH),
        "--scan-root", str(scan_root),
        "--ledger", str(ledger_path),
        "--heartbeat-file", str(heartbeat_path),
        "--dry-run",
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            env=env, cwd=str(_REPO_ROOT), timeout=60)
    assert result.returncode == 0, result.stderr
    assert "created=1" in result.stdout
    assert "deduped=0" in result.stdout
    assert heartbeat_path.exists()
    persisted = json.loads(ledger_path.read_text())
    assert list(persisted["entries"]) == [
        "fusion_calibration::calibration.mce_high_v2::f05227128ac2"
    ]


def test_watchdog_is_monitor_only():
    watchdog = (_REPO_ROOT / "profiles/jarvis/scripts/enhanced_acradr_watchdog.py").read_text()
    assert "resume" not in watchdog.lower()
    assert "062d182994e0" not in watchdog


def test_watchdog_fresh_heartbeat_silent(tmp_path):
    hb = tmp_path / "hb.txt"
    hb.write_text("2099-01-01T02:00:00Z\n")  # far future -> fresh
    wd = _load_watchdog()
    rc = wd.main(["--heartbeat-file", str(hb), "--stale-minutes", "90",
                  "--no-alert"])
    assert rc == 0


def test_watchdog_missing_heartbeat_alerts(tmp_path):
    hb = tmp_path / "hb.txt"  # not written -> missing
    wd = _load_watchdog()
    sent = []
    wd._alert = lambda m: (sent.append(m) or True)
    rc = wd.main(["--heartbeat-file", str(hb), "--stale-minutes", "90"])
    assert rc == 2
    assert sent and "MISSING" in sent[0]


def test_watchdog_stale_heartbeat_alerts(tmp_path):
    hb = tmp_path / "hb.txt"
    hb.write_text("2026-07-10T02:00:00Z\n")  # ~24h old -> stale
    wd = _load_watchdog()
    sent = []
    wd._alert = lambda m: (sent.append(m) or True)
    rc = wd.main(["--heartbeat-file", str(hb), "--stale-minutes", "90"])
    assert rc == 2
    assert sent and "STALE" in sent[0]
