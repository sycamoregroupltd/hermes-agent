"""Behavioural tests for the GLOBAL HOOK TIER (agent.shell_hooks).

The card's requirement #3: the regression test must assert BEHAVIOURALLY
(``hermes hooks test`` / ``run_once``), never by reading config.  ``configured``
and ``fires`` are different questions — a hook can be listed in config yet not
fire (allowlist / consent / matcher), or fire without being in the profile's
own config (global tier).  These tests prove the latter by actually RUNNING the
hook script (``run_once``) and checking its side effect (a marker file), for:

  * a profile with NO ``hooks:`` key at all  -> global chain must still fire
  * a profile with ONE hook                 -> global chain must STILL fire
    (profile hooks compose with the global chain rather than replacing it)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent import shell_hooks
from hermes_cli import plugins


@pytest.fixture(autouse=True)
def _reset_registration_state():
    shell_hooks.reset_for_tests()
    yield
    shell_hooks.reset_for_tests()


def _write_hook_script(tmp_path: Path, name: str, marker: Path) -> Path:
    """A hook script that records its own firing by touching a marker file."""
    path = tmp_path / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {marker}\n"
        "exit 0\n"
    )
    path.chmod(0o755)
    return path


def _make_global_hooks_file(home: Path, script: Path) -> Path:
    """Write the fleet-wide ``global-hooks.yaml`` at the Hermes root."""
    g = home / shell_hooks.GLOBAL_HOOKS_FILENAME
    g.write_text(
        "on_session_start:\n"
        f"  - command: {script}\n"
    )
    return g


class TestGlobalHookTierFiresForEveryProfile:
    """Global hooks fire for every profile regardless of its own hooks."""

    def test_profile_with_no_hooks_key_still_fires_global_chain(
        self, tmp_path, monkeypatch,
    ):
        """A profile that defines NO hooks at all must still fire the global
        chain.  This is the 'new profile ships ungated' regression."""

        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        marker = tmp_path / "global-fired.marker"
        global_script = _write_hook_script(tmp_path, "global_hook.sh", marker)
        _make_global_hooks_file(home, global_script)

        plugins._plugin_manager = plugins.PluginManager()

        # Profile config with NO hooks key whatsoever.
        registered = shell_hooks.register_from_config(
            {"model": {"default": "x"}}, accept_hooks=True,
        )

        # The global hook must be registered (not just 'configured').
        assert any(
            spec.command == str(global_script)
            and spec.event == "on_session_start"
            for spec in registered
        ), "global hook was not registered for a hooks-less profile"

        # AND it must actually FIRE — behavioural proof via run_once.
        global_spec = next(
            s for s in registered
            if s.command == str(global_script)
        )
        result = shell_hooks.run_once(global_spec, {"session_id": "test"})
        assert result.get("returncode") == 0
        assert marker.exists(), "global hook did not actually fire"

    def test_profile_with_one_hook_still_fires_global_chain(
        self, tmp_path, monkeypatch,
    ):
        """A profile that defines ONE hook must still fire the global chain —
        profile hooks COMPOSE with the global chain, not replace it."""

        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        marker = tmp_path / "global-fired.marker"
        global_script = _write_hook_script(tmp_path, "global_hook.sh", marker)
        _make_global_hooks_file(home, global_script)

        profile_marker = tmp_path / "profile-fired.marker"
        profile_script = _write_hook_script(
            tmp_path, "profile_hook.sh", profile_marker,
        )

        plugins._plugin_manager = plugins.PluginManager()

        registered = shell_hooks.register_from_config(
            {
                "model": {"default": "x"},
                "hooks": {
                    "on_session_start": [{"command": str(profile_script)}],
                },
            },
            accept_hooks=True,
        )

        # BOTH the profile hook AND the global hook must be registered.
        assert any(
            spec.command == str(profile_script)
            for spec in registered
        ), "profile hook was not registered"
        assert any(
            spec.command == str(global_script)
            for spec in registered
        ), "global hook was dropped when the profile defined its own hook"

        # Both must actually fire.
        for spec in registered:
            result = shell_hooks.run_once(spec, {"session_id": "test"})
            assert result.get("returncode") == 0

        assert profile_marker.exists(), "profile hook did not fire"
        assert marker.exists(), "global hook did not fire alongside profile hook"

    def test_iter_configured_hooks_returns_global_before_profile(
        self, tmp_path, monkeypatch,
    ):
        """``hermes hooks list`` / ``doctor`` must show the global tier ahead
        of the profile's own hooks, matching runtime order."""

        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        marker = tmp_path / "global-fired.marker"
        global_script = _write_hook_script(tmp_path, "global_hook.sh", marker)
        _make_global_hooks_file(home, global_script)

        profile_script = _write_hook_script(
            tmp_path, "profile_hook.sh", tmp_path / "profile.marker",
        )

        specs = shell_hooks.iter_configured_hooks(
            {"hooks": {"on_session_start": [{"command": str(profile_script)}]}}
        )
        commands = [s.command for s in specs]
        assert str(global_script) in commands
        assert str(profile_script) in commands
        # Global tier must come first.
        assert commands.index(str(global_script)) < commands.index(
            str(profile_script)
        )

    def test_absent_global_file_fails_open_to_empty(
        self, tmp_path, monkeypatch,
    ):
        """No global-hooks.yaml present -> iter_configured_hooks returns just
        the profile's hooks; registration of the profile hook still works."""

        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        # No global-hooks.yaml written.

        profile_script = _write_hook_script(
            tmp_path, "profile_hook.sh", tmp_path / "profile.marker",
        )
        plugins._plugin_manager = plugins.PluginManager()

        registered = shell_hooks.register_from_config(
            {"hooks": {"on_session_start": [{"command": str(profile_script)}]}},
            accept_hooks=True,
        )
        assert [s.command for s in registered] == [str(profile_script)]

    def test_malformed_global_file_fails_open(
        self, tmp_path, monkeypatch,
    ):
        """A broken global-hooks.yaml must not crash registration or block the
        profile's own hooks — it fails open to zero global hooks."""

        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        g = home / shell_hooks.GLOBAL_HOOKS_FILENAME
        g.write_text(":: not : valid: yaml: [")

        profile_script = _write_hook_script(
            tmp_path, "profile_hook.sh", tmp_path / "profile.marker",
        )
        plugins._plugin_manager = plugins.PluginManager()

        registered = shell_hooks.register_from_config(
            {"hooks": {"on_session_start": [{"command": str(profile_script)}]}},
            accept_hooks=True,
        )
        assert [s.command for s in registered] == [str(profile_script)]

    def test_global_hooks_obey_safe_mode(self, tmp_path, monkeypatch):
        """HERMES_SAFE_MODE=1 skips even global hooks — a troubleshooting run
        fires zero user-configured code."""

        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_SAFE_MODE", "1")

        marker = tmp_path / "global-fired.marker"
        global_script = _write_hook_script(tmp_path, "global_hook.sh", marker)
        _make_global_hooks_file(home, global_script)

        plugins._plugin_manager = plugins.PluginManager()

        registered = shell_hooks.register_from_config(
            {"model": {"default": "x"}}, accept_hooks=True,
        )
        assert registered == []
        assert not marker.exists()
