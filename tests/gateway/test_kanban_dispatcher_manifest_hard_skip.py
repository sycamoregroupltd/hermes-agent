"""Regression tests: boards-manifest denied/dormant hard-skip in the embedded
kanban dispatcher (`_KanbanDispatcher`, gateway/kanban_watchers_dispatcher.py).

Bug: the embedded gateway dispatcher enumerated every live board and
dispatched from all of them unconditionally, never consulting
``boards-manifest.json``. A board marked ``state: denied`` or
``state: dormant`` (e.g. orchestrator-sync — "Permanent deny — dispatching it
spawns phantom workers on protocol cards") was protected from spawning only
by happening to have zero ready-status tasks; any ready task landing on such
a board would be dispatched.

Fix: `_KanbanDispatcher._board_slugs()` — the single seam `tick_once()`,
`ready_nonempty()`, and `auto_decompose_tick()` all go through — now
hard-skips any board whose manifest state is denied/dormant, with a
fail-open contract matching ``scripts/fleet_boards.py``'s own
``ManifestError`` fallback exactly: a missing/unreadable/corrupt manifest
(or a fleet install with no ``fleet_boards.py`` at all) must never stop
dispatch fleet-wide — it degrades to "no manifest opinion", i.e. the prior
dispatch-everything behavior, not a hard stop.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gateway.kanban_watchers_dispatcher as kwd


# Minimal stand-in for scripts/fleet_boards.py's manifest()/ManifestError
# contract. That script is a fleet-operations file under HERMES_HOME/scripts
# (host-specific, not part of this repo), so tests supply their own copy of
# just the interface the dispatcher relies on: manifest() raises
# ManifestError on any read/parse failure, otherwise returns
# {"boards": {...}}. This mirrors the production script's fail-open shape
# exactly (same exception type name, same "unreadable/corrupt -> raise"
# behavior) without depending on host-local files.
_FAKE_FLEET_BOARDS = textwrap.dedent(
    '''
    import json
    import os
    from pathlib import Path

    MANIFEST_PATH = Path(os.environ.get("HERMES_BOARDS_MANIFEST", "/nonexistent/boards-manifest.json"))


    class ManifestError(RuntimeError):
        pass


    def manifest(path=None):
        p = path or MANIFEST_PATH
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot read boards manifest {p}: {exc}") from exc
        if not isinstance(data.get("boards"), dict):
            raise ManifestError(f"boards manifest {p} has no boards object")
        return data
    '''
)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A scratch Hermes root with a fake scripts/fleet_boards.py installed.

    Sets ``HERMES_HOME`` to *tmp_path* directly (as a root, not a
    profile-scoped path) via ``monkeypatch.setenv``. ``_fleet_boards_module()``
    resolves via ``get_default_hermes_root()``, which reads ``os.environ``
    directly (not the context-local override ``set_hermes_home_override()``
    sets) — tests must exercise the same env-var path production gateway
    processes actually use.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "fleet_boards.py").write_text(_FAKE_FLEET_BOARDS, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kwd._fleet_boards_module.cache_clear()
    monkeypatch.delenv("HERMES_BOARDS_MANIFEST", raising=False)
    yield tmp_path
    kwd._fleet_boards_module.cache_clear()


def _write_manifest(tmp_path: Path, boards: dict, monkeypatch) -> Path:
    manifest_path = tmp_path / "boards-manifest.json"
    manifest_path.write_text(json.dumps({"boards": boards}), encoding="utf-8")
    monkeypatch.setenv("HERMES_BOARDS_MANIFEST", str(manifest_path))
    return manifest_path


def _fake_kb(slugs):
    kb = MagicMock()
    kb.list_boards.return_value = [{"slug": s} for s in slugs]
    kb.DEFAULT_BOARD = "default"
    kb.kanban_db_path.side_effect = lambda slug: Path(f"/nonexistent/{slug}/kanban.db")
    return kb


def _dispatcher(kb):
    settings = kwd._DispatcherSettings(
        interval=60.0,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=3,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )
    return kwd._KanbanDispatcher(kb, settings)


def _wire_fake_dispatch(monkeypatch, dispatch_calls):
    """Patch `_kbc`/`_kbd` so tick_once_for_board never touches a real DB."""

    class _FakeConn:
        def close(self):
            pass

    class _FakeKbc:
        def connect(self, board):
            return _FakeConn()

    class _FakeKbd:
        def dispatch_once(self, conn, board, **kwargs):
            dispatch_calls.append(board)
            return MagicMock(spawned=[], reclaimed=0, crashed=[], timed_out=[], promoted=0, auto_blocked=[])

    monkeypatch.setattr(kwd, "_kbc", lambda: _FakeKbc())
    monkeypatch.setattr(kwd, "_kbd", lambda: _FakeKbd())


# --- (a) denied board is skipped ---------------------------------------


def test_denied_board_skipped_by_tick_once(hermes_home, monkeypatch, tmp_path):
    _write_manifest(
        tmp_path,
        {
            "orchestrator-sync": {"state": "denied", "dispatch": False, "reason": "permanent deny"},
            "jarvis-os": {"state": "active", "dispatch": True},
        },
        monkeypatch,
    )
    kb = _fake_kb(["orchestrator-sync", "jarvis-os"])
    dispatcher = _dispatcher(kb)
    dispatch_calls: list[str] = []
    _wire_fake_dispatch(monkeypatch, dispatch_calls)

    results = dispatcher.tick_once()

    assert [slug for slug, _ in results] == ["jarvis-os"]
    assert dispatch_calls == ["jarvis-os"]
    assert "orchestrator-sync" not in dispatch_calls


# --- (b) dormant board is skipped the same way --------------------------


def test_dormant_board_skipped_by_tick_once(hermes_home, monkeypatch, tmp_path):
    _write_manifest(
        tmp_path,
        {
            "quicknote": {"state": "dormant", "dispatch": False, "reason": "historical only"},
            "jarvis-os": {"state": "active", "dispatch": True},
        },
        monkeypatch,
    )
    kb = _fake_kb(["quicknote", "jarvis-os"])
    dispatcher = _dispatcher(kb)
    dispatch_calls: list[str] = []
    _wire_fake_dispatch(monkeypatch, dispatch_calls)

    results = dispatcher.tick_once()

    assert [slug for slug, _ in results] == ["jarvis-os"]
    assert dispatch_calls == ["jarvis-os"]
    assert "quicknote" not in dispatch_calls


# --- (c) active board with dispatch:true still ticks normally -----------


def test_active_board_not_false_positive_skipped(hermes_home, monkeypatch, tmp_path):
    _write_manifest(
        tmp_path,
        {
            "jarvis-os": {"state": "active", "dispatch": True},
            "sycode-trading": {"state": "active", "dispatch": True},
        },
        monkeypatch,
    )
    kb = _fake_kb(["jarvis-os", "sycode-trading"])
    dispatcher = _dispatcher(kb)
    dispatch_calls: list[str] = []
    _wire_fake_dispatch(monkeypatch, dispatch_calls)

    results = dispatcher.tick_once()

    assert sorted(slug for slug, _ in results) == ["jarvis-os", "sycode-trading"]
    assert sorted(dispatch_calls) == ["jarvis-os", "sycode-trading"]


# --- (d) missing/corrupt manifest -> fail-open (prior dispatch-everything) ---


def test_missing_manifest_fails_open_dispatches_everything(hermes_home, monkeypatch, tmp_path):
    # HERMES_BOARDS_MANIFEST left unset -> fake fleet_boards.py's default
    # path (/nonexistent/boards-manifest.json) doesn't exist -> ManifestError.
    kb = _fake_kb(["orchestrator-sync", "jarvis-os"])
    dispatcher = _dispatcher(kb)
    dispatch_calls: list[str] = []
    _wire_fake_dispatch(monkeypatch, dispatch_calls)

    results = dispatcher.tick_once()

    # Fail-open: no manifest opinion available -> nothing hard-skipped,
    # dispatcher degrades to dispatching every live board (prior behavior).
    assert sorted(slug for slug, _ in results) == ["jarvis-os", "orchestrator-sync"]
    assert sorted(dispatch_calls) == ["jarvis-os", "orchestrator-sync"]


def test_corrupt_manifest_fails_open_dispatches_everything(hermes_home, monkeypatch, tmp_path):
    manifest_path = tmp_path / "boards-manifest.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("HERMES_BOARDS_MANIFEST", str(manifest_path))

    kb = _fake_kb(["orchestrator-sync", "jarvis-os"])
    dispatcher = _dispatcher(kb)
    dispatch_calls: list[str] = []
    _wire_fake_dispatch(monkeypatch, dispatch_calls)

    results = dispatcher.tick_once()

    assert sorted(slug for slug, _ in results) == ["jarvis-os", "orchestrator-sync"]
    assert sorted(dispatch_calls) == ["jarvis-os", "orchestrator-sync"]


def test_missing_fleet_boards_script_fails_open(monkeypatch, tmp_path):
    """No scripts/fleet_boards.py at all (base install) -> also fails open."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no scripts/ dir created
    kwd._fleet_boards_module.cache_clear()
    try:
        kb = _fake_kb(["orchestrator-sync", "jarvis-os"])
        dispatcher = _dispatcher(kb)
        dispatch_calls: list[str] = []
        _wire_fake_dispatch(monkeypatch, dispatch_calls)

        results = dispatcher.tick_once()

        assert sorted(slug for slug, _ in results) == ["jarvis-os", "orchestrator-sync"]
    finally:
        kwd._fleet_boards_module.cache_clear()


# --- (e) profile-scoped HERMES_HOME still finds the shared-root script ----
#
# t_30b69b42 review round 2: production resolved fleet_boards.py via
# get_hermes_home(), which is profile-scoped. Every live gateway runs with
# HERMES_HOME=<root>/profiles/<name> (e.g. --profile jarvis-os-pm), and
# scripts/fleet_boards.py only ever exists at <root>/scripts/, never under
# a profile dir. That mismatch made _fleet_boards_module() return None on
# every real gateway process, silently defeating the hard-skip fleet-wide
# while every other test here (which points HERMES_HOME straight at a root
# containing scripts/) stayed green. This test pins HERMES_HOME to a
# profile subdirectory and asserts the hard-skip still fires by finding
# fleet_boards.py at the root two levels up, exactly like
# hermes_cli/kanban_db.py::kanban_home() (get_default_hermes_root()) already
# does for the kanban board root itself.


def test_denied_board_skipped_with_profile_scoped_hermes_home(monkeypatch, tmp_path):
    root = tmp_path
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "fleet_boards.py").write_text(_FAKE_FLEET_BOARDS, encoding="utf-8")
    # Marker file so get_default_hermes_root() recognizes *root* as a real
    # Hermes home even though it's a throwaway tmp_path, not ~/.hermes.
    (root / "config.yaml").write_text("{}", encoding="utf-8")

    profile_home = root / "profiles" / "some-profile"
    profile_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))  # profile-scoped, like every live gateway
    monkeypatch.delenv("HERMES_BOARDS_MANIFEST", raising=False)
    kwd._fleet_boards_module.cache_clear()
    try:
        _write_manifest(
            root,
            {
                "orchestrator-sync": {"state": "denied", "dispatch": False, "reason": "permanent deny"},
                "jarvis-os": {"state": "active", "dispatch": True},
            },
            monkeypatch,
        )
        kb = _fake_kb(["orchestrator-sync", "jarvis-os"])
        dispatcher = _dispatcher(kb)
        dispatch_calls: list[str] = []
        _wire_fake_dispatch(monkeypatch, dispatch_calls)

        results = dispatcher.tick_once()

        assert [slug for slug, _ in results] == ["jarvis-os"]
        assert dispatch_calls == ["jarvis-os"]
        assert "orchestrator-sync" not in dispatch_calls
    finally:
        kwd._fleet_boards_module.cache_clear()


# --- direct unit coverage of the manifest reader -------------------------


def test_manifest_hard_skip_boards_reads_denied_and_dormant(hermes_home, monkeypatch, tmp_path):
    _write_manifest(
        tmp_path,
        {
            "orchestrator-sync": {"state": "denied"},
            "quicknote": {"state": "dormant"},
            "jarvis-os": {"state": "active"},
        },
        monkeypatch,
    )
    assert kwd._manifest_hard_skip_boards() == frozenset({"orchestrator-sync", "quicknote"})
