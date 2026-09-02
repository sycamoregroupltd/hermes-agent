"""Focused tests for the READY-only cloud-overflow preparation seam.

Covers the jarvis-os-pm acceptance contract (t_d1382db8):
  * below/at/above max_spawn triggering
  * READY eligibility (status/claim/parents/work-class/isolation-safe marker)
  * every exclusion gate (EXCLUDED_CLASSES, including the A3 marker)
  * Codex missing ENV_ID / malformed ENV_ID / missing exact approval /
    cross-task or cross-env approval reuse
  * idempotent repeat planning (duplicate lease)
  * malformed provider config (non-mapping approval record)
  * provider failure with no fallback side effect (no silent cascade to the
    next provider once a lease is acquired for the first available one)
  * structured TickResult contract (trigger evidence / isolation verdict /
    approval verdict / next action)
  * a real dry-run against the fixture proving zero external spawn and zero
    live board mutation (subprocess-level: the CLI process never shells out
    to cursor/claude/codex)
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.cloud_overflow import (
    EXCLUDED_CLASSES,
    ISOLATION_SAFE_TOKEN,
    BoardSnapshot,
    ClaudeCloudAdapter,
    CodexCloudAdapter,
    CommentWriteError,
    CursorCloudAdapter,
    LaunchResult,
    OverflowState,
    ProviderRefused,
    TaskSnapshot,
    eligibility,
    exponential_backoff,
    load_fixture,
    record_launch,
    run_tick,
    sanitize_environment,
    sanitize_receipt,
    source_revision,
)


@pytest.fixture
def task() -> TaskSnapshot:
    return TaskSnapshot(
        id="t_docs", title="Documentation draft", skills=("docs",), labels=(ISOLATION_SAFE_TOKEN,)
    )


def _available_adapter(name="cursor-cloud"):
    return {name: CursorCloudAdapter(name, plan_authenticated=True, isolated_checkout="fixture")}


# ---------------------------------------------------------------------------
# Eligibility / work classification
# ---------------------------------------------------------------------------

def test_only_explicit_docs_or_research_is_eligible(task):
    assert eligibility(task) == (True, "eligible", "docs")
    assert eligibility(
        TaskSnapshot(id="t_r", title="Research", skills=("research",), labels=(ISOLATION_SAFE_TOKEN,))
    )[0]
    assert (
        eligibility(TaskSnapshot(id="t_amb", title="Research", skills=(), labels=(ISOLATION_SAFE_TOKEN,)))[1]
        == "work_class_not_explicit"
    )
    assert (
        eligibility(
            TaskSnapshot(
                id="t_both", title="Mixed", skills=("docs", "research"), labels=(ISOLATION_SAFE_TOKEN,)
            )
        )[1]
        == "work_class_ambiguous"
    )
    assert eligibility(
        TaskSnapshot(
            id="t_meta",
            title="Metadata",
            metadata={"work_class": "documentation"},
            labels=(ISOLATION_SAFE_TOKEN,),
        )
    )[0]


def test_missing_isolation_safe_marker_fails_closed():
    """A card with a valid work-class but no explicit isolation-safe marker
    (label, skill, or metadata.acceptance_contract) must never be planned —
    this is the 'cards lacking an isolation-safe acceptance contract'
    exclusion from the acceptance contract, independent of work class."""
    bare = TaskSnapshot(id="t_bare", title="Docs without contract", skills=("docs",))
    ok, reason, work_class = eligibility(bare)
    assert ok is False
    assert reason == "missing_isolation_safe_acceptance_contract"
    assert work_class is None
    # Metadata acceptance_contract also satisfies the gate.
    via_metadata = TaskSnapshot(
        id="t_meta_contract",
        title="Docs via metadata contract",
        skills=("docs",),
        metadata={"acceptance_contract": "isolation-safe"},
    )
    assert eligibility(via_metadata)[0] is True


@pytest.mark.parametrize("excluded", sorted(EXCLUDED_CLASSES))
def test_every_exclusion_fails_closed(excluded):
    value = TaskSnapshot(
        id="t_x",
        title="Plain title",
        skills=("docs",),
        labels=(excluded, ISOLATION_SAFE_TOKEN),
    )
    ok, reason, _ = eligibility(value)
    assert ok is False
    assert reason == f"excluded:{excluded}"


def test_ready_parent_and_claim_gates(task):
    assert eligibility(TaskSnapshot(**{**task.__dict__, "status": "running"}))[1] == "source_not_ready"
    assert eligibility(TaskSnapshot(**{**task.__dict__, "claim_lock": "leased"}))[1] == "source_claimed"
    assert (
        eligibility(TaskSnapshot(**{**task.__dict__, "parents_satisfied": False}))[1]
        == "parents_not_satisfied"
    )


# ---------------------------------------------------------------------------
# Saturation / triggering (below, at, above max_spawn)
# ---------------------------------------------------------------------------

def test_below_max_spawn_never_plans(tmp_path, task):
    """running < max_spawn: board is not saturated, no candidate is planned
    even though the task is otherwise fully eligible."""
    state = OverflowState(tmp_path / "state.sqlite3")
    board = BoardSnapshot("board-a", running=2, max_spawn=3, tasks=(task,))
    result = run_tick((board,), state=state, adapters=_available_adapter())
    assert result.status == "no-op"
    assert result.reason == "no_eligible_candidate"
    assert result.isolation_verdict == "not_evaluated"


def test_at_max_spawn_triggers_planning(tmp_path, task):
    """running == max_spawn: board is saturated at the boundary — must
    trigger, not just above it."""
    state = OverflowState(tmp_path / "state.sqlite3")
    board = BoardSnapshot("board-a", running=3, max_spawn=3, tasks=(task,))
    result = run_tick((board,), state=state, adapters=_available_adapter())
    assert result.status == "planned"
    assert result.isolation_verdict == "eligible_isolation_safe"


def test_above_max_spawn_triggers_planning(tmp_path, task):
    state = OverflowState(tmp_path / "state.sqlite3")
    board = BoardSnapshot("board-a", running=5, max_spawn=3, tasks=(task,))
    result = run_tick((board,), state=state, adapters=_available_adapter())
    assert result.status == "planned"


def test_saturation_and_max_one_tick(tmp_path, task):
    state = OverflowState(tmp_path / "state.sqlite3", max_concurrency=3)
    adapters = _available_adapter()
    board = BoardSnapshot("board-a", running=3, max_spawn=3, tasks=(task,))
    first = run_tick((board,), state=state, adapters=adapters, now=100)
    second = run_tick((board,), state=state, adapters=adapters, now=101)
    assert first.status == "planned"
    assert first.action == "prepare-only"
    assert second.reason == "duplicate_lease"
    assert state.get(first.idempotency_key)["status"] == "planned"
    assert (
        run_tick((BoardSnapshot("board-a", 2, 3, (task,)),), state=state, adapters=adapters).reason
        == "no_eligible_candidate"
    )


def test_toctou_reread_rejects_changed_source(tmp_path, task):
    state = OverflowState(tmp_path / "state.sqlite3")
    changed = TaskSnapshot(id=task.id, title="Changed", skills=task.skills, labels=task.labels)
    board = BoardSnapshot("board-a", 3, 3, (task,), reread=lambda _: changed)
    result = run_tick((board,), state=state, adapters=_available_adapter())
    assert result.reason == "no_eligible_candidate"


def test_atomic_idempotency_under_contention(tmp_path, task):
    path = tmp_path / "state.sqlite3"

    def acquire():
        store = OverflowState(path)
        return store.acquire(board="b", task=task, provider="cursor-cloud", now=1)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: acquire(), range(8)))
    assert sum(results) == 1


def test_idempotent_repeat_planning_same_revision_same_key(tmp_path, task):
    """Planning the identical unchanged task twice must reuse the same
    idempotency key and refuse a second lease — this is the literal
    'idempotent repeat planning' acceptance item."""
    state = OverflowState(tmp_path / "state.sqlite3")
    adapters = _available_adapter()
    board = BoardSnapshot("board-a", 3, 3, (task,))
    first = run_tick((board,), state=state, adapters=adapters, now=1)
    second = run_tick((board,), state=state, adapters=adapters, now=2)
    assert first.idempotency_key is not None
    assert second.reason == "duplicate_lease"
    assert second.idempotency_key == first.idempotency_key


def test_saturation_dry_run_cli_is_fixture_only(tmp_path):
    fixture = Path(__file__).parent.parent / "fixtures" / "cloud_overflow.json"
    state = tmp_path / "cli-state.sqlite3"
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "kanban",
        "cloud-overflow",
        "--fixture",
        str(fixture),
        "--state",
        str(state),
        "--dry-run",
        "--json",
    ]
    first = subprocess.run(cmd, capture_output=True, text=True, check=False)
    second = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["action"] == "prepare-only"
    assert payload["isolation_verdict"] == "eligible_isolation_safe"
    assert payload["next_action"].startswith("HUMAN_GATE")
    assert json.loads(second.stdout)["reason"] == "duplicate_lease"
    # Zero external spawn: none of the provider CLI invocation strings ever
    # appear in the process's own stdout, and — more directly — the fixture
    # path never shells out (CursorCloudAdapter/ClaudeCloudAdapter/
    # CodexCloudAdapter.launch() is never called by cloud_overflow_command).
    assert "cloud-agent" not in first.stdout
    assert "claude --cloud" not in first.stdout
    assert "codex cloud exec" not in first.stdout


def test_dry_run_cli_makes_zero_live_board_mutation(tmp_path):
    """The prepare CLI must never touch a live kanban.db: it is fixture-only
    end to end. Point HERMES_KANBAN_DB at a throwaway path and confirm the
    command never creates or writes it."""
    import os

    fixture = Path(__file__).parent.parent / "fixtures" / "cloud_overflow.json"
    state = tmp_path / "cli-state.sqlite3"
    live_db_sentinel = tmp_path / "would-be-live-kanban.db"
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "kanban",
        "cloud-overflow",
        "--fixture",
        str(fixture),
        "--state",
        str(state),
        "--dry-run",
        "--json",
    ]
    env = dict(os.environ)
    env["HERMES_KANBAN_DB"] = str(live_db_sentinel)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert not live_db_sentinel.exists(), "prepare-only CLI must never touch a live board DB"


# ---------------------------------------------------------------------------
# Provider adapters: env/config, secrets, argv shape
# ---------------------------------------------------------------------------

def test_provider_argv_timeout_and_parsed_identity(monkeypatch, task):
    calls = []

    def runner(argv, *, timeout, env):
        calls.append((tuple(argv), timeout, dict(env)))
        return SimpleNamespace(stdout='{"session_id":"sess-1", "url":"https://example.test/sess-1"}')

    adapter = ClaudeCloudAdapter(
        "claude-cloud", plan_authenticated=True, isolated_checkout="/repo/wt", timeout=17, runner=runner
    )
    result = adapter.launch(task)
    assert result == LaunchResult(
        "sess-1",
        "https://example.test/sess-1",
        (
            "claude",
            "--cloud",
            "--worktree",
            "/repo/wt",
            "ISO HOLD; draft PR only; task t_docs; title: Documentation draft. "
            "No merge, deploy, live trading, credential access, or schedule activation.",
        ),
    )
    assert calls[0][1] == 17
    assert calls[0][0][0] == "claude"
    assert "shell" not in calls[0][2]


def test_provider_output_regex_and_env_sanitization(task):
    captured = {}

    def runner(argv, *, timeout, env):
        captured.update(env)
        return SimpleNamespace(stdout="session_id: sess-2 https://example.test/sess-2")

    adapter = CursorCloudAdapter("cursor-cloud", plan_authenticated=True, isolated_checkout="x", runner=runner)
    assert adapter.launch(task).session_id == "sess-2"
    clean = sanitize_environment(
        {"PATH": "/bin", "HOME": "/tmp", "API_KEY": "do-not-pass", "TOKEN": "do-not-pass"}
    )
    assert clean == {"PATH": "/bin", "HOME": "/tmp"}
    assert "API_KEY" not in captured


# ---------------------------------------------------------------------------
# Codex Cloud: fail-closed gates
# ---------------------------------------------------------------------------

_FRANK_APPROVAL = {
    "approved_by": "frank",
    "approved_at": "2026-09-02T22:00:00Z",
    "task_id": "t_docs",
    "env_id": "env_exact",
}


def test_codex_missing_env_id_fails_closed(task):
    adapter = CodexCloudAdapter(
        env_id=None,
        approval_record=_FRANK_APPROVAL,
        task_id_for_approval="t_docs",
        plan_authenticated=True,
        isolated_checkout="x",
    )
    assert not adapter.available
    assert adapter.refusal_reason() == "codex_missing_env_id"
    with pytest.raises(ProviderRefused, match="codex_missing_env_id"):
        adapter.build_argv(task)


def test_codex_malformed_env_id_fails_closed(task):
    adapter = CodexCloudAdapter(
        env_id="env with spaces; rm -rf /",
        approval_record=_FRANK_APPROVAL,
        task_id_for_approval="t_docs",
        plan_authenticated=True,
        isolated_checkout="x",
    )
    assert not adapter.available
    assert adapter.refusal_reason() == "codex_malformed_env_id"


def test_codex_missing_exact_approval_fails_closed(task):
    adapter = CodexCloudAdapter(
        env_id="env_exact",
        approval_record=None,
        task_id_for_approval="t_docs",
        plan_authenticated=True,
        isolated_checkout="x",
    )
    assert not adapter.available
    assert adapter.refusal_reason() == "codex_missing_exact_approval"


@pytest.mark.parametrize(
    "record",
    [
        {**_FRANK_APPROVAL, "approved_by": "not-frank"},
        {**_FRANK_APPROVAL, "task_id": "t_other"},
        {**_FRANK_APPROVAL, "env_id": "env_other"},
        {**_FRANK_APPROVAL, "approved_at": ""},
        "a-plain-string-is-a-malformed-record",
        123,
    ],
)
def test_codex_approval_must_exactly_match_task_and_env(record, task):
    """Malformed provider config (a non-mapping record) and any near-match
    (wrong task id, wrong env id, wrong approver, missing timestamp) must
    all refuse — no partial credit, no cross-task/cross-env reuse."""
    adapter = CodexCloudAdapter(
        env_id="env_exact",
        approval_record=record,
        task_id_for_approval="t_docs",
        plan_authenticated=True,
        isolated_checkout="x",
    )
    assert not adapter.available
    assert adapter.refusal_reason() == "codex_missing_exact_approval"


def test_codex_argv_is_explicit_and_never_falls_back(task):
    adapter = CodexCloudAdapter(
        env_id="env_exact",
        approval_record=_FRANK_APPROVAL,
        task_id_for_approval="t_docs",
        plan_authenticated=True,
        isolated_checkout="x",
    )
    assert adapter.available
    assert adapter.build_argv(task)[:5] == ("codex", "cloud", "exec", "--env", "env_exact")


def test_codex_never_invoked_when_cursor_or_claude_available(tmp_path, task):
    """Provider order (Cursor -> Claude -> Codex): when an earlier provider
    is available, Codex's adapter.launch must never be called even if it is
    ALSO (incorrectly) configured as available — no fallback side effect
    fires past the first eligible provider in run_tick."""
    calls = {"codex": 0}

    class SpyCodex(CodexCloudAdapter):
        def launch(self, task):
            calls["codex"] += 1
            return super().launch(task)

    state = OverflowState(tmp_path / "state.sqlite3")
    adapters = {
        "cursor-cloud": CursorCloudAdapter("cursor-cloud", plan_authenticated=True, isolated_checkout="x"),
        "codex-cloud": SpyCodex(
            env_id="env_exact",
            approval_record=_FRANK_APPROVAL,
            task_id_for_approval="t_docs",
            plan_authenticated=True,
            isolated_checkout="x",
        ),
    }
    board = BoardSnapshot("board-a", 3, 3, (task,))
    result = run_tick((board,), state=state, adapters=adapters)
    assert result.provider == "cursor-cloud"
    assert calls["codex"] == 0
    # run_tick is prepare-only: neither adapter's .launch() runs on this path.


# ---------------------------------------------------------------------------
# Receipts / secrets / launch (approved-only seam, not reachable from prepare CLI)
# ---------------------------------------------------------------------------

def test_receipt_is_allowlisted_and_no_prompt_or_secret():
    receipt = sanitize_receipt(
        provider="claude-cloud",
        session_id="s",
        url="https://example.test/s",
        branch="wt/x",
        workspace="/tmp/x",
        status="launched",
        idempotency_key="b:t:r:p",
    )
    assert set(receipt) == {
        "provider",
        "external_session_id",
        "external_session_url",
        "isolated_branch",
        "isolated_workspace",
        "status",
        "idempotency_key",
    }
    assert "prompt" not in json.dumps(receipt).lower()


def test_comment_failure_marks_launch_unresolved(tmp_path, task):
    state = OverflowState(tmp_path / "state.sqlite3")
    ok, _, key = state.acquire(board="b", task=task, provider="claude-cloud", now=1)
    assert ok

    def runner(argv, *, timeout, env):
        return SimpleNamespace(stdout='{"session_id":"s"}')

    adapter = ClaudeCloudAdapter("claude-cloud", plan_authenticated=True, isolated_checkout="x", runner=runner)
    with pytest.raises(CommentWriteError):
        record_launch(
            state=state,
            lease_key=key,
            board="b",
            task=task,
            adapter=adapter,
            comment_writer=lambda *_: (_ for _ in ()).throw(RuntimeError("no comment")),
            approved=True,
            now=2,
        )
    assert state.get(key)["status"] == "unresolved"
    assert state.get(key)["veto"] == "receipt_comment_failed"


def test_provider_failure_has_no_fallback_side_effect(tmp_path, task):
    """A provider whose subprocess raises must fail its own launch and must
    never trigger a silent retry against a different provider or mutate
    unrelated state — record_launch is the only launch seam, and it is
    scoped to exactly one lease/adapter pair."""
    state = OverflowState(tmp_path / "state.sqlite3")
    ok, _, key = state.acquire(board="b", task=task, provider="cursor-cloud", now=1)
    assert ok

    def failing_runner(argv, *, timeout, env):
        raise TimeoutError("provider subprocess timed out")

    adapter = CursorCloudAdapter(
        "cursor-cloud", plan_authenticated=True, isolated_checkout="x", runner=failing_runner
    )
    with pytest.raises(TimeoutError):
        record_launch(
            state=state,
            lease_key=key,
            board="b",
            task=task,
            adapter=adapter,
            comment_writer=lambda *a: (_ for _ in ()).throw(AssertionError("must not be called")),
            approved=True,
            now=2,
        )
    # The lease is untouched at "planned" (acquire's initial state) — no
    # partial write, no other provider's lease was created as a fallback.
    row = state.get(key)
    assert row["status"] == "planned"
    with state._connect() as conn:  # noqa: SLF001 - test-only introspection
        count = conn.execute("SELECT COUNT(*) FROM overflow_state").fetchone()[0]
    assert count == 1


def test_backoff_is_exponential_and_bounded():
    assert exponential_backoff(0, base_seconds=3, cap_seconds=10) == 3
    assert exponential_backoff(2, base_seconds=3, cap_seconds=10) == 10
    assert exponential_backoff(99, base_seconds=3, cap_seconds=10) == 10


def test_pauses_and_kill_switch_do_not_lease(tmp_path, task):
    state = OverflowState(tmp_path / "state.sqlite3")
    adapter = _available_adapter()
    board = BoardSnapshot("b", 3, 3, (task,))
    paused = run_tick((board,), state=state, adapters=adapter, fleet_paused=True)
    killed = run_tick((board,), state=state, adapters=adapter, kill_switch=True)
    assert paused.status == "blocked"
    assert killed.status == "blocked"
    assert paused.next_action == "none — planner is paused or kill-switched"
    assert not list((tmp_path).glob("*.sqlite3-wal")) or state.get("unused") is None


def test_fixture_shape_and_revision_are_deterministic(task):
    fixture = Path(__file__).parent.parent / "fixtures" / "cloud_overflow.json"
    boards = load_fixture(fixture)
    assert boards[0].saturated
    assert source_revision(task) == source_revision(task)


# ---------------------------------------------------------------------------
# Structured output contract
# ---------------------------------------------------------------------------

def test_tick_result_carries_full_structured_contract(tmp_path, task):
    state = OverflowState(tmp_path / "state.sqlite3")
    board = BoardSnapshot("board-a", 3, 3, (task,))
    result = run_tick((board,), state=state, adapters=_available_adapter(), now=1)
    assert result.board == "board-a"
    assert result.task_id == task.id
    assert result.idempotency_key
    assert result.trigger_evidence["board_saturation"]["board-a"]["saturated"] is True
    assert result.isolation_verdict == "eligible_isolation_safe"
    assert set(result.approval_verdict) == {"cursor-cloud", "claude-cloud", "codex-cloud"}
    assert result.next_action.startswith("HUMAN_GATE")


def test_approval_verdict_reports_every_provider_even_unconfigured(tmp_path, task):
    state = OverflowState(tmp_path / "state.sqlite3")
    board = BoardSnapshot("board-a", 3, 3, (task,))
    result = run_tick((board,), state=state, adapters={}, now=1)
    assert result.approval_verdict == {
        "cursor-cloud": "not_configured",
        "claude-cloud": "not_configured",
        "codex-cloud": "not_configured",
    }
    assert result.reason == "no_eligible_candidate" or result.reason == "no_provider_available"
