import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from hermes_cli import web_server


def _init_board_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                last_heartbeat_at INTEGER,
                current_run_id INTEGER,
                session_id TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                profile TEXT,
                status TEXT NOT NULL,
                worker_pid INTEGER,
                started_at INTEGER NOT NULL,
                last_heartbeat_at INTEGER,
                outcome TEXT,
                summary TEXT
            );
            """
        )


def test_control_center_summary_is_read_only_and_aggregates_core_boards(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    now = int(time.time())

    for board in ("jarvis-os", "sycode-ai", "sycode-trading", "upero"):
        db_path = home / "kanban" / "boards" / board / "kanban.db"
        _init_board_db(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, priority, created_at, started_at, last_heartbeat_at, current_run_id, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"t_{board}",
                    f"{board} in flight",
                    "builder",
                    "running",
                    2,
                    now - 60,
                    now - 30,
                    now - 5,
                    1,
                    f"sess-{board}",
                ),
            )
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, priority, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"ready_{board}", f"{board} ready", "pm", "ready", 1, now - 120),
            )
            conn.execute(
                "INSERT INTO task_runs (id, task_id, profile, status, worker_pid, started_at, last_heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, f"t_{board}", "builder", "running", 1234, now - 30, now - 5),
            )

    monkeypatch.setattr(
        web_server,
        "_call_cron_for_profile",
        lambda profile, func_name, include_disabled=True: [
            {
                "id": "cron-health",
                "name": "dgx-health-watch",
                "profile": "jarvis",
                "schedule_display": "every 5m",
                "last_status": "success",
                "last_run_at": "2026-06-18T10:00:00Z",
                "next_run_at": "2026-06-18T10:05:00Z",
                "enabled": True,
            },
            {
                "id": "cron-fleet",
                "name": "fleet-status-refresh",
                "profile": "jarvis-os-pm",
                "schedule_display": "every 15m",
                "last_status": "failed",
                "last_run_at": "2026-06-18T09:45:00Z",
                "next_run_at": "2026-06-18T10:00:00Z",
                "enabled": True,
            },
        ],
    )

    # Live trace tails should be bounded and secret-like values redacted before leaving the API.
    trace_dir = home / "sessions"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "worker.jsonl").write_text(
        json.dumps({
            "role": "assistant",
            "content": "working with sk-live-secret-token",
        })
        + "\n",
        encoding="utf-8",
    )

    client = TestClient(web_server.app)
    response = client.get(
        "/api/control-center",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert [board["slug"] for board in body["boards"]] == [
        "jarvis-os",
        "sycode-ai",
        "sycode-trading",
        "upero",
    ]
    assert body["boards"][0]["counts"]["running"] == 1
    assert body["status"]["version"]
    assert body["profiles"]
    assert body["profiles"][0]["name"] == "default"
    assert set(body["profiles"][0]).issuperset({"name", "path", "is_default", "model", "provider", "has_env", "skill_count", "gateway_running"})
    assert body["boards"][0]["counts"]["ready"] == 1
    assert body["boards"][0]["in_flight"][0] == {
        "id": "t_jarvis-os",
        "title": "jarvis-os in flight",
        "assignee": "builder",
        "status": "running",
        "priority": 2,
        "started_at": now - 30,
        "last_heartbeat_at": now - 5,
        "current_run_id": 1,
        "session_id": "sess-jarvis-os",
    }
    assert body["cron_jobs"] == [
        {
            "id": "cron-health",
            "name": "dgx-health-watch",
            "profile": "jarvis",
            "schedule": "every 5m",
            "last_status": "success",
            "last_run_at": "2026-06-18T10:00:00Z",
            "next_run_at": "2026-06-18T10:05:00Z",
            "enabled": True,
        },
        {
            "id": "cron-fleet",
            "name": "fleet-status-refresh",
            "profile": "jarvis-os-pm",
            "schedule": "every 15m",
            "last_status": "failed",
            "last_run_at": "2026-06-18T09:45:00Z",
            "next_run_at": "2026-06-18T10:00:00Z",
            "enabled": True,
        },
    ]
    assert body["dgx_health"]["last_status"] == "success"
    assert body["voice_escalation"]["state"] in {"unknown", "ready", "degraded"}
    assert body["live_traces"][0]["lines"][0]["content"] == "working with [REDACTED]"
    assert "prompt" not in body["cron_jobs"][0]
    assert "body" not in body["boards"][0]["in_flight"][0]
