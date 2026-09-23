"""Real shell/CLI ingress: inherited context is not a board-write grant."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tools import kanban_tools
from tools.environments.local import LocalEnvironment


ROOT = Path(__file__).resolve().parents[2]


def _worker_board(tmp_path, monkeypatch):
    db = tmp_path / "assigned.db"
    conn = connect(db)
    own, foreign = [kb.create_task(conn, title=title) for title in ("own", "foreign")]
    for tid in (own, foreign):
        kb.claim_task(conn, tid)
    task = kb.get_task(conn, own)
    for key, value in {
        "HERMES_KANBAN_DB": str(db), "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_TASK": own, "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
        "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock, "HOME": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    return conn, own, foreign


def test_terminal_descendants_cannot_mutate_even_after_task_is_removed(tmp_path, monkeypatch):
    conn, own, foreign = _worker_board(tmp_path, monkeypatch)
    script = tmp_path / "descendant.py"
    script.write_text(
        "import os, sys, json, subprocess\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tools import kanban_tools as kt\n"
        "from agent.delegation_context import is_dispatcher_owned_worker_context\n"
        f"own, foreign = {own!r}, {foreign!r}\n"
        "out = {'owner': is_dispatcher_owned_worker_context(), 'default': kt._default_task_id(None),"
        " 'db': os.getenv('HERMES_KANBAN_DB'), 'board': os.getenv('HERMES_KANBAN_BOARD')}\n"
        "out['show'] = json.loads(kt._handle_show({'task_id':own}))\n"
        "out['tools'] = [json.loads(kt._handle_complete({'task_id': t, 'summary':'must refuse'})) for t in (own,foreign)]\n"
        "os.environ.pop('HERMES_KANBAN_TASK', None)\n"
        f"p = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'kanban', 'complete', foreign, '--result', 'must refuse'], cwd={str(ROOT)!r}, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=45)\n"
        "out['later_cli'] = {'rc': p.returncode, 'out':p.stdout, 'err':p.stderr}\n"
        "print('SCOPE_RESULT=' + json.dumps(out))\n"
    )
    terminal = LocalEnvironment(cwd=str(tmp_path))
    try:
        result = terminal.execute(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    finally:
        terminal.cleanup()
    from agent.skill_preprocessing import run_inline_shell
    from agent.shell_hooks import ShellHookSpec, _spawn
    from tools.code_execution_env import _build_child_env
    from tools.mcp_tool_config import _build_safe_env

    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    outputs = [result.get("output", ""), run_inline_shell(command, tmp_path, 45),
               _spawn(ShellHookSpec(event="session:start", command=command, timeout=45), "{}")['stdout']]
    child_envs = [
        _build_child_env(rpc_endpoint="fixture", rpc_token="fixture", tmpdir=str(tmp_path), child_python=sys.executable),
        _build_safe_env({"HERMES_HOME": os.environ["HERMES_HOME"]}),
    ]
    for env in child_envs:
        proc = subprocess.run([sys.executable, str(script)], env=env, cwd=tmp_path,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout)
    for output in outputs:
        row = json.loads(next(line.split("SCOPE_RESULT=", 1)[1] for line in output.splitlines() if "SCOPE_RESULT=" in line))
        assert row["show"]["task"]["id"] == own, row
        assert not row["owner"] and row["default"] is None, row
        assert row["db"] == str(tmp_path / "assigned.db") and row["board"] == "default"
        assert all("error" in value for value in row["tools"]), row
        assert row["later_cli"]["rc"] != 0, row
    assert [kb.get_task(conn, t).status for t in (own, foreign)] == ["running", "running"]
    assert json.loads(kanban_tools._handle_complete({"summary": "parent handoff"}))["ok"]
    assert kb.get_task(conn, own).status == "done"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_worker_cli_cannot_use_foreign_task_to_drop_run_scope(tmp_path, monkeypatch):
    conn, own, foreign = _worker_board(tmp_path, monkeypatch)
    assert "error" in json.loads(kanban_tools._handle_complete({"task_id": foreign, "summary": "no"}))
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "complete", foreign, "--result", "no"],
        cwd=ROOT, env=dict(os.environ), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=45,
    )
    assert proc.returncode != 0 and "worker is scoped to task" in proc.stderr, (proc.stdout, proc.stderr)
    assert kb.get_task(conn, foreign).status == "running"
    attachment = tmp_path / "note.txt"
    attachment.write_text("fixture")
    assert kb.block_task(conn, foreign, reason="fixture awaiting orchestrator")
    for arguments in (["attach", foreign, str(attachment)], ["unblock", foreign]):
        proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *arguments],
                              cwd=ROOT, env=dict(os.environ), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=45)
        assert proc.returncode != 0, (arguments, proc.stdout, proc.stderr)
    assert not kb.list_attachments(conn, foreign)
    assert json.loads(kanban_tools._handle_complete({"task_id": own, "summary": "parent"}))["ok"]
    conn.close()


def _run_descendant_probe(tmp_path, body: str) -> dict:
    """Run *body* in a real descendant process carrying the real fence marker.

    ``delegated_child_subprocess_env`` is the builder every terminal/execute_code child env goes
    through, so the marker (and the pin) this probe sees is the production one.
    """
    from agent.delegation_context import delegated_child_subprocess_env

    script = tmp_path / "descendant_probe.py"
    script.write_text(
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from hermes_cli import kanban_db as kb\n"
        "from hermes_cli.kanban_db_connect import connect\n"
        "row = {'marker': os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')}\n"
        + body +
        "print('SCOPE_RESULT=' + json.dumps(row))\n"
    )
    env = delegated_child_subprocess_env(dict(os.environ))
    proc = subprocess.run([sys.executable, str(script)], env=env, cwd=str(tmp_path),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(next(line.split("SCOPE_RESULT=", 1)[1]
                           for line in proc.stdout.splitlines() if "SCOPE_RESULT=" in line))


_CLI_HELPER = (
    "def cli(*argv, unpin=False):\n"
    "    env = dict(os.environ)\n"
    "    if unpin:\n"
    "        env.pop('HERMES_KANBAN_DB', None); env.pop('HERMES_KANBAN_BOARD', None)\n"
    "    p = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'kanban', *argv],\n"
    "                       cwd=%r, env=env, stdin=subprocess.DEVNULL, capture_output=True,\n"
    "                       text=True, timeout=60)\n"
    "    return {'rc': p.returncode, 'err': p.stderr, 'out': p.stdout}\n" % (str(ROOT),)
)


def test_descendant_cli_can_correct_a_foreign_board_but_not_its_lineage_board(tmp_path, monkeypatch):
    """A named board is the hard boundary: the lineage's board stays fenced for its descendants, a
    different board does not.

    Regression for the root-valued marker (jarvis-os/t_a65a42ae): a worker whose card instructs a
    cross-board correction could not mutate ANY board from its shell, because the marker named
    ``kanban_home()`` — every board lives under it.
    """
    lineage, other = "jarvis-os", "sycode-trading"
    root = tmp_path / "kanban-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    tasks = {}
    for slug in (lineage, other):
        conn = connect(kb.board_dir(slug) / "kanban.db")  # the owner initializes each board
        tasks[slug] = kb.create_task(conn, title=f"{slug} task")
        conn.close()
    own_db = kb.board_dir(lineage) / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_BOARD", lineage)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(own_db))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tasks[lineage])

    row = _run_descendant_probe(
        tmp_path,
        _CLI_HELPER
        + f"own_db, other_db = Path({str(own_db)!r}), Path({str(kb.board_dir(other) / 'kanban.db')!r})\n"
        + "try:\n"
          "    conn = connect(own_db); kb.create_task(conn, title='lineage'); row['module_own'] = 'WROTE'\n"
          "except PermissionError:\n"
          "    row['module_own'] = 'fenced'\n"
          "conn = connect(other_db); row['module_other'] = kb.create_task(conn, title='other')\n"
        + f"row['cli_pinned'] = cli('--board', {other!r}, 'comment', {tasks[other]!r}, 'pinned')\n"
        + f"row['cli_foreign'] = cli('--board', {other!r}, 'comment', {tasks[other]!r}, 'cross-board fix', unpin=True)\n"
        + f"row['cli_own'] = cli('--board', {lineage!r}, 'comment', {tasks[lineage]!r}, 'self', unpin=True)\n"
        + "row['cli_create_board'] = cli('boards', 'create', 'sneaky', unpin=True)\n",
    )

    # The marker names the lineage's BOARD, never the Hermes root every board lives under.
    assert row["marker"] == str(kb.board_dir(lineage)), row
    assert row["module_own"] == "fenced", row
    assert row["module_other"], row
    # Pinned: --board cannot address another board (the pin wins resolution), so the refusal is right.
    assert row["cli_pinned"]["rc"] != 0, row
    # Unpinned: the explicitly requested board is writable...
    assert row["cli_foreign"]["rc"] == 0, row["cli_foreign"]
    # ...while the lineage's own board and root-level board structure stay refused.
    assert row["cli_own"]["rc"] != 0 and "delegate_task child contexts" in row["cli_own"]["err"], row["cli_own"]
    assert row["cli_create_board"]["rc"] != 0, row["cli_create_board"]
    assert not kb.board_dir("sneaky").exists()

    other_conn = connect(kb.board_dir(other) / "kanban.db")
    bodies = [c.body for c in kb.list_comments(other_conn, tasks[other])]
    assert bodies == ["cross-board fix"], bodies
    other_conn.close()
    own_conn = connect(own_db)
    assert not kb.list_comments(own_conn, tasks[lineage])
    assert kb.get_task(own_conn, tasks[lineage]).status == "ready"
    own_conn.close()


def test_default_board_lineage_fences_its_db_not_the_whole_root(tmp_path, monkeypatch):
    """The ``default`` board's DB sits directly under the root, so the marker must name that DB
    file — naming the root fenced every named board beside it (jarvis-os/t_a65a42ae)."""
    root = tmp_path / "kanban-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    default_db = root / "kanban.db"
    connect(default_db).close()
    named_db = kb.board_dir("jarvis-os") / "kanban.db"
    named_conn = connect(named_db)
    named_task = kb.create_task(named_conn, title="named board task")
    named_conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(default_db))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_default_fixture")
    default_conn = connect(default_db)
    default_task = kb.create_task(default_conn, title="default board task")
    default_conn.close()

    row = _run_descendant_probe(
        tmp_path,
        _CLI_HELPER
        + f"default_db, named_db = Path({str(default_db)!r}), Path({str(named_db)!r})\n"
        + "try:\n"
          "    conn = connect(default_db); kb.create_task(conn, title='default'); row['module_default'] = 'WROTE'\n"
          "except PermissionError:\n"
          "    row['module_default'] = 'fenced'\n"
          "conn = connect(named_db); row['module_named'] = kb.create_task(conn, title='named')\n"
        + f"row['cli_named'] = cli('--board', 'jarvis-os', 'comment', {named_task!r}, 'cross-board fix', unpin=True)\n"
        + f"row['cli_default'] = cli('comment', {default_task!r}, 'self', unpin=True)\n"
        + "row['cli_switch'] = cli('boards', 'switch', 'jarvis-os', unpin=True)\n",
    )

    assert row["marker"] == str(default_db), row
    assert row["marker"] != str(root), row
    assert row["module_default"] == "fenced", row
    assert row["module_named"], row
    assert row["cli_named"]["rc"] == 0, row["cli_named"]
    assert row["cli_default"]["rc"] != 0, row["cli_default"]
    assert row["cli_switch"]["rc"] != 0, row["cli_switch"]
    assert not (root / "kanban" / "current").exists()
    named_conn = connect(named_db)
    assert [c.body for c in kb.list_comments(named_conn, named_task)] == ["cross-board fix"]
    named_conn.close()
    default_conn = connect(default_db)
    assert not kb.list_comments(default_conn, default_task)
    default_conn.close()


def test_child_shell_can_write_a_kanban_board_outside_its_lineage_root(tmp_path, monkeypatch):
    """The fence a delegate_task child inherits applies to ITS lineage's board, not to every Kanban
    DB its shell touches: a repro run against a scratch HERMES_HOME got a silently read-only board
    (``connect`` opened ``?mode=ro``; ``write_txn`` raised PermissionError). Real ``terminal``
    ingress, real subprocess, real SQLite — the lineage board stays fenced in the same shell."""
    from agent.delegation_context import delegated_child_context

    lineage_home = tmp_path / "lineage"
    scratch_home = tmp_path / "scratch"
    for home in (lineage_home, scratch_home):
        home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(lineage_home))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(key, raising=False)
    connect(kb.kanban_db_path()).close()  # the owner initializes the lineage board
    script = tmp_path / "repro.py"
    script.write_text(
        "import os, sys, json\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from hermes_cli import kanban_db as kb\n"
        "from hermes_cli.kanban_db_connect import connect\n"
        "out = {'marker': os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')}\n"
        "try:\n"
        "    conn = connect(kb.kanban_db_path()); kb.create_task(conn, title='lineage'); out['lineage'] = 'WROTE'\n"
        "except PermissionError as exc:\n"
        "    out['lineage'] = 'fenced: ' + str(exc)\n"
        f"os.environ['HERMES_HOME'] = {str(scratch_home)!r}\n"
        "import hermes_constants; hermes_constants._default_hermes_root_memo = None\n"
        "conn = connect(kb.kanban_db_path()); out['scratch'] = kb.create_task(conn, title='scratch')\n"
        "print('SCOPE_RESULT=' + json.dumps(out))\n"
    )
    with delegated_child_context("child-repro"):
        terminal = LocalEnvironment(cwd=str(tmp_path))
        try:
            result = terminal.execute(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
        finally:
            terminal.cleanup()
    output = result.get("output", "")
    row = json.loads(next(line.split("SCOPE_RESULT=", 1)[1] for line in output.splitlines() if "SCOPE_RESULT=" in line))
    assert row["marker"] and row["marker"] != "1", row
    assert row["marker"] == str(lineage_home / "kanban.db"), row  # the fenced BOARD, not the Hermes root
    assert row["lineage"].startswith("fenced"), row
    assert row["scratch"], row
    scratch_conn = connect(scratch_home / "kanban.db")
    assert kb.get_task(scratch_conn, row["scratch"]).title == "scratch"
    scratch_conn.close()
