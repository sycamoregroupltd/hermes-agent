"""End-to-end guard for selected-profile Kanban context inheritance.

The child is the real ``python -m hermes_cli.main -p ... -z`` entrypoint. A
local OpenAI-compatible SSE server makes the model deterministic: it requests
``kanban_complete`` only when the child exposes that tool, then the test reads
the isolated board back through the DB layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hermes_cli import kanban_db as kb


class _FakeChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    saw_kanban = False
    tool_call_sent = False

    def do_POST(self):  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size) or b"{}")
        names = [
            tool.get("function", {}).get("name", "")
            for tool in request.get("tools", [])
        ]
        if "kanban_complete" in names:
            self.__class__.saw_kanban = True

        if "kanban_complete" in names and not self.__class__.tool_call_sent:
            self.__class__.tool_call_sent = True
            deltas = [
                {"role": "assistant"},
                {"tool_calls": [{
                    "index": 0,
                    "id": "call_identity_probe",
                    "type": "function",
                    "function": {
                        "name": "kanban_complete",
                        "arguments": json.dumps({
                            "summary": "DISPATCHER_MARKER_ACCEPTED",
                        }),
                    },
                }]},
                {},
            ]
            finish = "tool_calls"
        else:
            deltas = [{"role": "assistant"}, {"content": "SMOKE_OK"}, {}]
            finish = "stop"

        events = []
        for index, delta in enumerate(deltas):
            payload = {
                "id": "identity-probe",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "smoke-model",
                "choices": [{
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish if index == len(deltas) - 1 else None,
                }],
            }
            events.append(f"data: {json.dumps(payload)}\n\n")
        events.append("data: [DONE]\n\n")
        body = "".join(events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, format, *args):  # noqa: A002 - stdlib handler API
        del format, args


def _config(port: int) -> str:
    return f"""model:
  default: smoke-model
  provider: custom
  base_url: http://127.0.0.1:{port}/v1
  api_key: smoke-key
toolsets: []
agent:
  max_turns: 2
"""


def _start_server():
    _FakeChatHandler.saw_kanban = False
    _FakeChatHandler.tool_call_sent = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _claimed_task(home: Path, db: Path, assignee: str):
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="isolated identity probe",
            assignee=assignee,
            initial_status="blocked",
            board="sandbox",
        )
        assert kb.unblock_task(conn, task_id)
        task = kb.claim_task(conn, task_id, claimer="identity-probe-lock")
        assert task is not None
        return task
    finally:
        conn.close()


def _read_task(db: Path, task_id: str) -> dict:
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        return {
            "status": task.status,
            "summary": kb.latest_summary(conn, task_id),
            "comments": [comment.body for comment in kb.list_comments(conn, task_id)],
        }
    finally:
        conn.close()


def _run_selected_profile(
    source_root: Path,
    home: Path,
    db: Path,
    task,
    *,
    marker: str | None = None,
):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("HERMES_KANBAN_"):
            env.pop(key, None)
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_PROFILE": "worker-a",
        "HERMES_KANBAN_TASK": task.id,
        "HERMES_KANBAN_DB": str(db),
        "HERMES_KANBAN_BOARD": "sandbox",
        "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
        "HERMES_KANBAN_CLAIM_LOCK": "identity-probe-lock",
        "HERMES_TEST_ISOLATION": str(home),
        "PYTHONPATH": str(source_root),
        "HERMES_SKIP_UPDATE_CHECK": "1",
    })
    if marker is not None:
        env["HERMES_KANBAN_DISPATCHER_SPAWN"] = marker
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "-p",
            "worker-b",
            "-z",
            "smoke",
        ],
        cwd=source_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _prepare_profiles(home: Path, port: int):
    for name in ("worker-a", "worker-b"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(_config(port), encoding="utf-8")


def test_real_selected_profile_scrubs_ambient_kanban_context(tmp_path, monkeypatch):
    """An ad-hoc ``-p worker-b`` child cannot mutate worker-a's task."""
    home = tmp_path / ".hermes"
    db = tmp_path / "sandbox.db"
    server, thread = _start_server()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "sandbox")
    _prepare_profiles(home, server.server_address[1])
    task = _claimed_task(home, db, "worker-a")

    try:
        result = _run_selected_profile(
            Path(__file__).resolve().parents[2], home, db, task,
        )
        state = _read_task(db, task.id)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 0
    assert result.stdout.strip() == "SMOKE_OK"
    assert "ignoring inherited Kanban task context" in result.stderr
    assert _FakeChatHandler.saw_kanban is False
    assert state == {"status": "running", "summary": None, "comments": []}


def test_dispatcher_marker_keeps_context_for_selected_worker(tmp_path, monkeypatch):
    """A dispatcher-marked worker retains its own task context exactly once."""
    home = tmp_path / ".hermes"
    db = tmp_path / "sandbox.db"
    server, thread = _start_server()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "sandbox")
    _prepare_profiles(home, server.server_address[1])
    task = _claimed_task(home, db, "worker-b")

    try:
        result = _run_selected_profile(
            Path(__file__).resolve().parents[2], home, db, task, marker="worker-b",
        )
        state = _read_task(db, task.id)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 0
    assert result.stdout.strip() == "SMOKE_OK"
    assert result.stderr == ""
    assert _FakeChatHandler.saw_kanban is True
    assert state == {
        "status": "done",
        "summary": "DISPATCHER_MARKER_ACCEPTED",
        "comments": [],
    }
