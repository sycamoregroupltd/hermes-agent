"""Dispatch hard stop for operator-blocked inference providers (t_e6c9ccaf).

Incident: the dispatcher spawned native-Nous workers against a fixed,
non-replenishable balance. The guarantee under test is narrow and absolute —
when an operator blocks a provider, **no worker process is created** for a
card whose effective provider is that provider.

Every test uses a temp HERMES_HOME with temp profiles, a temp kanban DB, and
a mocked process launcher. Nothing here touches a real profile, a real
board, or a real provider.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest


PROFILES = {
    # Mirrors the incident profile: nous default, no per-task override.
    "nousprofile": {"model": {"default": "deepseek/deepseek-v4-flash",
                              "provider": "nous"}},
    # Clearly non-Nous — must never be caught by the guard.
    "openaiprofile": {"model": {"default": "gpt-5.4", "provider": "openai"}},
    # Case/whitespace mangling of the same blocked provider.
    "messyprofile": {"model": {"default": "m", "provider": "  NoUs \n"}},
    # Provider resolution is ambiguous: worker would fall through to `auto`.
    "noproviderprofile": {"model": {"default": "some-model"}},
    # Explicit `auto` — resolved from credentials inside the child.
    "autoprofile": {"model": {"default": "some-model", "provider": "auto"}},
}


@pytest.fixture()
def kb_env(monkeypatch):
    """Fresh HERMES_HOME + kanban module, with profiles on disk.

    ``malformedprofile`` gets deliberately broken YAML; ``bareprofile`` gets
    no config.yaml at all. Both are ambiguity cases for the fail-closed rule.
    """
    import yaml

    home = tempfile.mkdtemp(prefix="kanban_provider_policy_test_")
    for name, cfg in PROFILES.items():
        pdir = os.path.join(home, "profiles", name)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh)
    mdir = os.path.join(home, "profiles", "malformedprofile")
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write("model: {default: 'x', provider: [unclosed\n")
    os.makedirs(os.path.join(home, "profiles", "bareprofile"), exist_ok=True)

    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.delenv("HERMES_KANBAN_BLOCKED_PROVIDERS", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    for mod in list(sys.modules):
        if (mod.startswith("hermes_cli") or mod.startswith("hermes_state")
                or mod == "hermes_constants"):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    kanban_db.create_board(slug="default", name="Test")
    yield kanban_db


class _SpawnRecorder:
    """Stand-in for the process launcher: records, never spawns."""

    def __init__(self):
        self.calls = []

    def __call__(self, task, workspace, **kwargs):
        self.calls.append((task.id, task.assignee))
        return 4242


def _block(monkeypatch, value="nous"):
    monkeypatch.setenv("HERMES_KANBAN_BLOCKED_PROVIDERS", value)


def _events(kb, conn, task_id, kind=None):
    return [
        e for e in kb.list_events(conn, task_id)
        if kind is None or e.kind == kind
    ]


# ---------------------------------------------------------------------------
# Core guarantee: blocked provider never reaches a spawn
# ---------------------------------------------------------------------------


def test_blocked_nous_profile_default_is_refused(kb_env, monkeypatch):
    """Profile default resolves to nous → refused, and the launcher is never
    called. This is the incident case."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == [], "a blocked provider must never reach the launcher"
    assert res.spawned == []
    assert res.skipped_provider_blocked == [(tid, "nous", "provider_blocked")]


def test_blocked_nous_via_task_override_is_refused(kb_env, monkeypatch):
    """A task on a clean profile that overrides *to* nous is refused."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="openaiprofile",
            model_override="deepseek/deepseek-v4-flash",
            provider_override="nous",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked == [(tid, "nous", "provider_blocked")]


def test_override_away_from_nous_is_not_falsely_blocked(kb_env, monkeypatch):
    """A nous-default profile whose card explicitly overrides to openai still
    dispatches — ``--provider openai`` replaces the profile default on argv."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="nousprofile",
            model_override="gpt-5.4", provider_override="openai",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert res.skipped_provider_blocked == []
    assert [c[0] for c in spawn.calls] == [tid]


def test_allowed_non_nous_default_dispatches(kb_env, monkeypatch):
    """A clearly non-Nous profile is unaffected by the policy."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="openaiprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert res.skipped_provider_blocked == []
    assert [c[0] for c in spawn.calls] == [tid]


def test_model_override_only_keeps_profile_provider(kb_env, monkeypatch):
    """``model_override`` with no provider means the worker still uses the
    profile's provider — a nous profile stays blocked."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="nousprofile", model_override="gpt-5.4",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked == [(tid, "nous", "provider_blocked")]


def test_provider_prefix_in_model_override_is_refused(kb_env, monkeypatch):
    """``-m nous:<model>`` on an allowed profile is conservatively refused."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="openaiprofile",
            model_override="nous:hermes-4",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked[0][0] == tid
    assert res.skipped_provider_blocked[0][2] == "provider_blocked"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["nous", "  NOUS  ", "NoUs", "nous,openai", "nous openai"])
def test_policy_value_case_and_whitespace(kb_env, monkeypatch, policy):
    kb = kb_env
    _block(monkeypatch, policy)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert len(res.skipped_provider_blocked) == 1


def test_profile_provider_case_and_whitespace(kb_env, monkeypatch):
    """``provider: '  NoUs \\n'`` in a profile is the same provider."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="messyprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked[0][1] == "nous"


def test_task_override_case_and_whitespace(kb_env, monkeypatch):
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        kb.create_task(
            conn, title="t", assignee="openaiprofile",
            model_override="m", provider_override=" NOUS ",
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked[0][1] == "nous"


# ---------------------------------------------------------------------------
# Fail-closed on ambiguity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("assignee", "expected_provider"),
    [
        # No `model.provider` key at all → worker falls through to `auto`.
        ("noproviderprofile", ""),
        # Unparseable config.yaml → same unpinnable outcome.
        ("malformedprofile", ""),
        # No config.yaml at all.
        ("bareprofile", ""),
        # Explicit `auto` — named, but still resolved from credentials.
        ("autoprofile", "auto"),
    ],
)
def test_ambiguous_provider_fails_closed(
    kb_env, monkeypatch, assignee, expected_provider,
):
    """No pinnable provider → the child would resolve one from credentials,
    which could be the blocked one. Refuse."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee=assignee)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked == [
        (tid, expected_provider, "provider_unresolved")
    ]


def test_ambiguous_profile_still_dispatches_with_no_policy(kb_env):
    """Fail-closed only bites when a policy is configured. Without one, an
    unresolvable profile dispatches exactly as it always did."""
    kb = kb_env
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="malformedprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert res.skipped_provider_blocked == []
    assert [c[0] for c in spawn.calls] == [tid]


def test_env_inference_provider_pins_resolution(kb_env, monkeypatch):
    """A profile with no provider falls through to $HERMES_INFERENCE_PROVIDER;
    when that names an allowed provider it is no longer ambiguous."""
    kb = kb_env
    _block(monkeypatch)
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openai")
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="noproviderprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert res.skipped_provider_blocked == []
    assert [c[0] for c in spawn.calls] == [tid]


def test_env_inference_provider_blocked_is_refused(kb_env, monkeypatch):
    kb = kb_env
    _block(monkeypatch)
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "nous")
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="noproviderprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked[0][2] == "provider_blocked"


# ---------------------------------------------------------------------------
# Backward compatibility (policy unset)
# ---------------------------------------------------------------------------


def test_default_install_dispatches_nous_unchanged(kb_env):
    """No policy configured → the guard is inert, including for nous."""
    kb = kb_env
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert [c[0] for c in spawn.calls] == [tid]
    assert res.skipped_provider_blocked == []


def test_empty_env_value_disables_policy_over_config(kb_env, monkeypatch):
    """Present-but-empty env is the documented off switch and must beat a
    non-empty config policy."""
    kb = kb_env
    from hermes_cli import kanban_provider_policy as kpp

    cfg = {"kanban": {"blocked_providers": ["nous"]}}
    assert kpp.load_blocked_providers(config=cfg, env={}) == frozenset({"nous"})
    assert kpp.load_blocked_providers(
        config=cfg, env={"HERMES_KANBAN_BLOCKED_PROVIDERS": ""},
    ) == frozenset()
    assert kpp.load_blocked_providers(
        config=cfg, env={"HERMES_KANBAN_BLOCKED_PROVIDERS": "openai"},
    ) == frozenset({"openai"})


@pytest.mark.parametrize("bad", [5, {"nous": True}, [1, 2], 3.5])
def test_malformed_policy_is_inert_not_global_block(kb_env, monkeypatch, bad):
    """A malformed policy must not become "block every provider"."""
    kb = kb_env
    from hermes_cli import kanban_provider_policy as kpp

    assert kpp.load_blocked_providers(
        config={"kanban": {"blocked_providers": bad}}, env={},
    ) == frozenset()


def test_malformed_config_does_not_block_clear_non_nous_profile(kb_env, monkeypatch):
    """Ordinary config breakage elsewhere must not take out a profile that
    plainly names an allowed provider."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="openaiprofile")
        kb.create_task(conn, title="t2", assignee="malformedprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert [c[0] for c in spawn.calls] == [tid]
    assert [s[0] for s in res.skipped_provider_blocked] != [tid]


# ---------------------------------------------------------------------------
# No retry burn, no storm, deterministic card state
# ---------------------------------------------------------------------------


def test_denial_does_not_claim_or_burn_a_retry(kb_env, monkeypatch):
    """The card keeps its status, takes no claim, opens no run, and its
    failure counter is untouched — so the circuit breaker never trips."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    for _ in range(5):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(conn, spawn_fn=spawn, failure_limit=1)
            assert res.auto_blocked == []

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, claim_lock, worker_pid, consecutive_failures, "
            "last_failure_error, current_run_id FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,),
        ).fetchone()[0]

    assert spawn.calls == []
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None
    assert row["consecutive_failures"] == 0
    assert row["last_failure_error"] is None
    assert row["current_run_id"] is None
    assert runs == 0


def test_repeated_dispatch_does_not_storm_the_event_log(kb_env, monkeypatch):
    """Ten ticks against a permanently-refused card write exactly one event."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    for _ in range(10):
        with kb.connect_closing() as conn:
            kb.dispatch_once(conn, spawn_fn=spawn)

    with kb.connect_closing() as conn:
        denials = _events(kb, conn, tid, "provider_policy_denied")

    assert spawn.calls == []
    assert len(denials) == 1


def test_denial_event_payload_is_deterministic(kb_env, monkeypatch):
    kb = kb_env
    _block(monkeypatch)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_SpawnRecorder())
    with kb.connect_closing() as conn:
        ev = _events(kb, conn, tid, "provider_policy_denied")[0]

    assert ev.payload["reason"] == "provider_blocked"
    assert ev.payload["provider"] == "nous"
    assert ev.payload["source"] == "profile_config"
    assert ev.payload["policy"] == ["nous"]
    assert isinstance(ev.payload["detail"], str) and ev.payload["detail"]


def test_denial_event_restated_after_intervening_activity(kb_env, monkeypatch):
    """De-duplication must not silence the record forever: activity on the
    card resets it so the next refusal is stated again."""
    kb = kb_env
    _block(monkeypatch)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_SpawnRecorder())
    with kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "operator", "poking the card")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_SpawnRecorder())
    with kb.connect_closing() as conn:
        denials = _events(kb, conn, tid, "provider_policy_denied")

    assert len(denials) == 2


def test_lifting_policy_lets_the_card_dispatch(kb_env, monkeypatch):
    """No operator unblock needed — the card was never transitioned."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=spawn)
    assert spawn.calls == []

    monkeypatch.setenv("HERMES_KANBAN_BLOCKED_PROVIDERS", "")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert [c[0] for c in spawn.calls] == [tid]
    assert res.skipped_provider_blocked == []


# ---------------------------------------------------------------------------
# Review lane
# ---------------------------------------------------------------------------


def test_review_lane_is_gated_too(kb_env, monkeypatch):
    """Review agents run inference; the review column cannot be a hole."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_has_terminal", lambda name: True,
    )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert spawn.calls == []
    assert res.skipped_provider_blocked == [(tid, "nous", "provider_blocked")]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"


# ---------------------------------------------------------------------------
# Spawn-time backstop: no process is created even if the loop gate is bypassed
# ---------------------------------------------------------------------------


def test_default_spawn_refuses_before_popen(kb_env, monkeypatch):
    """``_default_spawn`` called directly must raise before Popen runs."""
    kb = kb_env
    _block(monkeypatch, "nous")

    def exploding_popen(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("Popen must never be reached for a blocked provider")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
        task = kb.get_task(conn, tid)

    with tempfile.TemporaryDirectory() as ws:
        with pytest.raises(kb.ProviderPolicyBlocked) as excinfo:
            kb._default_spawn(task, ws)

    assert excinfo.value.decision.provider == "nous"
    assert excinfo.value.decision.reason == "provider_blocked"


def test_default_spawn_allows_permitted_provider(kb_env, monkeypatch):
    """The backstop is not a blanket refusal — allowed providers still spawn."""
    kb = kb_env
    _block(monkeypatch, "nous")
    captured = {}

    class FakeProc:
        pid = 777

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="openaiprofile")
        task = kb.get_task(conn, tid)

    with tempfile.TemporaryDirectory() as ws:
        assert kb._default_spawn(task, ws) == 777
    assert "-p" in captured["cmd"]


def test_backstop_denial_releases_claim_without_a_failure(kb_env, monkeypatch):
    """Policy flipped between the pre-claim check and the spawn: the claim is
    released, the card returns to ready, and no retry is consumed."""
    kb = kb_env
    spawn_attempts = []

    def flipping_spawn(task, workspace, **kwargs):
        # Policy turns on only now — the loop gate already passed.
        os.environ["HERMES_KANBAN_BLOCKED_PROVIDERS"] = "nous"
        spawn_attempts.append(task.id)
        return kb._default_spawn(task, workspace, **kwargs)

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    def exploding_popen(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("Popen must never be reached for a blocked provider")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    try:
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(conn, spawn_fn=flipping_spawn, failure_limit=1)
    finally:
        os.environ.pop("HERMES_KANBAN_BLOCKED_PROVIDERS", None)

    assert spawn_attempts == [tid]
    assert res.spawned == []
    assert res.auto_blocked == []
    assert res.skipped_provider_blocked == [(tid, "nous", "provider_blocked")]
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, claim_lock, consecutive_failures FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# Dispatch entry points
# ---------------------------------------------------------------------------


def test_cli_dispatch_reports_refusals(kb_env, monkeypatch, capsys):
    """``hermes kanban dispatch --json`` surfaces the refusal."""
    kb = kb_env
    _block(monkeypatch)
    from hermes_cli import kanban as kanban_cli

    monkeypatch.setattr(kb, "_default_spawn", _SpawnRecorder())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")

    class Args:
        board = None
        dry_run = False
        max = None
        json = True
        ttl = None
        failure_limit = 2

    rc = kanban_cli._cmd_dispatch(Args())
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["spawned"] == []
    assert payload["skipped_provider_blocked"] == [
        {"task_id": tid, "provider": "nous", "reason": "provider_blocked"}
    ]


def test_gateway_style_dispatch_is_gated(kb_env, monkeypatch):
    """The gateway/dashboard call the same ``dispatch_once`` with a board pin
    and no ``spawn_fn``; the real launcher must still refuse."""
    kb = kb_env
    _block(monkeypatch)

    def exploding_popen(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("Popen must never be reached for a blocked provider")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, max_spawn=8, board="default")

    assert res.spawned == []
    assert res.skipped_provider_blocked == [(tid, "nous", "provider_blocked")]


def test_dry_run_reports_refusal_without_writing(kb_env, monkeypatch):
    kb = kb_env
    _block(monkeypatch)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nousprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_SpawnRecorder(), dry_run=True)
    with kb.connect_closing() as conn:
        denials = _events(kb, conn, tid, "provider_policy_denied")

    assert res.spawned == []
    assert res.skipped_provider_blocked == [(tid, "nous", "provider_blocked")]
    assert denials == []


# ---------------------------------------------------------------------------
# Existing dispatcher semantics are preserved
# ---------------------------------------------------------------------------


def test_unrelated_cards_still_dispatch_alongside_a_refusal(kb_env, monkeypatch):
    """A refusal must not abort the tick for other cards."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        blocked_id = kb.create_task(conn, title="blocked", assignee="nousprofile")
        ok_id = kb.create_task(conn, title="ok", assignee="openaiprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn)

    assert [c[0] for c in spawn.calls] == [ok_id]
    assert [s[0] for s in res.skipped_provider_blocked] == [blocked_id]
    assert [s[0] for s in res.spawned] == [ok_id]


def test_refusal_does_not_consume_max_spawn_budget(kb_env, monkeypatch):
    """A refused card must not eat a concurrency slot from an allowed one."""
    kb = kb_env
    _block(monkeypatch)
    spawn = _SpawnRecorder()
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="blocked", assignee="nousprofile", priority=9)
        ok_id = kb.create_task(conn, title="ok", assignee="openaiprofile")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)

    assert [c[0] for c in spawn.calls] == [ok_id]
    assert len(res.spawned) == 1
