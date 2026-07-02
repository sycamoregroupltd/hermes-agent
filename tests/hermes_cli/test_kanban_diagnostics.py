"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


PHASE1_CLASSIFIER_FIXTURES = (
    Path(__file__).with_name("fixtures")
    / "kanban_failure_classifier_phase1.json"
)


# ---------------------------------------------------------------------------
# Read-only crash/failure classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "events", "runs", "log_excerpt", "expected"),
    [
        (
            _task(status="running"),
            [],
            [_run(outcome="crashed", error="worker died")],
            "PermissionDeniedError [HTTP 403] while streaming response",
            "provider_error",
        ),
        (
            _task(status="running"),
            [],
            [_run(outcome="crashed", error="worker exited cleanly (rc=0) without calling kanban_complete or kanban_block")],
            "Query: work kanban task t_x\nInitializing agent\nRateLimitError [HTTP 429]\nMessages: 1 (1 user, 0 tool calls)",
            "provider_pre_reasoning",
        ),
        (
            _task(status="running"),
            [],
            [_run(outcome="crashed", error="exited with code 1")],
            "Error: Unknown skill(s): riskfolio-lib, trading-data-analysis",
            "skill_preload_crash",
        ),
        (
            _task(status="running"),
            [],
            [_run(outcome="crashed", error="worker exited cleanly (rc=0) without calling kanban_complete or kanban_block")],
            "worker started and then exited",
            "protocol_violation",
        ),
        (
            _task(status="running"),
            [],
            [_run(outcome="crashed", error="pid 3269252 not alive")],
            "",
            "pid_not_alive_or_nonzero_crash",
        ),
        (
            _task(status="blocked", last_failure_error="workspace_kind=worktree but no workspace_path, and board 'upero' has no default_workdir"),
            [],
            [_run(outcome="spawn_failed", error="workspace_kind=worktree but no workspace_path")],
            "",
            "workspace_spawn_config_failure",
        ),
        (
            _task(status="ready", last_failure_error="Spawned=0; respawn_guarded blocker_auth"),
            [_event("commented", reason="skipped_nonspawnable terminal lane")],
            [],
            "",
            "ready_but_not_spawned",
        ),
        (
            _task(status="archived", current_run_id=7, last_failure_error="task archived with run still active"),
            [],
            [{"id": 7, "status": "running", "outcome": None, "ended_at": None}],
            "",
            "queue_metadata_leak_or_stale_active_run",
        ),
        (
            _task(status="scheduled", block_kind="dependency", last_failure_error="time-gated monitor wait until exact UTC boundary"),
            [],
            [],
            "",
            "dependency_time_gate",
        ),
        (
            _task(status="running", last_failure_error="mystery failure"),
            [],
            [_run(outcome="crashed", error="mystery failure")],
            "",
            "indeterminate",
        ),
    ],
)
def test_failure_classifier_covers_phase1_taxonomy(task, events, runs, log_excerpt, expected):
    result = kd.classify_kanban_failure(
        task, events, runs, log_excerpt=log_excerpt,
    )
    assert result.failure_class == expected
    assert result.confidence in {"low", "medium", "high"}
    assert isinstance(result.evidence_markers, list)
    assert result.safe_recovery_hint


def test_failure_classifier_replay_fixtures_cover_contract_and_stay_read_only():
    raw = PHASE1_CLASSIFIER_FIXTURES.read_text(encoding="utf-8")
    forbidden = (
        "api_key", "secret", "token", "password", "credential",
        "sk-", "xoxb-", "ghp_",
    )
    assert not any(marker in raw.lower() for marker in forbidden)
    fixtures = json.loads(raw)
    assert fixtures["source_contract_task"] == "jarvis-os/t_25e7fcb1"
    cases = fixtures["cases"]
    assert {case["expected_failure_class"] for case in cases} == set(kd.FAILURE_CLASSES)

    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    saw_safe_ambiguous_case = False

    for case in cases:
        task = case.get("task", {})
        events = case.get("events", [])
        runs = case.get("runs", [])
        log_excerpt = case.get("log_excerpt", "")
        dispatch_context = case.get("dispatch_context")
        before = copy.deepcopy((task, events, runs, dispatch_context))

        result = kd.classify_kanban_failure(
            task,
            events,
            runs,
            log_excerpt=log_excerpt,
            dispatch_context=dispatch_context,
            now=fixtures["now"],
        )

        assert copy.deepcopy((task, events, runs, dispatch_context)) == before, case["id"]
        assert set(result.to_dict()) == {
            "failure_class",
            "confidence",
            "evidence_markers",
            "safe_recovery_hint",
        }
        assert result.failure_class == case["expected_failure_class"], case["id"]
        assert confidence_rank[result.confidence] >= confidence_rank[case["min_confidence"]], case["id"]
        assert result.safe_recovery_hint.strip(), case["id"]
        assert result.evidence_markers, case["id"]
        assert any(
            expected in marker
            for expected in case["required_evidence_substrings"]
            for marker in result.evidence_markers
        ), case["id"]
        if case.get("ambiguous_mixed_evidence"):
            saw_safe_ambiguous_case = True
            assert result.failure_class == "indeterminate"
            assert result.confidence == "low"

    assert saw_safe_ambiguous_case


def test_failure_classifier_diagnostic_opt_in_surfaces_structured_payload():
    task = _task(status="running")
    runs = [_run(outcome="crashed", error="pid 123 not alive")]
    diags = kd.compute_task_diagnostics(
        task,
        [],
        runs,
        config={"enable_failure_classifier": True},
    )
    classifier = [d for d in diags if d.kind == "failure_classifier"]
    assert len(classifier) == 1
    data = classifier[0].data
    assert data["classifier_version"] == kd.FAILURE_CLASSIFIER_VERSION
    assert data["failure_class"] == "pid_not_alive_or_nonzero_crash"
    assert data["safe_recovery_hint"]


def test_failure_classifier_ignores_instructional_task_text():
    task = _task(
        status="running",
        block_kind="needs_input",
        title="Implement dependency_time_gate classifier",
        body="Acceptance mentions provider_error and dependency_time_gate taxonomy names.",
        current_run_id=123,
    )

    result = kd.classify_kanban_failure(task, [], [], log_excerpt="")

    assert result.failure_class == "indeterminate"
    diags = kd.compute_task_diagnostics(
        task,
        [],
        [],
        config={"enable_failure_classifier": True},
    )
    assert [d.kind for d in diags if d.kind == "failure_classifier"] == []


def test_failure_classifier_does_not_treat_live_running_run_as_stale_queue_metadata():
    task = _task(status="running", current_run_id=123)
    runs = [{"id": 123, "status": "running", "outcome": None, "ended_at": None}]

    result = kd.classify_kanban_failure(
        task,
        [],
        runs,
        log_excerpt="context included current_run_id=123 and stale active run text for a live run",
    )

    assert result.failure_class == "indeterminate"
    diags = kd.compute_task_diagnostics(
        task,
        [],
        runs,
        config={
            "enable_failure_classifier": True,
            "log_excerpt": "context included current_run_id=123 and stale active run text for a live run",
        },
    )
    assert [d.kind for d in diags if d.kind == "failure_classifier"] == []


def test_failure_classifier_accepts_dispatch_context_for_ready_skip_bucket():
    result = kd.classify_kanban_failure(
        _task(status="ready"),
        [],
        [],
        dispatch_context={"skip_bucket": "skipped_per_profile_capped", "spawned": 0},
    )

    assert result.to_dict().keys() == {
        "failure_class",
        "confidence",
        "evidence_markers",
        "safe_recovery_hint",
    }
    assert result.failure_class == "ready_but_not_spawned"
    assert any("skipped_per_profile_capped" in marker for marker in result.evidence_markers)


def test_failure_classifier_uses_contract_precedence_for_workspace_before_dependency():
    result = kd.classify_kanban_failure(
        _task(
            status="scheduled",
            block_kind="dependency",
            last_failure_error="workspace_kind=worktree but no workspace_path, and board has no default_workdir",
        ),
        [],
        [_run(outcome="spawn_failed", error="workspace_kind=worktree but no workspace_path")],
    )

    assert result.failure_class == "workspace_spawn_config_failure"


def test_failure_classifier_uses_contract_precedence_for_provider_before_dependency():
    result = kd.classify_kanban_failure(
        _task(
            status="scheduled",
            block_kind="dependency",
            last_failure_error="RateLimitError [HTTP 429] before reasoning",
        ),
        [_event("claimed"), _event("spawned")],
        [_run(outcome="provider_error_pre_reasoning", error="RateLimitError [HTTP 429]")],
        log_excerpt="Messages: 1 (1 user, 0 tool calls)",
    )

    assert result.failure_class == "provider_pre_reasoning"

# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------


def test_hallucinated_cards_fires_on_blocked_event():
    task = _task(status="ready")
    events = [
        _event("created", ts=100),
        _event("completion_blocked_hallucination", ts=200,
               phantom_cards=["t_bad1", "t_bad2"],
               verified_cards=["t_good1"]),
    ]
    # ``now=300`` keeps the synthetic event timestamps in scope without
    # tripping the stranded_in_ready rule (events are 100/200 epoch
    # which time.time() would treat as ~50yr old).
    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    halluc = [d for d in diags if d.kind == "hallucinated_cards"]
    assert len(halluc) == 1
    d = halluc[0]
    assert d.severity == "error"
    assert d.data["phantom_ids"] == ["t_bad1", "t_bad2"]
    # Generic recovery actions always available; comment action too.
    kinds = [a.kind for a in d.actions]
    assert "comment" in kinds
    assert "reassign" in kinds


def test_hallucinated_cards_clears_on_subsequent_completion():
    task = _task(status="done")
    events = [
        _event("completion_blocked_hallucination", ts=100, phantom_cards=["t_x"]),
        _event("completed", ts=200, summary="retry worked"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_prose_phantom_refs_fires_after_clean_completion():
    # Prose scan emits its event AFTER the completed event in the DB
    # path, but a subsequent clean completion clears it. Phantom id
    # must be valid hex — the scanner regex is ``t_[a-f0-9]{8,}``.
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="referenced t_bad", result_len=0),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_deadbeef99"], source="completion_summary"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert len(diags) == 1
    assert diags[0].kind == "prose_phantom_refs"
    assert diags[0].severity == "warning"
    assert diags[0].data["phantom_refs"] == ["t_deadbeef99"]


def test_prose_phantom_refs_clears_on_later_clean_edit():
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="bad"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_ffff0000cc"]),
        _event("edited", ts=200, fields=["result", "summary"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_repeated_failures_fires_at_threshold_on_spawn():
    """A task with multiple spawn_failed runs gets a spawn-flavoured
    diagnostic (title mentions 'spawn', suggested action is ``doctor``).
    """
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [
        _run(outcome="spawn_failed", run_id=1),
        _run(outcome="spawn_failed", run_id=2),
        _run(outcome="spawn_failed", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.severity == "error"
    # CLI hints are what operators actually need here.
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("doctor" in s for s in suggested)


def test_repeated_failures_fires_on_timeout_loop():
    """The rule surfaces for timeout loops too — that's the point of
    unifying the counter. Suggested action is 'check logs', not
    'fix profile'."""
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [
        _run(outcome="timed_out", run_id=1),
        _run(outcome="timed_out", run_id=2),
        _run(outcome="timed_out", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.data["most_recent_outcome"] == "timed_out"
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("log" in s.lower() for s in suggested)


def test_repeated_failures_escalates_to_critical():
    task = _task(consecutive_failures=6, last_failure_error="boom")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert diags[0].severity == "critical"


def test_repeated_failures_below_threshold_silent():
    task = _task(consecutive_failures=1)
    assert kd.compute_task_diagnostics(task, [], []) == []


def test_repeated_failures_default_matches_dispatcher_failure_limit():
    """Default dispatcher auto-blocks at 2 failures, so diagnostics must
    also surface at 2 instead of waiting for the stale threshold of 3.
    """
    task = _task(status="blocked", consecutive_failures=2,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [_run(outcome="timed_out", run_id=1)]
    diags = kd.compute_task_diagnostics(task, [], runs)
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    d = repeated[0]
    assert d.data["failure_threshold"] == 2
    assert d.data["failure_limit"] == 2
    assert "default 5" not in d.detail
    assert "configured for 2" in d.detail


def test_repeated_failures_derives_threshold_from_kanban_failure_limit():
    task = _task(status="ready", consecutive_failures=2,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    assert kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    ) == []

    task = _task(status="blocked", consecutive_failures=4,
                 last_failure_error="Profile 'debugger' does not exist")
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 4
    assert repeated[0].data["failure_limit"] == 4


def test_repeated_failures_explicit_threshold_overrides_failure_limit():
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 5, "failure_threshold": 3}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 3
    assert repeated[0].data["failure_limit"] == 5


def test_config_from_kanban_config_preserves_explicit_diagnostics_threshold():
    cfg = kd.config_from_kanban_config({
        "failure_limit": 5,
        "diagnostics": {"failure_threshold": 3},
    })
    assert cfg["failure_threshold"] == 3
    assert cfg["failure_limit"] == 5


def test_repeated_crashes_counts_trailing_streak_only():
    task = _task(status="ready", assignee="crashy")
    runs = [
        _run(outcome="completed", run_id=1),
        _run(outcome="crashed", run_id=2, error="OOM"),
        _run(outcome="crashed", run_id=3, error="OOM again"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_crashes"
    # 2 consecutive crashes at the end → default threshold 2 → error severity.
    assert d.severity == "error"
    assert d.data["consecutive_crashes"] == 2


def test_repeated_crashes_breaks_on_recent_success():
    task = _task(status="ready", assignee="fixed")
    runs = [
        _run(outcome="crashed", run_id=1),
        _run(outcome="crashed", run_id=2),
        _run(outcome="completed", run_id=3),
    ]
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_repeated_crashes_escalates_on_many_crashes():
    task = _task(status="ready", assignee="x")
    runs = [_run(outcome="crashed", run_id=i) for i in range(1, 6)]  # 5 in a row
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert diags[0].severity == "critical"


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48


def test_stuck_in_blocked_silent_with_recent_comment():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48),
        _event("commented", ts=now - 3600 * 2, author="human"),
    ]
    assert kd.compute_task_diagnostics(task, events, [], now=now) == []


def test_stuck_in_blocked_silent_when_not_blocked():
    task = _task(status="ready")
    events = [_event("blocked", ts=1000)]
    assert kd.compute_task_diagnostics(task, events, [], now=9999999) == []


def test_repeated_crashes_surfaces_actual_error_in_title():
    """The title should lead with the actual error text so operators
    see WHAT broke (e.g. rate-limit, auth, OOM) without opening logs.
    """
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error="openai: 429 Too Many Requests"),
        _run(outcome="crashed", run_id=2, error="openai: 429 Too Many Requests"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert "429" in d.title
    assert "Too Many Requests" in d.title
    # Full error in detail.
    assert "429 Too Many Requests" in d.detail


def test_repeated_crashes_no_error_fallback_title():
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error=None),
        _run(outcome="crashed", run_id=2, error=None),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert "no error recorded" in diags[0].title


def test_repeated_failures_surfaces_actual_error_in_title():
    task = _task(consecutive_failures=5,
                 last_failure_error="insufficient_quota: billing limit reached")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert len(diags) == 1
    d = diags[0]
    assert "insufficient_quota" in d.title or "billing limit" in d.title
    assert "insufficient_quota" in d.detail


def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------


def test_diagnostics_sorted_critical_first():
    """A task with both a critical (many spawn failures) and a warning
    (prose phantoms) diagnostic should list the critical one first."""
    task = _task(status="done", consecutive_failures=10,
                 last_failure_error="nope")
    events = [
        _event("completed", ts=100, summary="referenced t_missing"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_missing11"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    kinds = [d.kind for d in diags]
    assert kinds[0] == "repeated_failures"  # critical
    assert "prose_phantom_refs" in kinds


# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------


def test_broken_rule_is_isolated(monkeypatch):
    def _bad_rule(task, events, runs, now, cfg):
        raise RuntimeError("synthetic rule bug")

    # Insert a broken rule at the front of the registry; subsequent
    # rules should still run and produce their diagnostics.
    monkeypatch.setattr(kd, "_RULES", [_bad_rule] + kd._RULES)

    task = _task(consecutive_failures=5, last_failure_error="e")
    diags = kd.compute_task_diagnostics(task, [], [])
    # The broken rule silently drops, the real one still fires.
    kinds = [d.kind for d in diags]
    assert "repeated_failures" in kinds


# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"


def test_stranded_in_ready_silent_below_threshold():
    """A ready task only 10 min old should NOT fire."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 10 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_non_ready_status():
    """Tasks not in ready status are out of scope (running tasks have
    their own crash / failure rules)."""
    now = 100_000
    for status in ("running", "blocked", "done", "todo", "triage"):
        task = _task(status=status, assignee="demo")
        events = [_event("created", ts=now - 6 * 3600)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        assert [d for d in diags if d.kind == "stranded_in_ready"] == [], status


def test_stranded_in_ready_skips_unassigned_tasks():
    """Empty assignee = `skipped_unassigned` on the dispatcher already.
    Don't double-flag here."""
    now = 100_000
    task = _task(status="ready", assignee="", claim_lock=None)
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_claimed_tasks():
    """A live claim_lock means a worker is on it — even an old one. Don't
    second-guess: the run-level liveness signal owns that decision."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", claim_lock="run_xyz",
    )
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_uses_latest_ready_transition():
    """When multiple ready-transition events exist, the rule should
    age-from the most recent — a task reclaimed 20 min ago is NOT
    stranded for 6h even if it was first created 6h ago."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [
        _event("created", ts=now - 6 * 3600),       # 6 h ago
        _event("reclaimed", ts=now - 20 * 60),      # 20 min ago — wins
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_severity_escalates_with_age():
    """warning → error → critical at 2x and 6x threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    # Default threshold = 1800s.
    cases = [
        (45 * 60, "warning"),    # 1.5x → warning
        (90 * 60, "error"),      # 3x → error
        (4 * 3600, "critical"),  # 8x → critical
    ]
    for age, expected in cases:
        events = [_event("created", ts=now - age)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1, f"age={age}"
        assert stranded[0].severity == expected, (
            f"age={age} expected {expected}, got {stranded[0].severity}"
        )


def test_stranded_in_ready_respects_config_override():
    """Config override changes the threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [_event("created", ts=now - 10 * 60)]  # 10 min
    # Default 30 min — wouldn't fire.
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []
    # Lower the threshold to 5 min — now it fires.
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
        config={"stranded_threshold_seconds": 5 * 60},
    )
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


def test_stranded_in_ready_falls_back_to_created_at():
    """When events have no ready-transition kind, the rule falls back
    to the task's ``created_at`` so an ancient stranded task isn't
    invisible just because its events got pruned."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", created_at=now - 4 * 3600,
    )
    # No qualifying events.
    events = [_event("commented", ts=now - 100)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].data["age_seconds"] == 4 * 3600


def test_stranded_in_ready_works_on_real_db_row(kanban_home):
    """Round-trip through real kanban_db.connect() — confirms the rule
    works on sqlite3.Row objects, not just dicts."""
    import time as _t
    conn = kb.connect()
    try:
        # Create a task and force its created_at into the past.
        tid = kb.create_task(conn, title="stranded one", assignee="ghost")
        old_ts = int(_t.time()) - 90 * 60  # 90 min old
        conn.execute(
            "UPDATE tasks SET status = 'ready', created_at = ? WHERE id = ?",
            (old_ts, tid),
        )
        conn.commit()

        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall())
        # Override created event timestamps too so age calc lines up.
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (old_ts, tid),
        )
        conn.commit()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ?", (tid,),
        ).fetchall())

        diags = kd.compute_task_diagnostics(task_row, events, [])
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1
        assert stranded[0].data["assignee"] == "ghost"
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")


def test_triage_aux_unavailable_silent_without_config_context():
    """Low-level callers passing no config dict should not see this rule."""
    diags = kd.compute_task_diagnostics(_triage_task(), [], [])
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_main_model_visible():
    """Default `provider: auto` falls back to the main model — no warning."""
    config = {
        "auxiliary": {},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_decomposer_explicit():
    """User explicitly configured decomposer → no warning, even without main."""
    config = {
        "auxiliary": {
            "kanban_decomposer": {"provider": "openrouter", "model": "qwen/qwen3"},
        },
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_fires_auto_decompose_on_no_fallback():
    """auto_decompose=True, no decomposer, no main model → warn about decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert d.severity == "warning"
    assert "decomposer" in d.title.lower()
    assert d.data["auto_decompose"] is True
    assert d.data["primary_slot"] == "auxiliary.kanban_decomposer"
    suggested = [a for a in d.actions if a.suggested]
    assert suggested
    assert "auxiliary.kanban_decomposer" in suggested[0].payload["command"]


def test_triage_aux_unavailable_fires_auto_decompose_off_points_at_specifier():
    """auto_decompose=False → primary is specifier, not decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": False},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert "specifier" in d.title.lower()
    assert d.data["auto_decompose"] is False
    assert d.data["primary_slot"] == "auxiliary.triage_specifier"
    # And it should offer the manual specify command as an action
    labels = [a.label for a in d.actions]
    assert any("hermes kanban specify" in l for l in labels)


def test_triage_aux_unavailable_skips_non_triage_tasks():
    config = {"auxiliary": {}, "kanban": {"auto_decompose": True}}
    task = _task(status="todo")
    diags = kd.compute_task_diagnostics(task, [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_status_recognises_auto_default_as_not_explicit():
    """Default `provider: auto` with empty fields → not 'explicit'."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": ""},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is False


def test_triage_aux_status_recognises_explicit_model_only():
    """Even with provider=auto, a non-empty model counts as explicit."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": "qwen/qwen3"},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is True


def test_config_from_runtime_config_carries_aux_and_model():
    cfg = kd.config_from_runtime_config({
        "kanban": {"failure_limit": 5, "auto_decompose": False},
        "auxiliary": {"kanban_decomposer": {"provider": "openrouter"}},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
    })
    assert cfg["failure_threshold"] == 5
    assert cfg["kanban"]["auto_decompose"] is False
    assert cfg["auxiliary"]["kanban_decomposer"]["provider"] == "openrouter"
    assert cfg["model"]["default"] == "qwen/qwen3"


def test_config_from_runtime_config_handles_empty_input():
    assert kd.config_from_runtime_config(None) == {}
    assert kd.config_from_runtime_config({}) == {}


def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
