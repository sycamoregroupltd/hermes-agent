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


def test_control_center_uses_root_sources_when_hosted_from_profile_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    jarvis_home = home / "profiles" / "jarvis"
    monkeypatch.setenv("HERMES_HOME", str(jarvis_home))
    now = int(time.time())

    board_db = home / "kanban" / "boards" / "jarvis-os" / "kanban.db"
    _init_board_db(board_db)
    with sqlite3.connect(board_db) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t_rooted", "rooted board task", "builder", "running", 5, now - 60, now - 30),
        )

    (home / "profiles" / "jarvis-voice").mkdir(parents=True)
    (home / "profiles" / "jarvis-voice" / "response_store.db").write_text("", encoding="utf-8")
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "worker.jsonl").write_text(
        json.dumps({"role": "assistant", "content": "root trace line"}) + "\n",
        encoding="utf-8",
    )

    response = TestClient(web_server.app).get(
        "/api/control-center",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    boards = {board["slug"]: board for board in body["boards"]}
    assert boards["jarvis-os"]["available"] is True
    assert boards["jarvis-os"]["counts"]["running"] == 1
    assert boards["jarvis-os"]["in_flight"][0]["id"] == "t_rooted"
    assert body["voice_escalation"]["state"] == "ready"
    assert body["voice_escalation"]["source"] == str(home / "profiles" / "jarvis-voice" / "response_store.db")
    assert body["live_traces"][0]["path"] == str(home / "sessions" / "worker.jsonl")


def test_control_center_redacts_passwords_and_bearer_tokens():
    redacted = web_server._redact_control_center_text(
        "OPENAI_API_KEY=*** password=hunter2secret Bearer abcdefghijklmnop"
    )

    assert "***" not in redacted
    assert "hunter2secret" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "OPENAI_API_KEY=[REDACTED]" in redacted
    assert "password=[REDACTED]" in redacted
    assert "Bearer [REDACTED]" in redacted


def test_control_center_redacts_quoted_secret_fields():
    redacted = web_server._redact_control_center_text(
        'tool payload {"password":"hunter2secret", "api_key": "***", "token": "tok-jsonsecret"}'
    )

    assert "hunter2secret" not in redacted
    assert "***" not in redacted
    assert "tok-jsonsecret" not in redacted
    assert '"password":"[REDACTED]"' in redacted
    assert '"api_key": "[REDACTED]"' in redacted
    assert '"token": "[REDACTED]"' in redacted


def test_control_center_redacts_quoted_secret_fields_with_spaces():
    redacted = web_server._redact_control_center_text(
        'tool payload {"password":"spaceleak alpha beta gamma", "message": "safe text"}'
    )

    assert "spaceleak" not in redacted
    assert "alpha" not in redacted
    assert "beta" not in redacted
    assert "gamma" not in redacted
    assert '"password":"[REDACTED]"' in redacted
    assert '"message": "safe text"' in redacted


def test_control_center_redacts_unquoted_secret_fields_with_spaces():
    redacted = web_server._redact_control_center_text(
        "log line password: unquotedspace alpha beta gamma\nnext safe"
    )

    assert "unquotedspace" not in redacted
    assert "alpha" not in redacted
    assert "beta" not in redacted
    assert "gamma" not in redacted
    assert "password: [REDACTED]\nnext safe" in redacted


def test_control_center_live_trace_redacts_quoted_secret_fields(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    trace_dir = home / "sessions"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "worker.jsonl").write_text(
        json.dumps({
            "role": "assistant",
            "content": 'tool payload {"password":"spaceleak alpha beta gamma"}',
        })
        + "\n",
        encoding="utf-8",
    )

    response = TestClient(web_server.app).get(
        "/api/control-center",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    content = response.json()["live_traces"][0]["lines"][0]["content"]
    assert "spaceleak" not in content
    assert "alpha" not in content
    assert "beta" not in content
    assert "gamma" not in content
    assert '"password":"[REDACTED]"' in content


def test_control_center_live_trace_redacts_unquoted_secret_fields_with_spaces(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    trace_dir = home / "sessions"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "worker.jsonl").write_text(
        json.dumps({
            "role": "assistant",
            "content": "log line password: unquotedspace alpha beta gamma\nnext safe",
        })
        + "\n",
        encoding="utf-8",
    )

    response = TestClient(web_server.app).get(
        "/api/control-center",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    content = response.json()["live_traces"][0]["lines"][0]["content"]
    assert "unquotedspace" not in content
    assert "alpha" not in content
    assert "beta" not in content
    assert "gamma" not in content
    assert "password: [REDACTED]\nnext safe" in content


def test_control_center_uses_fast_read_only_profile_files(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "profiles" / "jarvis" / "cron").mkdir(parents=True)
    (home / "profiles" / "jarvis" / "config.yaml").write_text(
        "model:\n  provider: xai\n  model: grok-test\n",
        encoding="utf-8",
    )
    (home / "profiles" / "jarvis" / ".env").write_text("XAI_API_KEY=secret\n", encoding="utf-8")
    (home / "profiles" / "jarvis" / "profile.yaml").write_text(
        "description: Jarvis PM lane\ndescription_auto: true\n",
        encoding="utf-8",
    )
    (home / "profiles" / "jarvis" / "skills" / "example").mkdir(parents=True)
    (home / "profiles" / "jarvis" / "skills" / "example" / "SKILL.md").write_text("---\nname: example\n---\n", encoding="utf-8")
    (home / "profiles" / "jarvis" / "cron" / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "health",
                    "name": "dgx-health-watch",
                    "schedule": "every 5m",
                    "last_status": "success",
                    "last_run_at": "2026-06-18T10:00:00Z",
                    "next_run_at": "2026-06-18T10:05:00Z",
                    "enabled": True,
                    "prompt": "do not expose this",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        web_server,
        "_cron_profile_dicts",
        lambda: (_ for _ in ()).throw(AssertionError("slow profile registry should not be used")),
    )

    response = TestClient(web_server.app).get(
        "/api/control-center",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cron_jobs"][0]["profile"] == "jarvis"
    assert "prompt" not in body["cron_jobs"][0]
    profiles = {profile["name"]: profile for profile in body["profiles"]}
    assert profiles["jarvis"] == {
        "name": "jarvis",
        "path": str(home / "profiles" / "jarvis"),
        "is_default": False,
        "model": "grok-test",
        "provider": "xai",
        "has_env": True,
        "skill_count": 1,
        "gateway_running": False,
        "description": "Jarvis PM lane",
        "description_auto": True,
        "has_alias": False,
    }
