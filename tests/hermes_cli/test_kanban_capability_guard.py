from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated_kanban_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db
    kanban_db.init_db()
    return kanban_db


def test_decompose_routes_children_to_board_implementer_and_reserves_pm_only(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_decompose as decomp

    profiles = [
        SimpleNamespace(name=name, description=name, is_default=(name == "jarvis-os-pm"))
        for name in ("jarvis-os-pm", "fleet-engineer")
    ]
    monkeypatch.setattr(decomp.profiles_mod, "list_profiles", lambda: profiles)
    monkeypatch.setattr(decomp.profiles_mod, "profile_exists", lambda name: name in {p.name for p in profiles})
    monkeypatch.setattr(decomp.profiles_mod, "get_active_profile_name", lambda: "jarvis-os-pm")
    monkeypatch.setattr(kb, "get_current_board", lambda: "jarvis-os")
    monkeypatch.setattr(
        decomp,
        "_load_config",
        lambda: {
            "kanban": {
                "orchestrator_profile": "jarvis-os-pm",
                "decompose_default_assignee_by_board": {"jarvis-os": "fleet-engineer"},
                "decompose_pm_profiles": ["jarvis-os-pm"],
            }
        },
    )

    regular = decomp._load_routing(SimpleNamespace(body="Implement the requested change."))
    assert regular.default_assignee == "fleet-engineer"
    assert decomp._normalize_assignee_choice(
        "jarvis-os-pm",
        default_assignee=regular.default_assignee,
        valid_names=regular.valid_names,
        pm_profiles=regular.pm_profiles,
        allow_pm=regular.pm_only,
    ) == "fleet-engineer"

    pm_only = decomp._load_routing(SimpleNamespace(body="Coordinate the review.\nPM_ONLY: true\n"))
    assert pm_only.default_assignee == "jarvis-os-pm"
    assert decomp._normalize_assignee_choice(
        "jarvis-os-pm",
        default_assignee=pm_only.default_assignee,
        valid_names=pm_only.valid_names,
        pm_profiles=pm_only.pm_profiles,
        allow_pm=pm_only.pm_only,
    ) == "jarvis-os-pm"


def test_dispatcher_blocks_missing_terminal_file_capability(isolated_kanban_home, monkeypatch):
    kb = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="implement change",
            body="Edit the repository and run pytest.",
            assignee="jarvis-os-pm",
        )

    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "_profile_cli_toolsets", lambda _name: {"kanban", "gateway"})
    monkeypatch.setattr(kbd, "_capability_reroute_profile", lambda _board: "fleet-engineer")
    spawned = []

    with kbc.connect_closing() as conn:
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: spawned.append(True),
            dry_run=False,
        )
        task = kb.get_task(conn, task_id)

    assert not spawned
    assert result.capability_blocked and result.capability_blocked[0][0] == task_id
    assert "reroute to 'fleet-engineer'" in result.capability_blocked[0][1]
    assert task.status == "blocked"
    assert task.block_kind == "capability"


def test_dispatcher_capability_guard_is_dry_run_only(isolated_kanban_home, monkeypatch):
    kb = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="write file",
            body="Write the generated file.",
            assignee="jarvis-os-pm",
        )

    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "_profile_cli_toolsets", lambda _name: {"kanban"})
    monkeypatch.setattr(kbd, "_capability_reroute_profile", lambda _board: "fleet-engineer")

    with kbc.connect_closing() as conn:
        result = kbd.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 1, dry_run=True)
        task = kb.get_task(conn, task_id)

    assert result.capability_blocked and result.capability_blocked[0][0] == task_id
    assert task.status == "ready"
