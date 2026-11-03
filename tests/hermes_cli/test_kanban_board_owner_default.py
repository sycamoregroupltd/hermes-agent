"""Board-owner default for Kanban cards created without an assignee.

One rule for every create entry point (``kanban_db.create_task``, the
``hermes kanban create`` CLI, the ``kanban_create`` model tool):

* explicit assignee -> preserved;
* no assignee + triage -> unassigned triage card (no default, no dispatch);
* no assignee + not triage -> the target board's ``owner`` from
  ``<kanban root>/kanban/boards-manifest.json``; any unresolvable owner fails
  the create and inserts no row.

Hermetic: temp HERMES_HOME, temp board DBs + manifest, no network.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_board_owner import BoardOwnerError, boards_manifest_path

OWNER_A = "board-pm-alpha"
OWNER_B = "ops-lead"
EXPLICIT = "researcher-x"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    for profile in (OWNER_A, OWNER_B, EXPLICIT):
        pdir = root / "profiles" / profile
        pdir.mkdir(parents=True)
        (pdir / "config.yaml").write_text("{}\n", encoding="utf-8")
    kb.init_db()
    kb.create_board("alpha")
    kb.create_board("beta")
    return root


def _write_manifest(boards, *, raw: str | None = None) -> Path:
    path = boards_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"boards": boards}), encoding="utf-8")
    return path


def _standard_manifest() -> Path:
    return _write_manifest({
        "alpha": {"owner": OWNER_A, "state": "active"},
        "beta": {"owner": OWNER_B, "state": "active"},
    })


def _rows(board: str) -> list:
    with kbc.connect_closing(board=board) as conn:
        return kb.list_tasks(conn)


def _create(board: str, **kw) -> str:
    with kbc.connect_closing(board=board) as conn:
        return kb.create_task(conn, title=kw.pop("title", "card"), board=board, **kw)


def _assignee(board: str, task_id: str):
    with kbc.connect_closing(board=board) as conn:
        task = kb.get_task(conn, task_id)
    return task.assignee, task.status


# ---------------------------------------------------------------------------
# Shared create path (kanban_db.create_task)
# ---------------------------------------------------------------------------

def test_manifest_lives_under_shared_kanban_root(home):
    assert boards_manifest_path() == home / "kanban" / "boards-manifest.json"


def test_missing_non_triage_assignee_routes_to_board_owner(home):
    _standard_manifest()
    tid = _create("alpha")
    assert _assignee("alpha", tid) == (OWNER_A, "ready")


def test_explicit_assignee_preserved_even_when_manifest_unreadable(home):
    _standard_manifest()
    tid = _create("alpha", assignee=EXPLICIT)
    assert _assignee("alpha", tid)[0] == EXPLICIT
    # The manifest is never consulted for an explicit assignee.
    _write_manifest(None, raw="{not json")
    tid2 = _create("beta", assignee=EXPLICIT)
    assert _assignee("beta", tid2)[0] == EXPLICIT


def test_triage_without_assignee_stays_unassigned(home):
    _standard_manifest()
    tid = _create("alpha", triage=True)
    assert _assignee("alpha", tid) == (None, "triage")
    # Triage ignores the manifest entirely, even a broken one.
    _write_manifest(None, raw="{not json")
    tid2 = _create("alpha", triage=True)
    assert _assignee("alpha", tid2) == (None, "triage")


def test_two_boards_resolve_their_own_owners(home):
    _standard_manifest()
    a, b = _create("alpha"), _create("beta")
    assert _assignee("alpha", a)[0] == OWNER_A
    assert _assignee("beta", b)[0] == OWNER_B


def test_scoped_current_board_resolves_that_boards_owner(home):
    """`--board` scoping (context override) with no board= argument."""
    _standard_manifest()
    with kb.scoped_current_board("beta"):
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="scoped")
    assert _assignee("beta", tid)[0] == OWNER_B


def test_owner_name_is_canonicalized(home):
    _write_manifest({"alpha": {"owner": "  Board-PM-Alpha "}})
    tid = _create("alpha")
    assert _assignee("alpha", tid)[0] == OWNER_A


_BROKEN = {
    "board_absent": dict(boards={"beta": {"owner": OWNER_B}}),
    "owner_absent": dict(boards={"alpha": {"state": "active"}}),
    "owner_null": dict(boards={"alpha": {"owner": None}}),
    "owner_blank": dict(boards={"alpha": {"owner": "  "}}),
    "owner_list_ambiguous": dict(boards={"alpha": {"owner": [OWNER_A, OWNER_B]}}),
    "entry_not_object": dict(boards={"alpha": OWNER_A}),
    "case_duplicate_ambiguous": dict(boards={"alpha": {"owner": OWNER_A}, "ALPHA": {"owner": OWNER_B}}),
    "owner_invalid_name": dict(boards={"alpha": {"owner": "not a/profile"}}),
    "owner_not_a_profile": dict(boards={"alpha": {"owner": "ghost-profile"}}),
    "corrupt_json": dict(raw="{not json"),
    "no_boards_object": dict(raw=json.dumps({"owners": {"alpha": OWNER_A}})),
    "boards_not_object": dict(raw=json.dumps({"boards": ["alpha"]})),
    "duplicate_json_key": dict(
        raw=f'{{"boards": {{"alpha": {{"owner": "{OWNER_A}"}}, "alpha": {{"owner": "{OWNER_B}"}}}}}}'),
}


@pytest.mark.parametrize("case", sorted(_BROKEN))
def test_unresolvable_owner_fails_closed_with_no_row(home, case):
    spec = _BROKEN[case]
    _write_manifest(spec.get("boards"), raw=spec.get("raw"))
    with pytest.raises(BoardOwnerError):
        _create("alpha", title=f"must-not-land-{case}")
    assert _rows("alpha") == []


def test_dangling_symlink_manifest_fails_closed(home):
    path = boards_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(home / "nowhere.json", path)
    with pytest.raises(BoardOwnerError, match="not a readable file"):
        _create("alpha")
    assert _rows("alpha") == []


def test_manifest_path_is_directory_fails_closed(home):
    boards_manifest_path().mkdir(parents=True)
    with pytest.raises(BoardOwnerError):
        _create("alpha")
    assert _rows("alpha") == []


def test_no_manifest_keeps_historical_unassigned_behaviour(home):
    """Policy is configured by the manifest's presence; vanilla installs are unchanged."""
    assert not boards_manifest_path().exists()
    tid = _create("alpha")
    assert _assignee("alpha", tid)[0] is None


def test_failure_inside_outer_txn_rolls_back_whole_graph(home):
    """Graph builders nest create_task under one write txn: a fail-closed child
    must not leave a half-built graph behind."""
    _write_manifest({"alpha": {"owner": OWNER_A}})
    with kbc.connect_closing(board="alpha") as conn:
        with pytest.raises(BoardOwnerError):
            with kb.write_txn(conn):
                kb.create_task(conn, title="parent", assignee=EXPLICIT, board="alpha")
                kb.create_task(conn, title="child", board="beta")  # beta: no entry
    assert _rows("alpha") == []


def test_idempotent_replay_returns_existing_row_without_reresolving(home):
    _standard_manifest()
    first = _create("alpha", idempotency_key="k-1")
    assert _assignee("alpha", first)[0] == OWNER_A
    _write_manifest(None, raw="{not json")  # owner now unresolvable
    assert _create("alpha", idempotency_key="k-1") == first
    assert len(_rows("alpha")) == 1
    # A NEW key still fails closed.
    with pytest.raises(BoardOwnerError):
        _create("alpha", idempotency_key="k-2")
    assert len(_rows("alpha")) == 1


def test_pinned_worker_db_resolves_owner_of_pinned_board(home, monkeypatch):
    """Dispatched workers carry HERMES_KANBAN_DB + HERMES_KANBAN_BOARD; the
    owner must come from the board the pinned DB belongs to."""
    _standard_manifest()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="beta")))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="worker child")
    assert _assignee("beta", tid)[0] == OWNER_B


def test_pinned_db_without_board_name_fails_closed(home, monkeypatch):
    _standard_manifest()
    db = kb.kanban_db_path(board="beta")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    with kbc.connect_closing() as conn:
        with pytest.raises(BoardOwnerError, match="HERMES_KANBAN_BOARD"):
            kb.create_task(conn, title="orphan")
    assert _rows("beta") == []


def test_global_default_assignee_is_not_used(home):
    (home / "config.yaml").write_text(
        f"kanban:\n  default_assignee: {EXPLICIT}\n", encoding="utf-8")
    _standard_manifest()
    tid = _create("alpha")
    assert _assignee("alpha", tid)[0] == OWNER_A
    _write_manifest({"beta": {"owner": OWNER_B}})  # alpha now unregistered
    with pytest.raises(BoardOwnerError):
        _create("alpha", title="no-fallback")


# ---------------------------------------------------------------------------
# CLI entry point: `hermes kanban [--board B] create ...`
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc.kanban_command(root.parse_args(["kanban", *argv]))


def test_cli_follows_the_same_rule(home, capsys):
    _standard_manifest()
    assert _cli(["--board", "alpha", "create", "cli-default"]) == 0
    assert _cli(["--board", "beta", "create", "cli-default-b"]) == 0
    assert _cli(["--board", "alpha", "create", "cli-explicit", "--assignee", EXPLICIT]) == 0
    assert _cli(["--board", "alpha", "create", "cli-triage", "--triage"]) == 0
    capsys.readouterr()
    by_title = {t.title: (t.assignee, t.status) for t in _rows("alpha")}
    assert by_title == {
        "cli-default": (OWNER_A, "ready"),
        "cli-explicit": (EXPLICIT, "ready"),
        "cli-triage": (None, "triage"),
    }
    assert [(t.title, t.assignee) for t in _rows("beta")] == [("cli-default-b", OWNER_B)]


def test_cli_fails_closed_with_clear_error_and_no_row(home, capsys):
    _write_manifest({"beta": {"owner": OWNER_B}})
    rc = _cli(["--board", "alpha", "create", "cli-orphan"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "alpha" in err and "boards manifest" in err
    assert _rows("alpha") == []


# ---------------------------------------------------------------------------
# Model-tool entry point: kanban_create
# ---------------------------------------------------------------------------

def _tool(args: dict) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create(args))


def test_tool_follows_the_same_rule(home):
    _standard_manifest()
    owned = _tool({"title": "tool-default", "board": "alpha"})
    blank = _tool({"title": "tool-blank", "board": "beta", "assignee": "  "})
    explicit = _tool({"title": "tool-explicit", "board": "alpha", "assignee": EXPLICIT})
    triage = _tool({"title": "tool-triage", "board": "alpha", "triage": True})
    for out in (owned, blank, explicit, triage):
        assert out.get("ok") is True, out
    assert _assignee("alpha", owned["task_id"]) == (OWNER_A, "ready")
    assert _assignee("beta", blank["task_id"])[0] == OWNER_B
    assert _assignee("alpha", explicit["task_id"])[0] == EXPLICIT
    assert _assignee("alpha", triage["task_id"]) == (None, "triage")


def test_tool_fails_closed_with_clear_error_and_no_row(home):
    _write_manifest({"alpha": {"owner": "ghost-profile"}})
    out = _tool({"title": "tool-orphan", "board": "alpha"})
    assert out.get("ok") is not True
    assert "ghost-profile" in json.dumps(out)
    assert _rows("alpha") == []


def test_cli_and_tool_agree_on_same_inputs(home, capsys):
    """Same board + same (missing) assignee -> same landed assignee on both paths."""
    _standard_manifest()
    assert _cli(["--board", "beta", "create", "via-cli"]) == 0
    capsys.readouterr()
    out = _tool({"title": "via-tool", "board": "beta"})
    landed = {t.title: t.assignee for t in _rows("beta")}
    assert landed["via-cli"] == landed["via-tool"] == OWNER_B
    assert out["ok"] is True
