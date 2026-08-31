#!/usr/bin/env python3
"""Independent failure-path checks for ADOPT-5 notepad consumers."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, mock_open

SCRIPTS = Path("/home/frank/.hermes/scripts")
sys.path.insert(0, str(SCRIPTS))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main():
    bridge = load("adopt5_bridge", SCRIPTS / "notepad_state.py")
    store = bridge.NotepadStore("job", "/home/frank/.hermes")
    failed = SimpleNamespace(returncode=7, stdout="", stderr="store unavailable")
    with patch.object(bridge, "_run", return_value=failed):
        try:
            store.get("key")
        except RuntimeError as exc:
            assert "notepad get failed" in str(exc)
        else:
            raise AssertionError("NotepadStore.get did not fail closed")

    dqsh = load("adopt5_dqsh", SCRIPTS / "dqsh_daemon.py")
    dqsh.STATE_FILE = "/home/frank/.hermes/dqsh_state.json"
    dqsh._DQSH_NOTEPAD = SimpleNamespace(get=lambda key: (_ for _ in ()).throw(RuntimeError("broken store")))
    try:
        dqsh.load_state()
    except RuntimeError as exc:
        assert "broken store" in str(exc)
    else:
        raise AssertionError("dqsh.load_state fell back after notepad failure")

    # A malformed notepad value must not fall back to a divergent mirror.
    dqsh._DQSH_NOTEPAD = SimpleNamespace(get=lambda key: "{malformed-notepad")
    with patch(
        "builtins.open",
        mock_open(read_data='{"remediation_history": [{"type": "stale-mirror"}]}'),
    ) as mirror_open:
        try:
            dqsh.load_state()
        except RuntimeError as exc:
            assert "invalid JSON in cron notepad dqsh:state" in str(exc)
            mirror_open.assert_not_called()
        else:
            raise AssertionError("dqsh.load_state accepted malformed notepad via mirror")

    digest = load("adopt5_digest", SCRIPTS / "fleet-daily-digest-to-board.py")
    digest._NOTEPAD = SimpleNamespace(get=lambda key: (_ for _ in ()).throw(RuntimeError("broken store")))
    try:
        digest._load_state()
    except RuntimeError as exc:
        assert "broken store" in str(exc)
    else:
        raise AssertionError("digest loader fell back after notepad failure")

    print("ADOPT5 FAILURE PATH PASS: bridge + dqsh + digest fail closed")


if __name__ == "__main__":
    main()
