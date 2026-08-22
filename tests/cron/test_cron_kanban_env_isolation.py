"""Cron sessions must not inherit a kanban worker's dispatcher identity.

A cron job can be fired *in-process* from a kanban worker: the worker is a
normal ``hermes chat -q`` CLI agent (its default toolset includes ``cronjob``)
running with ``HERMES_KANBAN_TASK`` legitimately set in its own environment,
and ``cronjob(action="run")`` calls ``run_one_job()`` -> ``run_job()`` in that
same process.

Without isolation the cron ``AIAgent`` is misidentified as that worker: the
kanban toolset is force-added, the kanban-worker protocol is injected into its
system prompt, and ``kanban_complete`` defaults ``task_id`` to
``$HERMES_KANBAN_TASK`` — letting an unrelated cron job close the worker's task
and overwrite real results.

The isolation is a **ContextVar**, deliberately not an ``os.environ`` clear:
``os.environ`` is process-global and shared with

  * the worker's own claim heartbeat (``run_agent._touch_activity`` ->
    ``heartbeat_current_worker_from_env``), which would starve and let the
    dispatcher reclaim a task whose worker is still alive;
  * the gateway's kanban watchers, which do their own board save/restore;
  * concurrent cron jobs on the parallel pool, which take a *shared* read lock
    and can interleave one another's snapshot/restore.

So these tests assert both that the identity is hidden AND that the environment
is left completely untouched.
"""

from __future__ import annotations

import ast
import os
import threading

import pytest


@pytest.fixture(autouse=True)
def _clear_kanban_detect_cache():
    """`_detect_environment` memoizes per process; kanban is context-dependent."""
    import agent.skill_utils as su

    su._ENV_DETECT_CACHE.pop("kanban", None)
    yield
    su._ENV_DETECT_CACHE.pop("kanban", None)


@pytest.fixture()
def worker_env(monkeypatch, tmp_path):
    """Simulate running inside a dispatcher-spawned kanban worker.

    Creates a real temp board with a task claimed by THIS process (worker_pid =
    current pid), so the dispatcher-ownership belt in ``_default_task_id`` passes
    for the genuine worker. Tests that need the env-inheriting-child shape
    re-point ``worker_pid`` (or the claim_lock) before calling the tool.
    """
    import os

    from pathlib import Path

    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()

    host = kb._claimer_id().split(":", 1)[0]
    lock = f"{host}:{os.getpid()}"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="worker", assignee="capability-builder")
        kb.claim_task(conn, tid, claimer=lock)
        kb._set_worker_pid(conn, tid, os.getpid())

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKSPACES_ROOT",
        str(home / "kanban" / "boards" / "default" / "workspaces"),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", lock)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    return {"tid": tid, "claim_lock": lock}


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

class TestDispatcherOwnedPredicate:
    def test_default_is_dispatcher_owned(self):
        from agent.delegation_context import is_dispatcher_owned_worker_context

        assert is_dispatcher_owned_worker_context() is True

    def test_false_inside_non_dispatcher_context(self):
        from agent.delegation_context import (
            is_dispatcher_owned_worker_context,
            non_dispatcher_owned_context,
        )

        with non_dispatcher_owned_context():
            assert is_dispatcher_owned_worker_context() is False
        assert is_dispatcher_owned_worker_context() is True

    def test_token_form_restores(self):
        from agent.delegation_context import (
            enter_non_dispatcher_owned_context,
            exit_non_dispatcher_owned_context,
            is_dispatcher_owned_worker_context,
        )

        token = enter_non_dispatcher_owned_context()
        assert is_dispatcher_owned_worker_context() is False
        exit_non_dispatcher_owned_context(token)
        assert is_dispatcher_owned_worker_context() is True

    def test_nesting_restores_outer_value(self):
        from agent.delegation_context import (
            is_dispatcher_owned_worker_context,
            non_dispatcher_owned_context,
        )

        with non_dispatcher_owned_context():
            with non_dispatcher_owned_context():
                assert is_dispatcher_owned_worker_context() is False
            assert is_dispatcher_owned_worker_context() is False
        assert is_dispatcher_owned_worker_context() is True

    def test_delegated_child_still_not_dispatcher_owned(self, monkeypatch):
        """The pre-existing delegate_task flag keeps its meaning."""
        import agent.delegation_context as dc

        token = dc._DELEGATED_CHILD_CONTEXT.set(True)
        try:
            assert dc.is_dispatcher_owned_worker_context() is False
        finally:
            dc._DELEGATED_CHILD_CONTEXT.reset(token)

    def test_thread_isolation(self, worker_env):
        """A ContextVar set in one thread must not leak into a sibling thread.

        This is the property an os.environ clear cannot provide, and the reason
        concurrent cron jobs can't corrupt each other.
        """
        from agent.delegation_context import (
            is_dispatcher_owned_worker_context,
            non_dispatcher_owned_context,
        )

        seen = {}
        release = threading.Event()

        def sibling():
            seen["sibling"] = is_dispatcher_owned_worker_context()
            release.set()

        def job():
            with non_dispatcher_owned_context():
                seen["job"] = is_dispatcher_owned_worker_context()
                t = threading.Thread(target=sibling)
                t.start()
                release.wait(5)
                t.join(5)

        t = threading.Thread(target=job)
        t.start()
        t.join(5)

        assert seen["job"] is False, "job thread must be marked non-dispatcher"
        assert seen["sibling"] is True, "sibling thread must be unaffected"


# ---------------------------------------------------------------------------
# The gates that consume it
# ---------------------------------------------------------------------------

class TestKanbanGatesRespectContext:
    def test_task_tools_hidden_from_cron_agent(self, worker_env):
        from agent.delegation_context import non_dispatcher_owned_context
        from tools import kanban_tools

        assert kanban_tools._check_kanban_mode() is True
        with non_dispatcher_owned_context():
            assert kanban_tools._check_kanban_mode() is False

    def test_complete_does_not_default_to_worker_task(self, worker_env):
        """The damage path: kanban_complete must not inherit the task id."""
        from agent.delegation_context import non_dispatcher_owned_context
        from tools import kanban_tools

        # Genuine worker: defaulting resolves to the claimed task.
        assert kanban_tools._default_task_id(None) == worker_env["tid"]
        with non_dispatcher_owned_context():
            assert kanban_tools._default_task_id(None) is None

    def test_explicit_task_id_still_honoured(self, worker_env):
        """Only the implicit default is suppressed, not an explicit argument."""
        from agent.delegation_context import non_dispatcher_owned_context
        from tools import kanban_tools

        with non_dispatcher_owned_context():
            assert kanban_tools._default_task_id("t_explicit") == "t_explicit"

    def test_default_task_id_refused_when_worker_pid_mismatch(self, worker_env):
        """Belt: an env-inheriting child (not the recorded worker) must NOT
        default to the worker's task, even with every HERMES_KANBAN_* set."""
        import os

        from hermes_cli import kanban_db as kb
        from tools import kanban_tools

        # Re-point the recorded worker to a different pid — the shape of a cron
        # subprocess that inherited the worker's env but is a separate process.
        with kb.connect() as conn:
            kb._set_worker_pid(conn, worker_env["tid"], os.getpid() + 1)

        assert kanban_tools._default_task_id(None) is None

    def test_default_task_id_allows_when_worker_pid_unrecorded(self, worker_env):
        """Belt fail-open: when no worker_pid is recorded (legacy/manual setup),
        the env-scrub primary defense still applies and defaulting is not broken."""
        from hermes_cli import kanban_db as kb
        from tools import kanban_tools

        with kb.connect() as conn:
            conn.execute(
                "UPDATE tasks SET worker_pid = NULL WHERE id = ?",
                (worker_env["tid"],),
            )
            conn.commit()

        assert kanban_tools._default_task_id(None) == worker_env["tid"]

    def test_skill_environment_gate(self, worker_env):
        from agent.delegation_context import non_dispatcher_owned_context
        import agent.skill_utils as su

        su._ENV_DETECT_CACHE.pop("kanban", None)
        assert su._detect_environment("kanban") is True
        with non_dispatcher_owned_context():
            su._ENV_DETECT_CACHE.pop("kanban", None)
            assert su._detect_environment("kanban") is False

    def test_kanban_env_verdict_is_not_memoized(self, worker_env):
        """`kanban` must bypass _ENV_DETECT_CACHE: caching it process-wide would
        freeze whichever context asked first and leak it to the others."""
        from agent.delegation_context import non_dispatcher_owned_context
        import agent.skill_utils as su

        su._ENV_DETECT_CACHE.pop("kanban", None)
        assert su._detect_environment("kanban") is True
        with non_dispatcher_owned_context():
            # No manual cache clear here — the production code must not have
            # cached the previous True.
            assert su._detect_environment("kanban") is False
        assert su._detect_environment("kanban") is True

    def test_toolset_force_add_suppressed(self, worker_env):
        from agent.delegation_context import non_dispatcher_owned_context
        import model_tools

        assert model_tools._is_dispatcher_owned_worker() is True
        with non_dispatcher_owned_context():
            assert model_tools._is_dispatcher_owned_worker() is False


# ---------------------------------------------------------------------------
# run_job wiring
# ---------------------------------------------------------------------------

class TestRunJobKanbanIsolation:
    @staticmethod
    def _install_stubs(monkeypatch, observed: dict, agent_cls=None):
        import sys

        import cron.scheduler as sched
        from agent.delegation_context import is_dispatcher_owned_worker_context

        class FakeAgent:
            def __init__(self, **kwargs):
                observed["dispatcher_owned_during_init"] = (
                    is_dispatcher_owned_worker_context()
                )
                observed["kanban_env_during_init"] = {
                    k: v for k, v in os.environ.items()
                    if k.startswith("HERMES_KANBAN_")
                }

            def run_conversation(self, *_a, **_kw):
                observed["dispatcher_owned_during_run"] = (
                    is_dispatcher_owned_worker_context()
                )
                return {"final_response": "done", "messages": []}

            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = agent_cls or FakeAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        from hermes_cli import runtime_provider as _rtp

        monkeypatch.setattr(
            _rtp, "resolve_runtime_provider",
            lambda **_kw: {
                "provider": "test", "api_key": "k",
                "base_url": "http://test.local",
                "api_mode": "chat_completions",
            },
        )
        monkeypatch.setattr(
            sched, "_build_job_prompt", lambda job, prerun_script=None, **kw: "hi"
        )
        monkeypatch.setattr(sched, "_resolve_origin", lambda job: None)
        monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
        monkeypatch.setattr(
            sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None
        )
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        import dotenv

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)

    @staticmethod
    def _job(job_id="kanban-iso"):
        return {
            "id": job_id, "name": "kanban-iso-job",
            "workdir": None, "schedule_display": "manual",
        }

    def test_agent_runs_as_non_dispatcher(self, monkeypatch, worker_env):
        import cron.scheduler as sched

        observed: dict = {}
        self._install_stubs(monkeypatch, observed)

        success, *_ = sched.run_job(self._job())
        assert success is True
        assert observed["dispatcher_owned_during_init"] is False
        assert observed["dispatcher_owned_during_run"] is False

    def test_environment_is_left_untouched(self, monkeypatch, worker_env):
        """The whole point of the ContextVar: os.environ must not be mutated, so
        the worker's claim heartbeat and the gateway watchers keep working."""
        import cron.scheduler as sched

        before = {
            k: v for k, v in os.environ.items() if k.startswith("HERMES_KANBAN_")
        }
        assert before, "fixture should have populated kanban env"

        observed: dict = {}
        self._install_stubs(monkeypatch, observed)

        success, *_ = sched.run_job(self._job())
        assert success is True

        # Untouched DURING the job (the heartbeat thread reads it concurrently)...
        assert observed["kanban_env_during_init"] == before
        # ...and after.
        after = {
            k: v for k, v in os.environ.items() if k.startswith("HERMES_KANBAN_")
        }
        assert after == before

    def test_context_reset_after_job(self, monkeypatch, worker_env):
        import cron.scheduler as sched
        from agent.delegation_context import is_dispatcher_owned_worker_context

        observed: dict = {}
        self._install_stubs(monkeypatch, observed)

        sched.run_job(self._job("kanban-iso-reset"))
        assert is_dispatcher_owned_worker_context() is True

    def test_context_reset_even_when_job_raises(self, monkeypatch, worker_env):
        import cron.scheduler as sched
        from agent.delegation_context import is_dispatcher_owned_worker_context

        class ExplodingAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, *_a, **_kw):
                raise RuntimeError("boom")

            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        observed: dict = {}
        self._install_stubs(monkeypatch, observed, agent_cls=ExplodingAgent)

        success, *_ = sched.run_job(self._job("kanban-iso-fail"))
        assert success is False
        assert is_dispatcher_owned_worker_context() is True
        # And the env survived the failure too.
        assert os.environ.get("HERMES_KANBAN_BOARD") == "default"

    def test_concurrent_jobs_do_not_corrupt_worker_identity(
        self, monkeypatch, worker_env
    ):
        """Two workdir-less jobs run concurrently on the parallel pool and take a
        SHARED read lock, so they interleave. With an os.environ snapshot/clear/
        restore this permanently destroyed the worker's identity; a ContextVar is
        per-thread and cannot."""
        import cron.scheduler as sched

        before = {
            k: v for k, v in os.environ.items() if k.startswith("HERMES_KANBAN_")
        }
        observed: dict = {}
        self._install_stubs(monkeypatch, observed)

        results = {}

        def run(name):
            ok, *_ = sched.run_job(self._job(f"kanban-iso-{name}"))
            results[name] = ok

        threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        assert results == {"a": True, "b": True}
        after = {
            k: v for k, v in os.environ.items() if k.startswith("HERMES_KANBAN_")
        }
        assert after == before, "worker identity must survive concurrent cron jobs"


# ---------------------------------------------------------------------------
# Spawn-boundary env scrub (defense-in-depth for the CLI + cron subprocesses)
# ---------------------------------------------------------------------------

class TestCliCronRunScrubsKanbanEnv:
    def test_run_strips_kanban_env_for_duration_and_restores(self, monkeypatch, worker_env):
        """`_cron_api(action='run')` must strip the worker's kanban identity from
        os.environ for the run (so the job agent and its subprocesses never inherit
        it), then restore it so in-process callers are not tainted."""
        import os

        from agent.delegation_context import KANBAN_ENV_KEYS
        from hermes_cli import cron as croncli
        from tools import cronjob_tools

        def _identity():
            return {
                k: v for k, v in os.environ.items() if k in KANBAN_ENV_KEYS
            }

        observed: dict = {}

        def fake_cronjob(**kwargs):
            observed["during"] = _identity()
            return '{"success": true, "job": {}}'

        monkeypatch.setattr(cronjob_tools, "cronjob", fake_cronjob)

        before = _identity()
        assert before, "worker_env should populate kanban identity env"

        result = croncli._cron_api(action="run", job_id="j1")
        after = _identity()

        assert result == {"success": True, "job": {}}
        # During the run the identity vars are gone...
        assert observed["during"] == {}
        # ...and restored afterwards.
        assert after == before

    def test_non_run_actions_do_not_scrub(self, monkeypatch, worker_env):
        """Only action='run' scrubs; list/create etc. leave os.environ untouched."""
        import os

        from agent.delegation_context import KANBAN_ENV_KEYS
        from hermes_cli import cron as croncli
        from tools import cronjob_tools

        seen = {}

        def fake_cronjob(**kwargs):
            seen["kanban"] = {
                k: v for k, v in os.environ.items() if k in KANBAN_ENV_KEYS
            }
            return '{"success": true}'

        monkeypatch.setattr(cronjob_tools, "cronjob", fake_cronjob)
        croncli._cron_api(action="list")
        assert seen["kanban"], "list must not strip the worker's kanban identity"


class TestBotChatDeliveryScrubsKanbanEnv:
    def test_subprocess_env_has_no_kanban_identity(self, monkeypatch, worker_env):
        """The `hermes chat` subprocess spawned for bot-chat delivery must not
        inherit the worker's kanban identity."""
        import subprocess
        import types

        from agent.delegation_context import KANBAN_ENV_KEYS

        import cron.scheduler as sched

        captured: dict = {}

        def fake_run(argv, **kw):
            captured["env"] = dict(kw.get("env") or os.environ)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sched, "_get_bot_chat_delivery_timeout", lambda: 60)

        err = sched._deliver_to_bot_chat(
            {"id": "j1", "name": "botchat"}, "content", profile=""
        )
        assert err is None
        assert not any(k in captured["env"] for k in KANBAN_ENV_KEYS)


class TestScrubKanbanEnvMarkOption:
    def test_mark_false_strips_without_marker(self):
        from agent.delegation_context import (
            DELEGATED_CHILD_ENV_MARKER,
            scrub_kanban_env,
        )

        cleaned = scrub_kanban_env(
            {"HERMES_KANBAN_TASK": "t1", "HERMES_KANBAN_DB": "/x", "KEEP": "v"},
            mark=False,
        )
        assert not any(k.startswith("HERMES_KANBAN_") for k in cleaned)
        assert cleaned["KEEP"] == "v"
        assert DELEGATED_CHILD_ENV_MARKER not in cleaned

    def test_mark_true_sets_marker_by_default(self):
        from agent.delegation_context import (
            DELEGATED_CHILD_ENV_MARKER,
            scrub_kanban_env,
        )

        cleaned = scrub_kanban_env({"HERMES_KANBAN_TASK": "t1"})
        assert cleaned[DELEGATED_CHILD_ENV_MARKER] == "1"


class TestSubprocessBoundaryScrub:
    def test_scrubbed_env_crosses_process_boundary(self, monkeypatch, worker_env):
        """The strip must hold across a real fork: a child spawned with a scrubbed
        env sees zero kanban identity vars."""
        import os
        import subprocess
        import sys

        from agent.delegation_context import KANBAN_ENV_KEYS, scrub_kanban_env

        assert any(k in os.environ for k in KANBAN_ENV_KEYS), (
            "worker_env should populate kanban identity env in the parent"
        )
        child_env = scrub_kanban_env(dict(os.environ), mark=False)
        probe = (
            "import os;"
            "print([k for k in os.environ if k in "
            "{'HERMES_KANBAN_TASK','HERMES_KANBAN_DB','HERMES_KANBAN_RUN_ID',"
            "'HERMES_KANBAN_WORKSPACE','HERMES_KANBAN_WORKSPACES_ROOT',"
            "'HERMES_KANBAN_CLAIM_LOCK','HERMES_KANBAN_BOARD'}])"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=child_env,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------

def test_every_dispatcher_kanban_var_is_identity_gated():
    """Invariant: every HERMES_KANBAN_* var the dispatcher injects is covered by
    the canonical KANBAN_ENV_KEYS, so the delegate_task subprocess scrubber and
    any future consumer stay in sync with ``_default_spawn``.

    Fails loudly if a new dispatcher var is added without registering it.
    """
    import hermes_cli.kanban_db as kanban_db
    from agent.delegation_context import KANBAN_ENV_KEYS

    source = ast.parse(open(kanban_db.__file__, encoding="utf-8").read())
    spawn = next(
        node for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == "_default_spawn"
    )

    injected = set()
    for node in ast.walk(spawn):
        # env["HERMES_KANBAN_X"] = ...  and the annotated form
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                if ast.unparse(target.value) != "env":
                    continue
                key = ast.unparse(target.slice).strip("\"'")
                if key.startswith("HERMES_KANBAN_"):
                    injected.add(key)
        # env.update({"HERMES_KANBAN_X": ...}) / env.setdefault("HERMES_KANBAN_X", ...)
        elif isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            if func not in ("env.update", "env.setdefault"):
                continue
            literals = []
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    literals.extend(
                        k for k in arg.keys if isinstance(k, ast.Constant)
                    )
                elif isinstance(arg, ast.Constant):
                    literals.append(arg)
            for kw in node.keywords:
                if kw.arg and kw.arg.startswith("HERMES_KANBAN_"):
                    injected.add(kw.arg)
            for lit in literals:
                if isinstance(lit.value, str) and lit.value.startswith(
                    "HERMES_KANBAN_"
                ):
                    injected.add(lit.value)

    assert injected, "failed to parse dispatcher kanban env injection"

    # These are worker-behaviour knobs rather than board/task identity; they are
    # intentionally not part of KANBAN_ENV_KEYS. Listed explicitly so adding a
    # new var forces a decision instead of silently passing.
    behaviour_only = {
        "HERMES_KANBAN_BRANCH",
        "HERMES_KANBAN_GOAL_MODE",
        "HERMES_KANBAN_GOAL_MAX_TURNS",
    }
    uncovered = injected - set(KANBAN_ENV_KEYS) - behaviour_only
    assert not uncovered, (
        f"dispatcher injects {sorted(uncovered)} which is neither in "
        "KANBAN_ENV_KEYS nor explicitly classified as behaviour-only"
    )
