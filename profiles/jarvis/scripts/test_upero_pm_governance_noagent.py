#!/usr/bin/env python3
"""Regression tests for upero_pm_governance_noagent.py (kanban jarvis-os/t_f0a8777e).

Rework of rejected commit e7fa997 (checker jarvis-os/t_93c74f1b). The rejected
image kept a LOCAL parent-dependency predicate and swallowed every nonzero
kanban exit, so a genuinely broken mechanism printed "- PROMOTED ..." and exited
0 -> last_status=ok -> mechanism row GREEN. These tests pin the replacement
contract:

  * the Hermes engine is the SOLE dependency authority (no local predicate),
  * only the engine's exact "unsatisfied parent dependencies" refusal is a
    safe no-op; every other failure is visible and makes the process nonzero,
  * PROMOTED / NUDGED are never printed unless the command actually succeeded.

Test layers, deliberately separated so a failure localises:

  A. ENGINE TRUTH  - drives the REAL hermes_cli.kanban_db.promote_task against a
     disposable /tmp board. Pins the semantics the shim now defers to; fails
     loudly if the engine's gate ever changes.
  B. SCRIPT CONTRACT - drives main() against a disposable /tmp board through a
     shim that DELEGATES the decision to the real engine. The shim only
     reproduces the CLI's I/O contract, transcribed from hermes_cli/kanban.py
     (_cmd_promote 2416-2424: rc 1 + "cannot promote <id>: <err>" on stderr;
     _cmd_comment 2050-2063: rc 0 + "Comment added to <id>").
  C. COUNTERFACTUAL - loads the exact rejected blob (34b9f4c3641bb30c50d367aa
     35b085cec9c27eef @ e7fa997) and proves it still fabricates the false-GREEN
     on a fixture where the reworked image correctly goes nonzero.

Nothing here touches the live upero board, the deployed script, jobs.json, any
schedule/config/provider/credential, or any real Hermes runtime. Every fixture
is a throwaway directory under /tmp with HERMES_HOME repointed at it.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("upero_pm_governance_noagent.py")

# Exact rejected image reviewed and rejected on jarvis-os/t_93c74f1b.
REJECTED_BLOB = "34b9f4c3641bb30c50d367aa35b085cec9c27eef"
REJECTED_COMMIT = "e7fa99705e77a5ffebf892e98761b4ac95a044db"


def _load_module(path: Path, name: str):
    """Load a fresh module instance from a file path.

    Fresh per test so module-level constants (DB, HERMES, timeouts) can be
    repointed without leaking between cases.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engine():
    """Import the real kanban engine, or skip.

    HERMES_HOME is pinned to a temp dir first so importing the engine can never
    resolve or write to a live profile.
    """
    home = Path(tempfile.mkdtemp(prefix="upero-test-home-"))
    os.environ.setdefault("HERMES_HOME", str(home))
    for root in ("/home/frank/.hermes/hermes-agent",):
        if root not in sys.path and Path(root).is_dir():
            sys.path.insert(0, root)
    try:
        from hermes_cli import kanban_db as kb  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - environment dependent
        raise unittest.SkipTest(f"hermes_cli.kanban_db unavailable: {exc}")
    return kb


# Shim source. Reproduces the kanban CLI's observable contract while delegating
# every promote/comment DECISION to the real engine, so the behaviour under test
# is the engine's, not a hand-written imitation of it.
SHIM_SRC = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import sys, time, os
    LOG = {log!r}
    DB = {db!r}
    MODE = {mode!r}
    ENGINE_ROOT = {engine_root!r}

    with open(LOG, "a") as fh:
        fh.write("\\t".join(sys.argv[1:]) + "\\n")

    if MODE == "hang":
        time.sleep(30)
        sys.exit(0)

    if MODE == "fail":
        print("kanban: store error: disk I/O error", file=sys.stderr)
        sys.exit(1)

    os.environ.setdefault("HERMES_HOME", os.path.dirname(DB))
    sys.path.insert(0, ENGINE_ROOT)
    from hermes_cli import kanban_db as kb

    # Invoked as: <shim> kanban --board <slug> <subcommand> [...]
    argv = list(sys.argv[1:])
    if argv and argv[0] == "kanban":
        argv.pop(0)
    if "--board" in argv:
        i = argv.index("--board")
        del argv[i:i + 2]
    sub = argv[0] if argv else ""

    conn = kb.connect(__import__("pathlib").Path(DB))

    if sub == "promote":
        tid = argv[1]
        if MODE == "flip_parent":
            # TOCTOU: the parent flips out of a satisfied state between the
            # script's read-only snapshot and this promote.
            with conn:
                conn.execute(
                    "UPDATE tasks SET status='blocked' WHERE id IN "
                    "(SELECT parent_id FROM task_links WHERE child_id=?)", (tid,)
                )
        ok, err = kb.promote_task(conn, tid, actor="test", reason=None)
        if ok:
            print(f"Promoted {{tid}} -> ready")
            sys.exit(0)
        print(f"cannot promote {{tid}}: {{err}}", file=sys.stderr)
        sys.exit(1)

    if sub == "comment":
        rest = argv[1:]
        if "--author" in rest:
            i = rest.index("--author")
            author = rest[i + 1]
            del rest[i:i + 2]
        else:
            author = "test"
        tid, body = rest[0], " ".join(rest[1:])
        if MODE == "comment_target_gone":
            # The task is purged between the script's snapshot and this write,
            # so the real engine raises "unknown task".
            with conn:
                conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
        try:
            kb.add_comment(conn, tid, author, body)
        except Exception as exc:
            print(f"kanban: {{exc}}", file=sys.stderr)
            sys.exit(1)
        print(f"Comment added to {{tid}}")
        sys.exit(0)

    print(f"kanban: unsupported subcommand {{sub}}", file=sys.stderr)
    sys.exit(2)
    '''
)


class _Fixture:
    """Disposable board + shim. Never touches anything outside its tmpdir."""

    def __init__(self, kb, mode: str = "engine"):
        self.kb = kb
        self.dir = Path(tempfile.mkdtemp(prefix="upero-pm-gov-"))
        self.db = self.dir / "kanban.db"
        self.log = self.dir / "cli-invocations.log"
        self.log.touch()
        self.conn = kb.connect(self.db)
        self.shim = self.dir / "hermes"
        self.shim.write_text(
            SHIM_SRC.format(
                log=str(self.log),
                db=str(self.db),
                mode=mode,
                engine_root="/home/frank/.hermes/hermes-agent",
            )
        )
        self.shim.chmod(0o755)

    # -- board construction -------------------------------------------------
    def task(self, title: str, status: str, priority: int = 0,
             assignee: str = "upero-pm", heartbeat: int | None = None) -> str:
        # create_task only accepts 'blocked'/'running' as an initial status, so
        # the target status is set directly afterwards.
        created = self.kb.create_task(
            self.conn, title=title, assignee=assignee, priority=priority,
            created_by="test", initial_status="blocked",
        )
        tid = created if isinstance(created, str) else created["id"]
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET status=?, last_heartbeat_at=? WHERE id=?",
                (status, heartbeat, tid),
            )
        return tid

    def link(self, parent: str, child: str) -> None:
        self.kb.link_tasks(self.conn, parent, child)

    def drop_task_row(self, tid: str) -> None:
        """Delete a task row, leaving its task_links edge dangling."""
        with self.conn:
            self.conn.execute("DELETE FROM tasks WHERE id=?", (tid,))

    def status_of(self, tid: str) -> str:
        return self.conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()["status"]

    def comment_count(self, tid: str) -> int:
        return self.conn.execute(
            "SELECT count(*) n FROM task_comments WHERE task_id=?", (tid,)
        ).fetchone()["n"]

    def invocations(self) -> list[str]:
        txt = self.log.read_text().strip()
        return [ln for ln in txt.splitlines() if ln.strip()]

    # -- running the script under test --------------------------------------
    def run(self, module_path: Path = SCRIPT, name: str = "under_test",
            timeout: int | None = None) -> tuple[int, str]:
        mod = _load_module(module_path, f"{name}_{int(time.time()*1e6)}")
        mod.DB = self.db
        mod.HERMES = str(self.shim)
        if timeout is not None and hasattr(mod, "KANBAN_TIMEOUT_SECS"):
            mod.KANBAN_TIMEOUT_SECS = timeout
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
        return rc, buf.getvalue()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class EngineTruthTests(unittest.TestCase):
    """Layer A: what the REAL engine actually permits.

    These are the facts the reworked shim delegates to. If any of them changes,
    the shim's behaviour changes with it - which is the whole point of deleting
    the local predicate.
    """

    @classmethod
    def setUpClass(cls):
        cls.kb = _engine()

    def setUp(self):
        self.fx = _Fixture(self.kb)
        self.addCleanup(self.fx.close)

    def test_done_parent_is_promotable(self):
        parent = self.fx.task("parent", "done")
        child = self.fx.task("child", "todo")
        self.fx.link(parent, child)
        ok, err = self.kb.promote_task(self.fx.conn, child, actor="t")
        self.assertTrue(ok, err)

    def test_archived_parent_is_promotable(self):
        # The rejected image's SATISFIED_PARENT_STATES={"done"} refused this;
        # the engine allows it (kanban_db.promote_task -> ("done","archived")).
        parent = self.fx.task("parent", "archived")
        child = self.fx.task("child", "todo")
        self.fx.link(parent, child)
        ok, err = self.kb.promote_task(self.fx.conn, child, actor="t")
        self.assertTrue(ok, f"engine must allow archived parent, got {err!r}")

    def test_dangling_parent_link_is_promotable(self):
        # promote_task INNER JOINs tasks->task_links, so a link whose parent row
        # is gone yields no unsatisfied parent. The rejected local predicate
        # returned False here (parent row is None) and skipped forever.
        parent = self.fx.task("parent", "done")
        child = self.fx.task("child", "todo")
        self.fx.link(parent, child)
        self.fx.drop_task_row(parent)
        ok, err = self.kb.promote_task(self.fx.conn, child, actor="t")
        self.assertTrue(ok, f"engine must allow dangling parent link, got {err!r}")

    def test_genuinely_unsatisfied_parent_refused_with_exact_marker(self):
        parent = self.fx.task("parent", "blocked")
        child = self.fx.task("child", "todo")
        self.fx.link(parent, child)
        ok, err = self.kb.promote_task(self.fx.conn, child, actor="t")
        self.assertFalse(ok)
        # The marker the shim keys on must really be the engine's wording.
        mod = _load_module(SCRIPT, "marker_probe")
        self.assertIn(mod.UNSATISFIED_PARENT_MARKER, err or "")


class ScriptContractTests(unittest.TestCase):
    """Layer B: end-to-end main() over a real board, decisions made by the engine."""

    @classmethod
    def setUpClass(cls):
        cls.kb = _engine()

    def _fx(self, mode="engine"):
        fx = _Fixture(self.kb, mode=mode)
        self.addCleanup(fx.close)
        return fx

    # -- promotion follows the engine ---------------------------------------
    def test_archived_parent_todo_is_promoted(self):
        fx = self._fx()
        parent = fx.task("parent", "archived")
        child = fx.task("child", "todo", priority=90)
        fx.link(parent, child)

        rc, out = fx.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("PROMOTED", out)
        self.assertEqual(fx.status_of(child), "ready")

    def test_dangling_parent_todo_is_promoted(self):
        fx = self._fx()
        parent = fx.task("parent", "done")
        child = fx.task("child", "todo", priority=90)
        fx.link(parent, child)
        fx.drop_task_row(parent)

        rc, out = fx.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("PROMOTED", out)
        self.assertEqual(fx.status_of(child), "ready")

    # -- the one tolerated refusal ------------------------------------------
    def test_unsatisfied_parent_is_zero_exit_no_mutation_no_claim(self):
        fx = self._fx()
        parent = fx.task("parent", "blocked")
        child = fx.task("child", "todo", priority=100)
        fx.link(parent, child)

        rc, out = fx.run()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("PROMOTED", out)
        self.assertNotIn("UPERO_PM_GOVERNANCE_DEGRADED", out)
        self.assertIn("UPERO_PM_GOVERNANCE_SKIPS", out)
        self.assertIn("unsatisfied parent dependencies", out)
        # No mutation, and the promote was attempted exactly once (no --force).
        self.assertEqual(fx.status_of(child), "todo")
        inv = fx.invocations()
        self.assertEqual(len(inv), 1, inv)
        self.assertNotIn("--force", inv[0])

    def test_parent_status_flip_between_snapshot_and_promote_is_safe(self):
        # TOCTOU: snapshot sees a done parent; the parent flips to blocked
        # before the promote lands. The engine refuses, and that refusal must be
        # classified as the safe no-op - not reported as success.
        fx = self._fx(mode="flip_parent")
        parent = fx.task("parent", "done")
        child = fx.task("child", "todo", priority=100)
        fx.link(parent, child)

        rc, out = fx.run()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("PROMOTED", out)
        self.assertIn("UPERO_PM_GOVERNANCE_SKIPS", out)
        self.assertEqual(fx.status_of(child), "todo")

    # -- genuine failures must be visible -----------------------------------
    def test_genuine_promote_failure_is_nonzero_without_fabricated_success(self):
        fx = self._fx(mode="fail")
        fx.task("child", "todo", priority=90)

        rc, out = fx.run()
        self.assertEqual(rc, 1, out)
        self.assertNotIn("PROMOTED", out)
        self.assertIn("UPERO_PM_GOVERNANCE_DEGRADED", out)
        self.assertIn("PROMOTE_FAILED", out)

    def test_genuine_comment_failure_is_nonzero_without_fabricated_success(self):
        fx = self._fx(mode="fail")
        fx.task("stale", "running", heartbeat=int(time.time()) - 6 * 3600)

        rc, out = fx.run()
        self.assertEqual(rc, 1, out)
        self.assertNotIn("NUDGED", out)
        self.assertIn("COMMENT_FAILED", out)

    def test_comment_failure_on_unknown_task_is_nonzero(self):
        # Real engine failure path: the task is purged between the snapshot and
        # the write, so add_comment raises "unknown task" -> rc 1.
        fx = self._fx(mode="comment_target_gone")
        fx.task("stale", "running", heartbeat=int(time.time()) - 6 * 3600)

        rc, out = fx.run()
        self.assertEqual(rc, 1, out)
        self.assertNotIn("NUDGED", out)
        self.assertIn("COMMENT_FAILED", out)

    def test_successful_nudge_is_claimed_and_written(self):
        fx = self._fx()
        stale = fx.task("stale", "running", heartbeat=int(time.time()) - 6 * 3600)

        rc, out = fx.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("NUDGED", out)
        self.assertEqual(fx.comment_count(stale), 1)

    def test_timeout_is_nonzero(self):
        fx = self._fx(mode="hang")
        fx.task("child", "todo", priority=90)

        rc, out = fx.run(timeout=1)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("PROMOTED", out)
        self.assertIn("timeout", out.lower())

    def test_missing_binary_is_nonzero(self):
        fx = self._fx()
        fx.task("child", "todo", priority=90)
        mod = _load_module(SCRIPT, "missing_bin")
        mod.DB = fx.db
        mod.HERMES = str(fx.dir / "definitely-not-here" / "hermes")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
        out = buf.getvalue()
        self.assertEqual(rc, 1, out)
        self.assertNotIn("PROMOTED", out)
        self.assertIn("not found", out)

    def test_missing_board_db_is_nonzero(self):
        fx = self._fx()
        mod = _load_module(SCRIPT, "missing_db")
        mod.DB = fx.dir / "no-such-board.db"
        mod.HERMES = str(fx.shim)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
        self.assertEqual(rc, 1)
        self.assertIn("missing board DB", buf.getvalue())
        self.assertEqual(fx.invocations(), [])

    def test_unreadable_board_schema_is_nonzero_not_silent_ok(self):
        # B3 regression: a schema/store error must be visible, and must not
        # escape as an uncaught traceback either.
        fx = self._fx()
        with fx.conn:
            fx.conn.execute("DROP TABLE tasks")
        rc, out = fx.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("board DB read failed", out)
        self.assertEqual(fx.invocations(), [])

    def test_no_local_parent_predicate_remains(self):
        # The removed predicate is the drift/TOCTOU source; keep it deleted.
        mod = _load_module(SCRIPT, "surface_probe")
        self.assertFalse(hasattr(mod, "parents_satisfied"))
        self.assertFalse(hasattr(mod, "SATISFIED_PARENT_STATES"))
        self.assertNotIn("task_links", SCRIPT.read_text())

    def test_quiet_healthy_tick_emits_nothing(self):
        fx = self._fx()
        fx.task("ready-work", "ready")
        rc, out = fx.run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")
        self.assertEqual(fx.invocations(), [])


class RejectedImageCounterfactualTests(unittest.TestCase):
    """Layer C: the exact rejected blob still fails where the rework passes."""

    @classmethod
    def setUpClass(cls):
        cls.kb = _engine()
        cls.repo = Path(__file__).resolve()
        try:
            blob = subprocess.run(
                ["git", "cat-file", "blob", REJECTED_BLOB],
                cwd=str(cls.repo.parent), text=True, capture_output=True,
                timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            raise unittest.SkipTest(f"git unavailable: {exc}")
        if blob.returncode != 0 or not blob.stdout.strip():
            raise unittest.SkipTest(
                f"rejected blob {REJECTED_BLOB} not available: {blob.stderr.strip()}"
            )
        cls.tmp = Path(tempfile.mkdtemp(prefix="upero-rejected-"))
        cls.rejected = cls.tmp / "rejected_image.py"
        cls.rejected.write_text(blob.stdout)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "tmp"):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fx(self, mode):
        fx = _Fixture(self.kb, mode=mode)
        self.addCleanup(fx.close)
        return fx

    def test_rejected_image_fabricates_promoted_on_hard_failure(self):
        """The false-GREEN being fixed: promote fails, output claims success."""
        fx = self._fx("fail")
        fx.task("child", "todo", priority=90)

        rc, out = fx.run(self.rejected, name="rejected")
        self.assertEqual(rc, 0, "rejected image exited 0 on a hard promote failure")
        self.assertIn("PROMOTED", out, "rejected image fabricated a PROMOTED claim")

        # Same fixture, reworked image: nonzero and no fabricated claim.
        fx2 = self._fx("fail")
        fx2.task("child", "todo", priority=90)
        rc2, out2 = fx2.run()
        self.assertEqual(rc2, 1)
        self.assertNotIn("PROMOTED", out2)

    def test_rejected_image_skips_a_todo_the_engine_would_promote(self):
        """Inert-but-GREEN class: archived parent refused locally, allowed by engine."""
        fx = self._fx("engine")
        parent = fx.task("parent", "archived")
        child = fx.task("child", "todo", priority=90)
        fx.link(parent, child)

        rc, out = fx.run(self.rejected, name="rejected")
        self.assertEqual(rc, 0)
        self.assertNotIn("PROMOTED", out)
        self.assertEqual(fx.status_of(child), "todo", "rejected image skipped it")
        self.assertEqual(fx.invocations(), [], "rejected image made no CLI call")

        # Reworked image promotes the same board, following the engine.
        fx2 = self._fx("engine")
        p2 = fx2.task("parent", "archived")
        c2 = fx2.task("child", "todo", priority=90)
        fx2.link(p2, c2)
        rc2, out2 = fx2.run()
        self.assertEqual(rc2, 0, out2)
        self.assertIn("PROMOTED", out2)
        self.assertEqual(fx2.status_of(c2), "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
