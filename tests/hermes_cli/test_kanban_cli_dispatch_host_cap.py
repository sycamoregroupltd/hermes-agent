"""CLI dispatch: the HOST's cap, and the one-dispatcher-per-host rule.

Two ways the standalone ``hermes kanban dispatch`` tick escaped the gateway's
accounting before this:

1. ``_cmd_dispatch`` resolved ``kanban.max_in_progress`` from the INVOKING
   home's ``config.yaml``. PM crons run inside profiles, so a profile that
   carried its own (looser) copy of the number could push the host past the
   budget every other entry point enforces — the gateway dispatcher then sat at
   "ready queue non-empty but 0 spawned" while short-lived CLI ticks kept
   claiming cards.
2. Nothing stopped a CLI tick from running while the gateway's embedded
   dispatcher was live. Both act on the same board DBs, so the two ticks race
   for claims and double the spawn burst — the same footgun ``kanban daemon``
   guards with ``--force``.

The last test pins the claim invariant a one-shot tick makes easy to violate: a
spawn that reports no PID must not leave a card ``running`` behind a claim no
process holds.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME (the host home a tick acts on) + empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _dispatch_args(**kw) -> argparse.Namespace:
    base = dict(dry_run=True, max=None, failure_limit=2, json=False, force_offline=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _capture_dispatch(monkeypatch) -> dict:
    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    return captured


def _patch_local_config(monkeypatch, kanban_cfg: dict) -> None:
    """The invoking home's config (what a PM profile cron would see)."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": kanban_cfg})


def _patch_host_config(monkeypatch, kanban_cfg: dict) -> None:
    """The kanban HOST home's config (the shared root)."""
    monkeypatch.setattr(kbd, "host_kanban_config", lambda: kanban_cfg)


def _patch_gateway_state(monkeypatch, state: str, detail: str = "probe detail") -> None:
    monkeypatch.setattr(kbd, "gateway_dispatch_state", lambda **kwargs: (state, detail))


# ---------------------------------------------------------------------------
# 1. The host's cap binds the CLI tick
# ---------------------------------------------------------------------------


def test_cli_dispatch_uses_host_cap_when_local_copy_is_looser(
    kanban_home, monkeypatch,
):
    """A profile's looser copy of max_in_progress must not raise the shared budget."""
    captured = _capture_dispatch(monkeypatch)
    _patch_local_config(monkeypatch, {"max_in_progress": 8})
    _patch_host_config(monkeypatch, {"max_in_progress": 6})
    _patch_gateway_state(monkeypatch, "down")

    assert kb_cli._cmd_dispatch(_dispatch_args()) == 0
    assert captured.get("max_in_progress") == 6, (
        f"the host cap must bind a CLI tick; got {captured.get('max_in_progress')!r}"
    )


def test_cli_dispatch_keeps_a_stricter_local_cap(kanban_home, monkeypatch):
    """A profile may tighten the shared budget, never loosen it."""
    captured = _capture_dispatch(monkeypatch)
    _patch_local_config(monkeypatch, {"max_in_progress": 3})
    _patch_host_config(monkeypatch, {"max_in_progress": 6})
    _patch_gateway_state(monkeypatch, "down")

    assert kb_cli._cmd_dispatch(_dispatch_args()) == 0
    assert captured.get("max_in_progress") == 3


def test_cli_dispatch_host_cap_applies_when_local_is_unset(kanban_home, monkeypatch):
    captured = _capture_dispatch(monkeypatch)
    _patch_local_config(monkeypatch, {})
    _patch_host_config(monkeypatch, {"max_in_progress": 6})
    _patch_gateway_state(monkeypatch, "down")

    assert kb_cli._cmd_dispatch(_dispatch_args()) == 0
    assert captured.get("max_in_progress") == 6


def test_cli_dispatch_derives_default_when_neither_side_sets_a_cap(
    kanban_home, monkeypatch,
):
    """No explicit cap anywhere still flows the memory-derived default through."""
    captured = _capture_dispatch(monkeypatch)
    _patch_local_config(monkeypatch, {})
    _patch_host_config(monkeypatch, {})
    _patch_gateway_state(monkeypatch, "down")

    assert kb_cli._cmd_dispatch(_dispatch_args()) == 0
    assert captured.get("max_in_progress") == kbd.resolve_max_in_progress(None)


def test_cli_dispatch_passes_the_other_caps_through_unchanged(
    kanban_home, monkeypatch,
):
    """The host-cap fix must not touch the per-profile cap or per-board max_spawn."""
    captured = _capture_dispatch(monkeypatch)
    _patch_local_config(
        monkeypatch,
        {"max_in_progress": 6, "max_in_progress_per_profile": 3, "max_spawn": 4},
    )
    _patch_host_config(monkeypatch, {"max_in_progress": 6})
    _patch_gateway_state(monkeypatch, "down")

    assert kb_cli._cmd_dispatch(_dispatch_args()) == 0
    assert captured.get("max_in_progress") == 6
    assert captured.get("max_in_progress_per_profile") == 3
    assert captured.get("max_spawn") == 4


def test_resolve_host_max_in_progress_takes_the_stricter_side(monkeypatch):
    monkeypatch.setattr(kbd, "host_kanban_config", lambda: {"max_in_progress": 6})
    assert kbd.resolve_host_max_in_progress(8) == 6
    assert kbd.resolve_host_max_in_progress(3) == 3
    assert kbd.resolve_host_max_in_progress(None) == 6

    monkeypatch.setattr(kbd, "host_kanban_config", dict)
    assert kbd.resolve_host_max_in_progress(3) == 3
    # Unset everywhere -> None, so the caller derives the default instead of
    # silently disabling the cap.
    assert kbd.resolve_host_max_in_progress(None) is None

    # Garbage in the host config is ignored, not fatal.
    monkeypatch.setattr(kbd, "host_kanban_config", lambda: {"max_in_progress": 0})
    assert kbd.resolve_host_max_in_progress(None) is None
    monkeypatch.setattr(kbd, "host_kanban_config", lambda: {"max_in_progress": "lots"})
    assert kbd.resolve_host_max_in_progress(None) is None


def test_cli_tick_spends_one_host_budget_across_boards(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """End to end: the CLI's resolved cap counts workers on OTHER boards too."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        for title in ("busy-1", "busy-2"):
            tid = kb.create_task(conn, title=title, assignee="alice")
            assert kb.claim_task(conn, tid) is not None
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")

    _patch_local_config(monkeypatch, {"max_in_progress": 8})   # looser profile copy
    _patch_host_config(monkeypatch, {"max_in_progress": 3})    # real host budget
    _patch_gateway_state(monkeypatch, "down")
    # Deterministic budget: the memory-pressure guard is not what this pins.
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")

    spawned: list = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 4242

    monkeypatch.setattr(kbd, "_default_spawn", fake_spawn)

    assert kb_cli._cmd_dispatch(_dispatch_args(dry_run=False)) == 0
    # 2 running on the other board + host cap 3 -> exactly one slot here.
    assert len(spawned) == 1, f"expected 1 spawn, got {spawned}"


# ---------------------------------------------------------------------------
# 2. One dispatcher per host: the CLI refuses while the gateway dispatches
# ---------------------------------------------------------------------------


def test_cli_dispatch_refused_while_gateway_dispatches(
    kanban_home, monkeypatch, capsys,
):
    captured = _capture_dispatch(monkeypatch)
    _patch_gateway_state(monkeypatch, "dispatching", "gateway pid=4242 dispatches kanban for /root")

    rc = kb_cli._cmd_dispatch(_dispatch_args())

    assert rc == 2
    assert captured == {}, "a refused tick must not call dispatch_once"
    err = capsys.readouterr().err
    assert "refused" in err and "pid=4242" in err


def test_cli_force_offline_cannot_override_a_live_gateway(
    kanban_home, monkeypatch, capsys,
):
    """--force-offline is for an inconclusive probe, not for bypassing the gateway."""
    captured = _capture_dispatch(monkeypatch)
    _patch_gateway_state(monkeypatch, "dispatching", "gateway pid=4242 dispatches kanban")

    rc = kb_cli._cmd_dispatch(_dispatch_args(force_offline=True))

    assert rc == 2
    assert captured == {}
    assert "--force-offline does NOT override" in capsys.readouterr().err


def test_cli_dispatch_refused_when_probe_is_inconclusive(
    kanban_home, monkeypatch, capsys,
):
    captured = _capture_dispatch(monkeypatch)
    _patch_gateway_state(monkeypatch, "unknown", "gateway liveness probe reported an error")

    rc = kb_cli._cmd_dispatch(_dispatch_args())

    assert rc == 2
    assert captured == {}
    assert "--force-offline" in capsys.readouterr().err


def test_cli_force_offline_takes_the_tick_when_probe_is_inconclusive(
    kanban_home, monkeypatch, capsys,
):
    captured = _capture_dispatch(monkeypatch)
    _patch_gateway_state(monkeypatch, "unknown", "gateway liveness probe reported an error")
    _patch_local_config(monkeypatch, {})

    rc = kb_cli._cmd_dispatch(_dispatch_args(force_offline=True))

    assert rc == 0
    assert captured, "an explicit --force-offline tick must still dispatch"
    assert "force-offline" in capsys.readouterr().err


def test_cli_dispatch_proceeds_when_no_gateway_or_dispatch_disabled(
    kanban_home, monkeypatch,
):
    for state in ("down", "idle"):
        captured = _capture_dispatch(monkeypatch)
        _patch_gateway_state(monkeypatch, state)
        _patch_local_config(monkeypatch, {})
        assert kb_cli._cmd_dispatch(_dispatch_args()) == 0, state
        assert captured, state


# ---------------------------------------------------------------------------
# 3. The probe itself: host-scoped, and never invents a dispatcher
# ---------------------------------------------------------------------------


def test_gateway_dispatch_state_is_scoped_to_the_host_home(kanban_home):
    """A home with no gateway reports ``down`` — never the root's live gateway."""
    state, detail = kbd.gateway_dispatch_state()
    assert state == "down", detail


def test_gateway_dispatch_state_maps_liveness(kanban_home, monkeypatch):
    import gateway.status as gateway_status

    def liveness(**kwargs):
        return SimpleNamespace(running=True, pid=4242, probe_error=False)

    monkeypatch.setattr(gateway_status, "resolve_gateway_liveness", liveness)

    _patch_host_config(monkeypatch, {"dispatch_in_gateway": True})
    assert kbd.gateway_dispatch_state()[0] == "dispatching"

    _patch_host_config(monkeypatch, {"dispatch_in_gateway": False})
    state, detail = kbd.gateway_dispatch_state()
    assert state == "idle" and "dispatch_in_gateway=false" in detail

    # Unset falls back to the gateway's own default (dispatch enabled).
    _patch_host_config(monkeypatch, {})
    assert kbd.gateway_dispatch_state()[0] == "dispatching"

    monkeypatch.setattr(
        gateway_status, "resolve_gateway_liveness",
        lambda **kwargs: SimpleNamespace(running=False, pid=None, probe_error=False),
    )
    assert kbd.gateway_dispatch_state()[0] == "down"

    monkeypatch.setattr(
        gateway_status, "resolve_gateway_liveness",
        lambda **kwargs: SimpleNamespace(running=False, pid=None, probe_error=True),
    )
    assert kbd.gateway_dispatch_state()[0] == "unknown"

    def exploded(**kwargs):
        raise RuntimeError("probe died")

    monkeypatch.setattr(gateway_status, "resolve_gateway_liveness", exploded)
    assert kbd.gateway_dispatch_state()[0] == "unknown"


# ---------------------------------------------------------------------------
# 4. A claim never outlives the worker that was supposed to hold it
# ---------------------------------------------------------------------------


def test_spawn_without_pid_releases_the_claim(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """No PID -> nothing confirms a worker: release the claim, don't park the card."""
    monkeypatch.setattr(
        kbd, "_default_spawn", lambda task, workspace, board=None: None,
    )
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no-pid", assignee="alice")
        res = kbd.dispatch_once(conn)
        row = conn.execute(
            "SELECT status, claim_lock, consecutive_failures, last_failure_error "
            "FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id = ?", (tid,),
            ).fetchall()
        ]

    assert row["status"] == "ready", "a card with no worker must not stay running"
    assert row["claim_lock"] in (None, "")
    assert row["consecutive_failures"] == 1
    assert "no pid" in (row["last_failure_error"] or "")
    assert "spawn_failed" in outcomes
    assert not res.spawned


def test_injected_spawn_fn_still_may_report_nothing(
    kanban_home, all_assignees_spawnable,
):
    """The spawn_fn seam keeps its contract — injected stubs may return nothing.

    The no-PID release above is about the REAL spawn path (``_default_spawn``);
    a harness that injects its own spawn_fn reports whatever it likes and the
    card stays claimed, exactly as before this change.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="injected", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=lambda task, workspace, board=None: None,
        )
        row = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert len(res.spawned) == 1
    assert row["status"] == "running"
    assert row["claim_lock"]
