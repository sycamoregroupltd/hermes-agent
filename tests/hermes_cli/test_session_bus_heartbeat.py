"""A real hermes process writes its own session-bus heartbeat, opt-in only.

The fake busctl.py stands in for the real bus. It is never the real one.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

FAKE_BUSCTL = textwrap.dedent(
    """\
    import argparse
    import os
    import sqlite3

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    hb = sub.add_parser("heartbeat")
    hb.add_argument("--session", required=True)
    hb.add_argument("--state", required=True)
    hb.add_argument("--pid", type=int, required=True)
    hb.add_argument("--card", default="")
    args = parser.parse_args()

    conn = sqlite3.connect(os.environ["FAKE_BUS_DB"])
    conn.execute(
        "CREATE TABLE IF NOT EXISTS beats ("
        "id INTEGER PRIMARY KEY, session TEXT, state TEXT, pid INTEGER, card TEXT)"
    )
    conn.execute(
        "INSERT INTO beats (session, state, pid, card) VALUES (?, ?, ?, ?)",
        (args.session, args.state, args.pid, args.card or None),
    )
    conn.commit()
    conn.close()
    print("ok")
    """
)

# Runs the real hermes entry point. --version is the cheapest command, and
# SystemExit from argparse/version is treated as a normal return.
CHILD = textwrap.dedent(
    """\
    import sys
    sys.argv = ["hermes", "--version"]
    from hermes_cli.main import main
    try:
        main()
    except SystemExit:
        pass
    """
)

_BUS_KEYS = (
    "HERMES_SESSION_BUS_SESSION",
    "HERMES_SESSION_BUS_CTL",
    "HERMES_SESSION_BUS_CARD",
    "HERMES_SESSION_BUS_STATE",
    "HERMES_SESSION_BUS_INTERVAL",
)


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _BUS_KEYS}
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONNOUSERSITE"] = "1"
    return env


@pytest.fixture
def fake_bus(tmp_path):
    bus_dir = tmp_path / "bus"
    bus_dir.mkdir()
    ctl = bus_dir / "busctl.py"
    ctl.write_text(FAKE_BUSCTL, encoding="utf-8")
    db = bus_dir / "bus.db"
    child = bus_dir / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    return {"ctl": ctl, "db": db, "child": child}


def _rows(db: Path, session: str) -> list[tuple]:
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT session, state, pid, card FROM beats WHERE session = ?",
            (session,),
        ).fetchall()
    finally:
        conn.close()


def _total_rows(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM beats").fetchone()[0]
    finally:
        conn.close()


def test_bound_hermes_process_writes_heartbeat_without_shell_loop(tmp_path, fake_bus):
    env = _base_env(tmp_path)
    env.update(
        {
            "HERMES_SESSION_BUS_SESSION": "hb-test-session",
            "HERMES_SESSION_BUS_CTL": str(fake_bus["ctl"]),
            "HERMES_SESSION_BUS_CARD": "jarvis-os/hb-test",
            "HERMES_SESSION_BUS_STATE": "active",
            "HERMES_SESSION_BUS_INTERVAL": "30",
            "FAKE_BUS_DB": str(fake_bus["db"]),
        }
    )

    proc = subprocess.Popen(
        [sys.executable, str(fake_bus["child"])],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=180)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == 0, stderr

    rows = _rows(fake_bus["db"], "hb-test-session")
    assert len(rows) >= 1, stderr
    for session, state, pid, card in rows:
        assert session == "hb-test-session"
        assert state == "active"
        assert card == "jarvis-os/hb-test"
        # Must be the hermes child's pid, not the pytest process.
        assert pid == proc.pid
        assert pid != os.getpid()


def test_unbound_hermes_process_writes_no_heartbeat(tmp_path, fake_bus):
    env = _base_env(tmp_path)
    # Ctl is present but the session is not bound, and vice versa. Neither
    # alone may start a heartbeat; the fake bus must not be touched.
    env["FAKE_BUS_DB"] = str(fake_bus["db"])
    env["HERMES_SESSION_BUS_CTL"] = str(fake_bus["ctl"])

    proc = subprocess.run(
        [sys.executable, str(fake_bus["child"])],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert not fake_bus["db"].exists() or _total_rows(fake_bus["db"]) == 0
