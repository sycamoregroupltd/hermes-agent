"""SQLite-backed Kanban board for multi-profile, multi-project collaboration.

In a fresh install the board lives at ``<root>/kanban.db`` where
``<root>`` is the **shared Hermes root** (the parent of any active
profile). Profiles intentionally collapse onto a shared board: it IS
the cross-profile coordination primitive. A worker spawned with
``hermes -p <profile>`` joins the same board as the dispatcher that
claimed the task. The same applies to ``<root>/kanban/workspaces/`` and
``<root>/kanban/logs/``.

**Multiple boards (projects):** users can create additional boards to
separate unrelated streams of work (e.g. one per project / repo / domain).
Each board is a directory under ``<root>/kanban/boards/<slug>/`` with
its own ``kanban.db``, ``workspaces/``, and ``logs/``. All boards share
the profile's Hermes home but are otherwise isolated: a worker spawned
for a task on board ``atm10-server`` sees only that board's tasks,
cannot enumerate other boards, and its dispatcher ticks don't touch
other boards' DBs.

The first (and for single-project users, only) board is ``default``.
For back-compat its on-disk DB is ``<root>/kanban.db`` (not
``boards/default/kanban.db``), so installs that predate the boards
feature keep working with zero migration. See :func:`kanban_db_path`.

Board resolution order (highest precedence first, all optional):

* ``board=`` argument passed directly to :func:`connect` / :func:`init_db`
  (explicit — used by the CLI ``--board`` flag and the dashboard
  ``?board=...`` query param).
* ``HERMES_KANBAN_BOARD`` env var (used by the dispatcher to pin workers
  to the board their task lives on — workers cannot see other boards).
* ``HERMES_KANBAN_DB`` env var (pins the DB file path directly — legacy
  override still honoured; highest precedence when the file path itself
  is what the caller wants to force).
* ``<root>/kanban/current`` — a one-line text file holding the slug of
  the "currently selected" board. Written by ``hermes kanban boards
  switch <slug>``. When absent, the active board is ``default``.

In standard installs ``<root>`` is ``~/.hermes``. In Docker / custom
deployments where ``HERMES_HOME`` points outside ``~/.hermes`` (e.g.
``/opt/hermes``), ``<root>`` is ``HERMES_HOME``. Legacy env-var
overrides still work:

* ``HERMES_KANBAN_DB`` — pin the database file path directly.
* ``HERMES_KANBAN_WORKSPACES_ROOT`` — pin the workspaces root directly.
* ``HERMES_KANBAN_HOME`` — pin the umbrella root that anchors kanban
  paths. Useful for tests and unusual deployments.

The dispatcher injects ``HERMES_KANBAN_DB``,
``HERMES_KANBAN_WORKSPACES_ROOT``, and ``HERMES_KANBAN_BOARD`` into
worker subprocess env so workers converge on the exact DB the
dispatcher used to claim their task — even under unusual symlink or
Docker layouts.

Schema is intentionally small: tasks, task_links, task_comments,
task_events.  The ``workspace_kind`` field decouples coordination from git
worktrees so that research / ops / digital-twin workloads work alongside
coding workloads.  See ``docs/hermes-kanban-v1-spec.pdf`` for the full
design specification.

Concurrency strategy: WAL mode + ``BEGIN IMMEDIATE`` for write
transactions + compare-and-swap (CAS) updates on ``tasks.status`` and
``tasks.claim_lock``.  SQLite serializes writers via its WAL lock, so at
most one claimer can win any given task.  Losers observe zero affected
rows and move on -- no retry loops, no distributed-lock machinery.
The CAS coordination is **per-board** — each board is a separate DB,
so multi-board installs get the same atomicity guarantees without any
new locking.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import random
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing
from toolsets import get_toolset_names

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
VALID_INITIAL_STATUSES = {"running", "blocked"}

# Typed block reasons. Distinguishes the two fundamentally different things a
# worker (or human) means by "blocked", so each can be routed differently
# instead of all landing in one undifferentiated ``blocked`` bucket that a cron
# unblocks → worker re-blocks → cron unblocks … forever.
#
#   * ``dependency``   — can't proceed until another task finishes. Routed to
#                        ``todo`` (NOT ``blocked``) so the existing
#                        parent-gating / ``recompute_ready`` machinery promotes
#                        it automatically once parents are done. No human, no
#                        cron, no retry storm.
#   * ``needs_input``  — needs a human decision/answer it cannot derive.
#   * ``capability``   — hit a hard wall (no access, missing creds, an action no
#                        AI agent can perform). Genuinely human-only.
#   * ``transient``    — a flaky/temporary failure that may clear on retry.
#
# ``needs_input`` and ``capability`` are "truly blocked": they go to ``blocked``
# for a human, and the unblock-loop breaker (see ``block_task`` /
# ``BLOCK_RECURRENCE_LIMIT``) escalates them to ``triage`` if a cron keeps
# unblocking them only to have the worker re-block for the same reason.
# ``None`` = legacy/un-typed block (treated as a generic human blocker).
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}

# After a task has been blocked, unblocked, and re-blocked this many times for
# the same (truly-blocked) reason, the unblock-loop breaker stops trusting the
# unblocker (usually a cron) and routes the task to ``triage`` instead of back
# to ``blocked`` — breaking the infinite unblock↔re-block loop and forcing a
# human-in-the-loop decision. Mirrors the dispatcher's ``DEFAULT_FAILURE_LIMIT``
# spirit (default 2) but counts a different signal: manual unblock recurrences,
# not dispatcher spawn/crash/timeout failures.
BLOCK_RECURRENCE_LIMIT = 2
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024


def _assert_not_delegated_child_mutation() -> None:
    """Reject Kanban state mutations from ``delegate_task`` child contexts.

    The structured kanban tools and CLI dispatch layer both have fast-fail
    guards for better UX, but neither is a trust boundary: a delegated child can
    still shell out to the CLI or import this module directly. The actual
    invariant belongs at the DB/filesystem mutation layer so every public
    mutator that uses ``write_txn`` (tasks, runs, comments, attachments,
    dispatcher claims, repair events, subscriptions, GC, etc.) and every board
    metadata mutator fails closed before touching durable state.
    """
    try:
        from agent.delegation_context import is_delegated_child_process_context

        delegated = is_delegated_child_process_context()
    except Exception:
        delegated = bool(os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"))
    if delegated:
        raise PermissionError(
            "delegate_task child contexts cannot mutate Kanban tasks or boards"
        )

# Review-lane diagnostics deliberately recognise only reviewer-ish lanes.  A
# blocked implementation/landing/remediation parent is a real dependency gate;
# the deadlock pattern we want to surface is a reviewer child parented to the
# blocked source it is supposed to review.
REVIEW_LANE_ASSIGNEE_MARKERS = (
    "reviewer",
    "guardian",
    "review",
)
REVIEW_LANE_TEXT_MARKERS = (
    "review",
    "review_verdict",
    "approve_with_notes",
    "changes_requested",
    "guardian",
)

ACTIVE_SERVICE_GATE_STATUSES = {
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
}
SERVICE_GATE_APPROVAL_STATUSES = ACTIVE_SERVICE_GATE_STATUSES | {"done"}
TRUE_CRITICAL_LIST_MARKERS = (
    "credential",
    "credentials",
    "secret",
    "secrets",
    "money",
    "payment",
    "payments",
    "live trading",
    "production deploy",
    "prod deploy",
    "irreversible data",
    "drop table",
    "mass delete",
    "new spend",
    "gateway restart",
    "runtime activation",
    "workforce-scaler",
    "workforce scaler",
    "dynamic-spawning activation",
    "dynamic spawning activation",
    "guardrail weakening",
    "guardrail disable",
    "disable guardrail",
    "auth/tenant",
    "tenant live-data",
)


def _fire_kanban_lifecycle_hook(event: str, task_id: str, **fields: Any) -> None:
    """Fire a kanban lifecycle plugin hook, fully best-effort.

    Called by the claim/complete/block transitions AFTER their write txn has
    committed, so plugin code never runs while a SQLite write lock is held and
    always observes durable board state. Any failure (plugins unavailable,
    a plugin raising, import error) is swallowed — a misbehaving observer must
    never break a board state transition.

    ``profile_name`` is resolved from the active HERMES_HOME so dispatcher- and
    worker-side hooks both carry the right profile without the caller plumbing
    it through.
    """
    try:
        from hermes_cli.lifecycle import invoke_hook
        from hermes_cli.profiles import get_active_profile_name
        try:
            profile_name = get_active_profile_name()
        except Exception:
            profile_name = "default"
        invoke_hook(event, task_id=task_id, profile_name=profile_name, **fields)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban lifecycle hook %s failed: %s", event, exc)


# A running task's claim is valid for 15 minutes by default; after that the
# next dispatcher tick reclaims it. Workers that outlive this window should
# call ``heartbeat_claim(task_id)`` periodically. In practice most kanban
# workloads either finish within 15m, set a longer claim explicitly, or use
# ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` to raise the default claim window for
# long single-call MCP workflows.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60

# If a worker's PID is still alive but its ``last_heartbeat_at`` is
# older than this when ``release_stale_claims`` runs, treat the worker
# as wedged and reclaim regardless of PID liveness (#29747 gap 3).
# This catches the logic-loop case where the process is technically
# running but not making observable progress.  ``_touch_activity``
# bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
# so any genuinely active worker keeps its heartbeat fresh as a side
# effect of normal API traffic.
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60

# Grace added to a claim when a reclaim is deferred because the previous
# host-local worker is still alive after a termination attempt. Releasing the
# claim in that state would spawn a duplicate alongside the surviving worker —
# the runaway seen when a cgroup memory.high throttle parks a worker in
# uninterruptible (D) state, where a pending SIGKILL cannot be delivered until
# the throttle lifts. Holding the claim a short grace and retrying next tick
# stops the duplication; once no duplicate is spawned the pressure eases, the
# signal lands, and the following tick reclaims cleanly.
RECLAIM_DEFER_GRACE_SECONDS = 120


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Return the effective claim TTL, honoring the kanban env override.

    Explicit call-site values win. Otherwise a positive integer from
    ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` overrides the built-in default.
    Invalid or non-positive env values fall back silently so existing
    installs keep working.
    """
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    raw = os.environ.get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed

    return DEFAULT_CLAIM_TTL_SECONDS


# Grace period after a task transitions to ``running`` during which
# ``detect_crashed_workers`` skips the ``_pid_alive`` check. Covers the
# fork() → /proc-visibility window where liveness can transiently report
# False for a freshly-spawned worker. The 15-minute claim TTL still
# catches genuinely-crashed workers; this only suppresses false positives
# during the launch window.
DEFAULT_CRASH_GRACE_SECONDS = 30


# Sentinel exit code a kanban worker uses to signal "I bailed because the
# provider rate-limited / exhausted quota, not because the task failed."
# The dispatcher's reap classifier maps this to a ``rate_limited`` exit kind
# so ``detect_crashed_workers`` can release the task back to ``ready``
# WITHOUT counting a failure (the circuit breaker must never trip on a
# transient throttle). 75 == BSD ``EX_TEMPFAIL`` (sysexits.h) — the
# conventional "temporary failure, retry later" code, and well clear of the
# 0/1/2 codes the worker uses for success / generic failure / usage error.
KANBAN_RATE_LIMIT_EXIT_CODE = 75


def _resolve_crash_grace_seconds() -> int:
    """Return the crash-detection grace period in seconds.

    Reads ``HERMES_KANBAN_CRASH_GRACE_SECONDS`` from the environment;
    falls back to ``DEFAULT_CRASH_GRACE_SECONDS`` when absent, empty,
    non-integer, or negative. A value of 0 restores immediate-reclaim
    behaviour (useful for tests).
    """
    raw = os.environ.get("HERMES_KANBAN_CRASH_GRACE_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_CRASH_GRACE_SECONDS


def _resolve_rate_limit_cooldown_seconds() -> int:
    """Return the rate-limit requeue cooldown in seconds.

    Reads ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` from the environment;
    falls back to ``DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS`` when absent, empty,
    non-integer, or negative. A value of 0 disables the cooldown (re-spawn on
    the next tick) — useful for tests that want to assert the task becomes
    spawnable again immediately.
    """
    raw = os.environ.get(
        "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", ""
    ).strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS


# Worker-context caps so build_worker_context() stays bounded on
# pathological boards (retry-heavy tasks, comment storms, giant
# summaries). Values chosen to fit a typical 100k-char LLM prompt with
# plenty of headroom. Each constant is tuned independently so users
# who need to relax one don't have to relax all of them.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # 4 KB per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # 8 KB per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # 2 KB per comment


def _relative_age(ts: Optional[int], now: Optional[int] = None) -> str:
    """Render the age of an epoch-seconds timestamp as a coarse, human-
    readable string like ``just now``, ``18h ago``, ``3d ago``.

    Workers read parent handoffs, comments, and prior-attempt summaries as
    if they describe *current* state. A bare absolute timestamp
    (``2026-06-25 14:30``) doesn't make an LLM reason about staleness — it
    reads the content as fact regardless of how old it is. A relative age
    ("18h ago") is the signal that prompts the worker to re-verify against
    the live source before acting on stale sibling work. Returns an empty
    string for missing/invalid timestamps so callers can append
    unconditionally.
    """
    if ts is None:
        return ""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if now is None:
        now = int(time.time())
    delta = now - ts
    if delta < 0:
        # Clock skew across machines/profiles — don't claim "in the future".
        return "just now"
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DEFAULT_BOARD = "default"
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override",
    default=None,
)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Temporarily pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)

# Slug validator: lowercase alphanumerics, digits, hyphens; 1–64 chars.
# Strict enough to stop traversal (`..`) and embedded path separators, loose
# enough that kebab-case names like ``atm10-server`` or ``hermes-agent``
# pass without fuss. Board names with display formatting (spaces, emoji)
# live in ``board.json``; the slug is just the directory name.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    if slug is None:
        return None
    s = str(slug).strip().lower()
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def kanban_home() -> Path:
    """Return the shared Hermes root that anchors the kanban board.

    Resolution order:

    1. ``HERMES_KANBAN_HOME`` env var when set and non-empty (explicit
       override for tests and unusual deployments).
    2. ``get_default_hermes_root()``, which already returns ``<root>``
       when ``HERMES_HOME`` is ``<root>/profiles/<name>``, and returns
       ``HERMES_HOME`` directly for Docker / custom deployments.

    The kanban board is shared across profiles **by design** (see the
    module docstring). Resolving the kanban paths through the active
    profile's ``HERMES_HOME`` would silently fork the board per profile,
    which breaks the dispatcher / worker handoff.
    """
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """Return ``<root>/kanban/boards`` — the parent of non-default board dirs.

    ``default`` is intentionally NOT under this directory — its DB lives at
    ``<root>/kanban.db`` for back-compat with pre-boards installs. This
    function returns the directory where *additional* named boards live,
    used by :func:`list_boards` to enumerate them.
    """
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """Return the path to ``<root>/kanban/current``.

    One-line text file written by ``hermes kanban boards switch <slug>``
    to persist the user's board selection across CLI invocations. Absent
    by default (meaning: active board is ``default``).
    """
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Return the active board slug, honouring the resolution chain.

    Order (highest precedence first):

    1. ``HERMES_KANBAN_BOARD`` env var (set by the dispatcher on worker
       spawn, or manually for ad-hoc overrides).
    2. ``<root>/kanban/current`` on disk (set by ``hermes kanban boards
       switch``), but only when that board still exists.
    3. ``DEFAULT_BOARD`` (``"default"``).

    A malformed or stale slug at any step falls through to the next layer
    with a best-effort warning — the dispatcher must never crash because a
    user hand-edited a file or removed a board directory.
    """
    scoped = (_CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if scoped:
        try:
            normed = _normalize_board_slug(scoped)
            if normed and board_exists(normed):
                return normed
        except ValueError:
            pass

    env = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if env:
        try:
            normed = _normalize_board_slug(env)
            if normed and board_exists(normed):
                return normed
        except ValueError:
            pass
    try:
        f = current_board_path()
        if f.exists():
            val = f.read_text(encoding="utf-8").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board. Returns the file written.

    Writes ``<root>/kanban/current``. The caller should validate the slug
    exists first (via :func:`board_exists`) — this function does not —
    so that ``hermes kanban boards switch <typo>`` returns an error
    instead of silently pointing at nothing.
    """
    _assert_not_delegated_child_mutation()
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    _assert_not_delegated_child_mutation()
    try:
        current_board_path().unlink()
    except FileNotFoundError:
        pass


def board_dir(board: Optional[str] = None) -> Path:
    """Return the on-disk directory for ``board``.

    ``default`` is ``<root>/kanban/boards/default/`` **for metadata only**
    (board.json + workspaces/ + logs/). Its DB file stays at
    ``<root>/kanban.db`` for back-compat — see :func:`kanban_db_path`.

    All other boards live at ``<root>/kanban/boards/<slug>/`` with
    everything inside that directory including the ``kanban.db``.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return boards_root() / slug


def board_exists(board: Optional[str] = None) -> bool:
    """Return True if the board has persisted metadata or a DB on disk.

    ``default`` is considered to always exist — its DB is created
    on first :func:`connect` and there's no way for it to be missing
    in a configuration where the kanban feature is usable at all.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    if slug == DEFAULT_BOARD:
        return True
    d = board_dir(slug)
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def kanban_db_path(board: Optional[str] = None) -> Path:
    """Return the path to the ``kanban.db`` for ``board``.

    Resolution (highest precedence first):

    1. ``HERMES_KANBAN_DB`` env var — pins the path directly. Honoured for
       back-compat and for the dispatcher→worker handoff (defense in
       depth: dispatcher injects this into worker env so workers are
       immune to any path-resolution disagreement).
    2. When ``board`` arg is None, the active board from
       :func:`get_current_board` is used.
    3. Board ``default`` → ``<root>/kanban.db`` (back-compat path).
       Other boards → ``<root>/kanban/boards/<slug>/kanban.db``.
    """
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban.db"
    return board_dir(slug) / "kanban.db"


def workspaces_root(board: Optional[str] = None) -> Path:
    """Return the directory under which ``scratch`` workspaces are created.

    Anchored per-board so workspaces don't leak between projects.
    ``HERMES_KANBAN_WORKSPACES_ROOT`` pins the path directly (highest
    precedence) — the dispatcher injects this into worker env.

    ``default`` keeps the legacy path ``<root>/kanban/workspaces/`` so
    that existing scratch workspaces from before the boards feature are
    preserved. Other boards use ``<root>/kanban/boards/<slug>/workspaces/``.
    """
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "workspaces"
    return board_dir(slug) / "workspaces"


def attachments_root(board: Optional[str] = None) -> Path:
    """Return the directory under which task file attachments are stored.

    Mirrors :func:`worker_logs_dir` / :func:`workspaces_root`: anchored
    per-board so attachments don't leak between projects. Each task gets
    its own ``<root>/.../attachments/<task_id>/`` subdirectory.

    ``HERMES_KANBAN_ATTACHMENTS_ROOT`` pins the path directly (highest
    precedence) for tests and unusual deployments.

    ``default`` uses ``<root>/kanban/attachments/``; other boards use
    ``<root>/kanban/boards/<slug>/attachments/``.

    Workers (which run with full file-tool access) read attached files
    by the absolute path surfaced in :func:`build_worker_context`. On the
    local terminal backend — the default for kanban — that path resolves
    directly. Remote backends (Docker/Modal) need this directory mounted;
    see the kanban docs.
    """
    override = os.environ.get("HERMES_KANBAN_ATTACHMENTS_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "attachments"
    return board_dir(slug) / "attachments"


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / task_id


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Return the directory under which per-task worker logs are written.

    ``default`` keeps the legacy path ``<root>/kanban/logs/``. Other
    boards use ``<root>/kanban/boards/<slug>/logs/``. Logs follow the
    board — makes ``hermes kanban log`` unambiguous even when multiple
    boards have tasks with the same id.
    """
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "logs"
    return board_dir(slug) / "logs"


def board_metadata_path(board: Optional[str] = None) -> Path:
    """Return the path to ``board.json`` for ``board``.

    Stores display metadata (display name, description, icon, color,
    created_at). The on-disk slug is the canonical identity; this file
    is purely for presentation in the CLI / dashboard.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return board_dir(slug) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """Turn a slug into a reasonable default display name.

    ``atm10-server`` → ``Atm10 Server``. Users can override via
    ``board.json`` but the default should look presentable in the
    dashboard without any follow-up editing.
    """
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """Return ``board.json`` contents (or synthesized defaults).

    Never raises — a missing / malformed ``board.json`` falls back to a
    synthesised entry so the dashboard always has something to render.
    Includes the canonical ``slug`` and ``db_path`` so the caller
    doesn't need to reconstruct them.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    archived: Optional[bool] = None,
    default_workdir: Optional[str] = None,
) -> dict:
    """Create / update ``board.json`` for ``board``.

    Preserves any existing fields not mentioned in the call. Sets
    ``created_at`` on first write. Returns the resulting metadata dict.
    """
    _assert_not_delegated_child_mutation()
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta = read_board_metadata(slug)
    # Preserve existing DB-derived fields — they get re-computed each
    # read but shouldn't be written into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    if description is not None:
        meta["description"] = str(description)
    if icon is not None:
        meta["icon"] = str(icon)
    if color is not None:
        meta["color"] = str(color)
    if archived is not None:
        meta["archived"] = bool(archived)
    if default_workdir is not None:
        meta["default_workdir"] = str(default_workdir) if default_workdir else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def create_board(
    slug: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    default_workdir: Optional[str] = None,
) -> dict:
    """Create a new board directory + DB + metadata. Idempotent.

    Returns the resulting metadata. Raises :class:`ValueError` for a
    malformed slug; returns the existing metadata (not an error) if the
    board already exists — matching ``mkdir -p`` semantics.
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    meta = write_board_metadata(
        normed,
        name=name,
        description=description,
        icon=icon,
        color=color,
        default_workdir=default_workdir,
    )
    # Touch the DB so list_boards() sees it immediately.
    init_db(board=normed)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Enumerate all boards that exist on disk.

    Always includes ``default`` (even when the ``boards/default/``
    metadata dir doesn't exist, because its DB is at the legacy path).
    Other boards are discovered by scanning ``boards/`` for subdirectories
    that either contain a ``kanban.db`` or a ``board.json``.

    Returns a list of metadata dicts, sorted with ``default`` first and
    the rest alphabetically.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    # Default board is always first.
    entries.append(read_board_metadata(DEFAULT_BOARD))
    seen.add(DEFAULT_BOARD)

    root = boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            slug = child.name
            # Keep slug normalisation soft for discovery — but skip dirs
            # that don't parse as valid slugs so we don't surface junk.
            try:
                normed = _normalize_board_slug(slug)
            except ValueError:
                continue
            if not normed or normed in seen:
                continue
            has_db = (child / "kanban.db").exists()
            has_meta = (child / "board.json").exists()
            if not (has_db or has_meta):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Remove or archive a board.

    ``archive=True`` (default) moves the board's directory to
    ``<root>/kanban/boards/_archived/<slug>-<timestamp>/`` so the data
    is recoverable. ``archive=False`` deletes the directory outright.

    The ``default`` board cannot be removed — raises :class:`ValueError`.
    Returns a summary dict describing what happened (``{"slug", "action",
    "new_path"}``).
    """
    _assert_not_delegated_child_mutation()
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    if normed == DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # If the user removed the currently-active board, revert to default.
    if get_current_board() == normed:
        clear_current_board()

    # A concurrent connect(board=normed) after the rename/delete recreates
    # an empty sqlite file via mkdir(exist_ok=True); the cache entry must be
    # dropped first so the schema init pass re-runs on that fresh file.
    _INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        archive_root = boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        # Avoid collision on rapid double-archives.
        suffix = 1
        while target.exists():
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    else:
        import shutil
        shutil.rmtree(d)
        return {"slug": normed, "action": "deleted", "new_path": ""}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Unified non-success counter. Incremented on any of:
    #   * spawn failure (dispatcher couldn't launch the worker)
    #   * timed_out outcome (worker exceeded max_runtime_seconds)
    #   * crashed outcome (worker PID vanished)
    # Reset to 0 only on a successful completion. See
    # ``_record_task_failure`` for the circuit-breaker trip rule.
    # (Pre-rename column: ``spawn_failures``.)
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    # Short excerpt of the last failure's error text (any outcome, not
    # just spawn). Pre-rename column: ``last_spawn_error``.
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    # Force-loaded skills for the worker on this task (passed via
    # --skills). Stored as a JSON array of skill names. None = use only
    # the defaults; empty list = explicitly no extra skills.
    skills: Optional[list] = None
    model_override: Optional[str] = None
    # Provider that ``model_override`` belongs to. When set, the dispatcher
    # passes ``--provider <name>`` alongside ``-m <model>`` so the worker
    # resolves the model against the right backend instead of the profile's
    # configured provider. NULL = worker profile's provider resolves the
    # model (pre-existing behaviour). Solves the "model from provider A,
    # profile configured for provider B" mismatch class.
    provider_override: Optional[str] = None
    # Per-task override for the consecutive-failure circuit breaker.
    # The value is the failure count at which the breaker trips — e.g.
    # ``max_retries=1`` blocks on the first failure (zero retries),
    # ``max_retries=3`` blocks on the third (two retries allowed).
    # ``None`` (the common case) falls through to the dispatcher-level
    # ``kanban.failure_limit`` config, and then to ``DEFAULT_FAILURE_LIMIT``.
    # Name matches the ``--max-retries`` CLI flag on ``kanban create``.
    max_retries: Optional[int] = None
    # When True, the dispatched worker runs in a Ralph-style goal loop
    # (the same engine behind the ``/goal`` slash command): after each
    # turn an auxiliary judge model evaluates the worker's response
    # against this card's title/body (treated as the goal). If the judge
    # says "not done" and budget remains, the worker is fed a
    # continuation prompt IN THE SAME SESSION and keeps working until the
    # judge agrees, the goal-turn budget is exhausted (→ kanban_block),
    # or the worker explicitly blocks/completes. ``False`` (default) =
    # the classic single-shot worker. ``goal_max_turns`` bounds the loop.
    goal_mode: bool = False
    # Goal-loop turn budget for ``goal_mode`` workers. ``None`` falls
    # through to the goals engine default (``goals.DEFAULT_MAX_TURNS``).
    goal_max_turns: Optional[int] = None
    # Originating chat/agent session id, when the task was created from
    # within an agent loop that propagated ``HERMES_SESSION_ID``. NULL for
    # tasks created from the CLI, the dashboard, or any path that doesn't
    # set the env var. Lets clients render a per-session board without
    # relying on tenant + time-window heuristics.
    session_id: Optional[str] = None
    # Typed block reason (one of VALID_BLOCK_KINDS) or None for legacy/un-typed
    # blocks. Set by ``block_task``; preserved across unblock so a re-block for
    # the same kind is recognisable as an unblock↔re-block loop.
    block_kind: Optional[str] = None
    # Unblock-loop counter. See the column comment in SCHEMA_SQL and
    # ``BLOCK_RECURRENCE_LIMIT``. Reset only on successful completion.
    block_recurrences: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        keys = set(row.keys())
        # Parse skills JSON blob if present
        skills_value: Optional[list] = None
        if "skills" in keys and row["skills"]:
            try:
                parsed = json.loads(row["skills"])
                if isinstance(parsed, list):
                    skills_value = [str(s) for s in parsed if s]
            except Exception:
                skills_value = None
        return cls(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            assignee=row["assignee"],
            status=row["status"],
            priority=row["priority"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            workspace_kind=row["workspace_kind"],
            workspace_path=row["workspace_path"],
            branch_name=row["branch_name"] if "branch_name" in keys else None,
            project_id=row["project_id"] if "project_id" in keys else None,
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            tenant=row["tenant"] if "tenant" in keys else None,
            result=row["result"] if "result" in keys else None,
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
            consecutive_failures=(
                row["consecutive_failures"] if "consecutive_failures" in keys
                # Pre-migration fallback: ``_migrate_add_optional_columns`` always
                # adds ``consecutive_failures`` now, so this branch is only reachable
                # on a DB that was never opened since pre-#20410 code ran. Keep for
                # belt-and-suspenders safety; in practice it is dead code post-migration.
                else (row["spawn_failures"] if "spawn_failures" in keys else 0)
            ),
            worker_pid=row["worker_pid"] if "worker_pid" in keys else None,
            last_failure_error=(
                row["last_failure_error"] if "last_failure_error" in keys
                # Same belt-and-suspenders fallback as consecutive_failures above.
                else (row["last_spawn_error"] if "last_spawn_error" in keys else None)
            ),
            max_runtime_seconds=(
                row["max_runtime_seconds"] if "max_runtime_seconds" in keys else None
            ),
            last_heartbeat_at=(
                row["last_heartbeat_at"] if "last_heartbeat_at" in keys else None
            ),
            current_run_id=(
                row["current_run_id"] if "current_run_id" in keys else None
            ),
            workflow_template_id=(
                row["workflow_template_id"] if "workflow_template_id" in keys else None
            ),
            current_step_key=(
                row["current_step_key"] if "current_step_key" in keys else None
            ),
            skills=skills_value,
            model_override=row["model_override"] if "model_override" in keys and row["model_override"] else None,
            provider_override=(
                row["provider_override"]
                if "provider_override" in keys and row["provider_override"]
                else None
            ),
            max_retries=(
                row["max_retries"] if "max_retries" in keys else None
            ),
            goal_mode=(
                bool(row["goal_mode"]) if "goal_mode" in keys and row["goal_mode"] else False
            ),
            goal_max_turns=(
                row["goal_max_turns"] if "goal_max_turns" in keys and row["goal_max_turns"] else None
            ),
            session_id=(
                row["session_id"] if "session_id" in keys else None
            ),
            block_kind=(
                row["block_kind"] if "block_kind" in keys and row["block_kind"] else None
            ),
            block_recurrences=(
                int(row["block_recurrences"])
                if "block_recurrences" in keys and row["block_recurrences"] is not None
                else 0
            ),
        )


@dataclass
class Run:
    """In-memory view of a ``task_runs`` row.

    A run is one attempt to execute a task — created on claim, closed
    on complete/block/crash/timeout/spawn_failure/reclaim. Multiple runs
    per task when retries happen. Carries the claim machinery, PID,
    heartbeat, and the structured handoff summary that downstream workers
    read via ``build_worker_context``.
    """

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else None
        except Exception:
            meta = None
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            profile=row["profile"],
            step_key=row["step_key"],
            status=row["status"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            worker_pid=row["worker_pid"],
            max_runtime_seconds=row["max_runtime_seconds"],
            last_heartbeat_at=row["last_heartbeat_at"],
            started_at=int(row["started_at"]),
            ended_at=(int(row["ended_at"]) if row["ended_at"] is not None else None),
            outcome=row["outcome"],
            summary=row["summary"],
            metadata=meta,
            error=row["error"],
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,
    -- the task's worktree is anchored under the project's primary repo with a
    -- deterministic branch name instead of a random wt/<task-id> fallback.
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Passed to the worker via `--skills`. NULL or empty array = no extras.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Provider the model override belongs to. When set (alongside
    -- model_override), the dispatcher passes --provider <name> so the
    -- worker resolves the model against the right backend instead of the
    -- profile's configured provider. NULL = profile provider.
    provider_override    TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var. Indexed so per-session list queries stay cheap on
    -- larger boards.
    session_id           TEXT,
    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or
    -- NULL for legacy/un-typed blocks). Drives routing: ``dependency`` never
    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go
    -- to ``blocked`` for a human. Preserved across unblock so a re-block for
    -- the SAME kind can be recognised as a loop.
    block_kind           TEXT,
    -- Unblock-loop counter. Incremented each time a task is re-blocked for the
    -- same truly-blocked reason after having been unblocked. When it reaches
    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of
    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a
    -- successful completion — NOT on unblock (resetting on unblock is exactly
    -- the amnesia that let the loop run unbounded).
    block_recurrences    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    chat_type     TEXT,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- Per-board broker/orchestrator consumer cursor. Same shape and same atomic
-- claim discipline as ``kanban_notify_subs`` (BEGIN IMMEDIATE + CAS on
-- ``last_event_id``), but scoped to the whole board rather than one task, so a
-- control-loop consumer can tail ``task_events`` across every card on this
-- board exactly once.
--
-- There is deliberately no separate cursor file, JSON sidecar, or Markdown
-- marker: ``task_events.id`` is the only cursor authority, and it lives in the
-- same DB and the same transaction as the rows it orders.
CREATE TABLE IF NOT EXISTS kanban_broker_subs (
    consumer      TEXT NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    -- SHA-256 of a shared secret, when the consumer registered with one.
    -- NULL = unauthenticated (legacy/local use); a non-NULL digest makes the
    -- name unusable without the matching token.
    token_sha256  TEXT,
    PRIMARY KEY (consumer)
);

-- The ONLY authority for a provider/session mapping. A binding that is not in
-- this table does not exist: request preparation reads it here and nowhere
-- else, so an in-memory SessionBinding can no longer drive a resume.
--
-- One row per run: the run is the natural idempotency key, and a run cannot
-- have two live sessions without the ambiguity this slice refuses to resolve.
-- `retired_at IS NULL` means live; retirement is a durable fact, not a flag
-- the caller passes in.
CREATE TABLE IF NOT EXISTS kanban_session_bindings (
    run_id      INTEGER NOT NULL,
    task_id     TEXT    NOT NULL,
    provider    TEXT    NOT NULL,
    session_id  TEXT    NOT NULL,
    -- Provenance vocabulary: 'dispatcher_spawn' | 'operator_declared'.
    -- 'inferred' is rejected at write time and can never be stored.
    source      TEXT    NOT NULL,
    seat_id     TEXT,
    owner       TEXT    NOT NULL,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    retired_at  INTEGER,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (run_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.RLock()
_SQLITE_HEADER = b"SQLite format 3\x00"
DEFAULT_BUSY_TIMEOUT_MS = 120_000

# Maximum number of ``<db>.corrupt.<hash>.bak`` quarantine files retained per
# board DB. Content-addressing already dedupes identical corrupt bytes, but
# repeatedly-mutating corruption (partial repairs, further damage between
# dispatcher retries) mints a new fingerprint each time; without a cap a user
# accumulated 124 backups. Oldest-by-mtime files beyond the cap are pruned
# right after each new backup is created.
_CORRUPT_BACKUP_RETENTION = 10

# Bounded acquire for the cross-process init lock (#36644). The original bare
# blocking flock had no timeout, so a wedged holder blocked the dispatcher's
# next-tick connect forever. We retry a non-blocking acquire up to this
# deadline, polling at this interval, then proceed without the cross-process
# lock (the in-process _INIT_LOCK + idempotent init remain the backstop).
_INIT_LOCK_TIMEOUT_SECONDS = 10.0
_INIT_LOCK_POLL_SECONDS = 0.05


def _resolve_busy_timeout_ms() -> int:
    """Return the SQLite busy timeout for Kanban connections.

    Kanban is the shared cross-profile dispatch bus, so worker stampedes are
    expected.  A long busy timeout lets SQLite serialize writers via WAL rather
    than surfacing transient ``database is locked`` failures during bursts.
    """
    raw = os.environ.get("HERMES_KANBAN_BUSY_TIMEOUT_MS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_BUSY_TIMEOUT_MS


def _sqlite_connect(path: Path) -> sqlite3.Connection:
    """Open a Kanban SQLite connection with consistent lock waiting.

    Uses ``connect_tracked`` so the live-connection registry knows this file
    is open: while it is, byte-level probes of the same file are refused,
    because an ``open()``/``close()`` would cancel this process's POSIX
    advisory locks on the database (see ``hermes_cli.sqlite_safe_read``).
    The registration is released automatically when the connection closes.
    """
    from hermes_cli.sqlite_safe_read import connect_tracked

    busy_timeout_ms = _resolve_busy_timeout_ms()
    conn = connect_tracked(
        path,
        connect_fn=sqlite3.connect,
        isolation_level=None,
        timeout=busy_timeout_ms / 1000.0,
    )
    # ``sqlite3.connect(timeout=...)`` normally maps to busy_timeout, but set
    # the PRAGMA explicitly so it is observable and survives future wrapper
    # changes. Parameter binding is not supported for PRAGMA assignments.
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


@contextlib.contextmanager
def _cross_process_init_lock(path: Path):
    """Serialize first-connect WAL/schema/integrity setup across processes.

    ``_INIT_LOCK`` only protects threads inside one Python process. During a
    dispatcher burst, many worker processes can all hit a fresh/legacy board at
    once and each process has an empty ``_INITIALIZED_PATHS`` cache. This file
    lock keeps header validation, integrity probing, WAL activation, and
    additive migrations single-file/single-writer across the whole host while
    leaving normal post-init DB usage concurrent under SQLite WAL.

    The acquire is **bounded** (issue #36644): the original bare blocking
    ``flock(LOCK_EX)`` had no timeout, so a single process stalled inside the
    critical section (or a stale lock held by a wedged worker) blocked every
    other ``connect()`` — including the long-lived gateway dispatcher's
    next-tick connect — forever, with no traceback and no recovery short of a
    restart. We now retry a non-blocking acquire up to a deadline; on timeout
    we log a WARNING and proceed WITHOUT the cross-process lock. That is safe:
    the in-process ``_INIT_LOCK`` still serializes same-process threads, and
    the init work itself is idempotent (``CREATE TABLE IF NOT EXISTS`` +
    additive migrations), so the worst case of two processes racing first-init
    is redundant work, not corruption. A bounded "proceed anyway" beats an
    unbounded hang that silently stops the board.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".init.lock")
    handle = lock_path.open("a+b")
    acquired = False
    try:
        deadline = time.monotonic() + _INIT_LOCK_TIMEOUT_SECONDS
        if _IS_WINDOWS:
            import msvcrt

            locking = getattr(msvcrt, "locking")
            nb_lock = getattr(msvcrt, "LK_NBLCK")
            while True:
                try:
                    handle.seek(0)
                    locking(handle.fileno(), nb_lock, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_INIT_LOCK_POLL_SECONDS)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_INIT_LOCK_POLL_SECONDS)
        if not acquired:
            _log.warning(
                "kanban init lock for %s not acquired within %.0fs — proceeding "
                "without the cross-process lock (in-process lock + idempotent "
                "init are the correctness backstop). A stuck holder is no longer "
                "able to block this connect indefinitely (#36644).",
                lock_path, _INIT_LOCK_TIMEOUT_SECONDS,
            )
        yield
    finally:
        try:
            if acquired:
                if _IS_WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    locking = getattr(msvcrt, "locking")
                    unlock_mode = getattr(msvcrt, "LK_UNLCK")
                    locking(handle.fileno(), unlock_mode, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextlib.contextmanager
def _dispatch_tick_lock(db_path: Path):
    """Non-blocking single-writer guard around one dispatcher tick.

    Yields ``True`` when this process holds the board's dispatch lock and
    may proceed with the tick, or ``False`` when another process already
    holds it (the caller should skip the tick this round).

    Motivation (issue #35240): a ``hermes gateway run --replace`` /
    ``gateway restart`` invoked from a shell on a systemd/launchd host can
    leave an orphan gateway whose dispatcher escapes the service cgroup,
    survives ``systemctl restart``, and becomes a *second* long-lived
    writer on the same ``kanban.db``. Two dispatchers that each believe
    they own the file both pass SQLite ``busy_timeout`` and then race on
    WAL frames — the documented root cause of multi-writer corruption.
    The startup guard (``_guard_supervised_gateway_conflict``) blocks the
    common way an orphan is born, but this lock is the defense-in-depth
    that prevents two dispatchers from ever writing concurrently
    *regardless of how the second one got there*.

    The lock is **non-blocking** on purpose: the gateway's async watcher
    must never stall on a held lock. A losing dispatcher simply skips its
    tick (the winner is making progress on the same board), and tries
    again next interval.

    Board-scoped: the lock file is a ``.dispatch.lock`` sibling of the
    board's ``kanban.db``, so unrelated boards tick independently. On
    platforms without ``fcntl``/``msvcrt`` the guard degrades to a no-op
    (yields ``True``) — single-writer enforcement is best-effort and the
    orphan-dispatcher scenario is specific to POSIX service managers.
    """
    lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
    handle = None
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if _IS_WINDOWS:
            try:
                import msvcrt

                handle.seek(0)
                locking = getattr(msvcrt, "locking")
                # LK_NBLCK = non-blocking exclusive byte-range lock.
                nb_lock = getattr(msvcrt, "LK_NBLCK")
                locking(handle.fileno(), nb_lock, 1)
                acquired = True
            except (OSError, AttributeError):
                acquired = False
        else:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                acquired = False
    except OSError:
        # Could not even open the lock file (permissions, read-only FS).
        # Degrade to a no-op so a probe failure never blocks dispatch.
        acquired = True
        handle = None
    try:
        yield acquired
    finally:
        if handle is not None:
            try:
                if acquired:
                    if _IS_WINDOWS:
                        import msvcrt

                        handle.seek(0)
                        locking = getattr(msvcrt, "locking")
                        unlock_mode = getattr(msvcrt, "LK_UNLCK")
                        locking(handle.fileno(), unlock_mode, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, AttributeError):
                pass
            finally:
                handle.close()


# Periodic WAL checkpoint state for the dispatcher tick path. The kanban
# connections run with ``wal_autocheckpoint=100``, but a passive
# autocheckpoint can be starved forever on a busy multi-process board (any
# reader with an open snapshot blocks the WAL reset), letting the -wal file
# grow without bound between gateway restarts. Once per coarse interval the
# dispatcher — the board's single writer during a tick, and holding the
# dispatch flock — issues an explicit ``wal_checkpoint(TRUNCATE)``.
# Best-effort: a busy/locked checkpoint is logged at DEBUG and retried next
# interval. Keyed per resolved DB path so multi-board dispatchers checkpoint
# each board on its own clock.
_WAL_CHECKPOINT_INTERVAL_SECONDS = 300.0
_LAST_WAL_CHECKPOINT: dict[str, float] = {}
_WAL_CHECKPOINT_LOCK = threading.Lock()


def _maybe_checkpoint_wal(conn: sqlite3.Connection, db_path: Path) -> None:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` at a coarse interval.

    Called from the dispatcher tick while the board's dispatch lock is
    held. No-ops (cheaply) until ``_WAL_CHECKPOINT_INTERVAL_SECONDS`` has
    elapsed since this process last checkpointed this board. Never raises:
    the checkpoint is pure hygiene and must not fail a dispatch tick.
    """
    try:
        key = str(db_path.resolve())
    except OSError:
        key = str(db_path)
    now = time.monotonic()
    with _WAL_CHECKPOINT_LOCK:
        last = _LAST_WAL_CHECKPOINT.get(key)
        if last is not None and (now - last) < _WAL_CHECKPOINT_INTERVAL_SECONDS:
            return
        # Claim the slot before doing the work so concurrent ticks (other
        # threads in this process) don't double-checkpoint on the boundary.
        _LAST_WAL_CHECKPOINT[key] = now
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        _log.debug(
            "kanban WAL checkpoint (TRUNCATE) on %s -> %s "
            "(busy, wal_frames, checkpointed_frames)",
            key, tuple(row) if row is not None else None,
        )
    except sqlite3.Error as exc:
        _log.debug("kanban WAL checkpoint on %s skipped: %s", key, exc)


def _looks_like_tls_record_at(data: bytes, offset: int) -> bool:
    """Return True for a TLS record header at ``data[offset:]``."""
    if len(data) < offset + 5:
        return False
    content_type = data[offset]
    major = data[offset + 1]
    minor = data[offset + 2]
    length = int.from_bytes(data[offset + 3:offset + 5], "big")
    return (
        content_type in {0x14, 0x15, 0x16, 0x17}
        and major == 0x03
        and minor in {0x00, 0x01, 0x02, 0x03, 0x04}
        and 0 < length <= 18432
    )


def _validate_sqlite_header(path: Path) -> None:
    """Fail early with an actionable error for non-SQLite Kanban DB files.

    ``sqlite3.connect()`` creates missing and zero-byte files, so those are
    allowed. Existing non-empty files must have the SQLite header before we
    hand them to SQLite/WAL setup. This keeps corrupted page-0 failures from
    being collapsed into a generic PRAGMA error and lets the gateway's corrupt
    board handling identify the board by fingerprint.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.st_size == 0:
        return
    # Byte-level probe, so it must run BEFORE any connection to this path
    # exists (connect() calls it under the init lock, ahead of _sqlite_connect).
    # read_header_bytes_preopen refuses once a connection is live, because the
    # close() would cancel this process's POSIX locks on the file.
    from hermes_cli.sqlite_safe_read import read_header_bytes_preopen

    head = read_header_bytes_preopen(path, length=64)
    if head is None:
        return
    if head.startswith(_SQLITE_HEADER):
        return
    signature = ""
    if head.startswith(b"SQLit") and _looks_like_tls_record_at(head, 5):
        signature = " (TLS record header detected at byte offset 5)"
    elif _looks_like_tls_record_at(head, 0):
        signature = " (TLS record header detected at byte offset 0)"
    raise sqlite3.DatabaseError(
        "file is not a database: invalid SQLite header for "
        f"{path}{signature}; first_32={head[:32].hex(' ')}"
    )


class KanbanDbCorruptError(RuntimeError):
    """Raised when an existing kanban DB file fails integrity checks.

    Fail-closed guard against silent recreation of a corrupt board file,
    which would otherwise destroy the user's tasks. Carries both the
    original path and the timestamped backup we made before refusing.
    """

    def __init__(self, db_path: Path, backup_path: Optional[Path], reason: str):
        self.db_path = db_path
        self.backup_path = backup_path
        self.reason = reason
        backup_str = str(backup_path) if backup_path is not None else "<backup failed>"
        super().__init__(
            f"Refusing to open corrupt kanban DB at {db_path}: {reason}. "
            f"Original preserved; backup at {backup_str}."
        )


def _prune_corrupt_backups(
    parent: Path, base_name: str, keep: Optional[Path] = None,
) -> None:
    """Cap the number of retained ``<db>.corrupt.<hash>.bak`` files.

    Content-addressed backups dedupe identical corrupt bytes, but a board
    whose file keeps changing between corruption events (partial repairs,
    ongoing damage, fleets of retrying dispatchers) can still accumulate
    backups without bound — a user reported 124 of them. After creating a
    new backup we keep only the ``_CORRUPT_BACKUP_RETENTION`` most recent
    (by mtime) and delete the rest, including their copied ``-wal``/``-shm``
    sidecars. ``keep`` (the just-created backup) is never pruned regardless
    of its mtime — ``shutil.copy2`` preserves the source file's timestamp,
    which may be older than existing backups. Best-effort: prune failures
    never mask the corruption error the caller is about to raise.
    """
    try:
        backups = [
            candidate
            for candidate in parent.glob(f"{base_name}.corrupt.*.bak")
            if candidate.is_file() and candidate != keep
        ]
    except OSError:
        return
    budget = _CORRUPT_BACKUP_RETENTION - (1 if keep is not None else 0)
    budget = max(budget, 0)
    if len(backups) <= budget:
        return

    def _mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    backups.sort(key=_mtime, reverse=True)
    for stale in backups[budget:]:
        for victim in (
            stale,
            stale.with_name(stale.name + "-wal"),
            stale.with_name(stale.name + "-shm"),
        ):
            try:
                victim.unlink(missing_ok=True)
            except OSError:
                pass


def _backup_corrupt_db(path: Path) -> Optional[Path]:
    """Copy a corrupt DB (and its WAL/SHM sidecars) to a content-addressed backup.

    The backup filename is deterministic in the main DB's sha256, so repeated
    quarantines of the same corrupt bytes (gateway restarts, dispatcher retries,
    multi-profile fleets all hitting the same shared DB) reuse one backup
    instead of amplifying disk usage by N. If the corrupt bytes actually
    change between attempts — e.g. a partial repair or further damage — the
    fingerprint changes and a separate backup is preserved.

    Returns the backup path of the main DB file, or ``None`` if the copy
    itself failed (the caller still raises loudly in that case).

    Writes are confined to the original DB's parent directory. The backup
    basename is derived purely from ``path.name`` and a content hash, never
    from caller-supplied directory segments — no traversal is possible.
    """
    # Resolve once and pin the parent so subsequent path operations cannot
    # escape it. ``Path.resolve()`` collapses any ``..`` segments and
    # symlinks, and we only ever write inside ``parent``.
    resolved = path.resolve()
    parent = resolved.parent
    base_name = resolved.name  # basename only
    # This reads the whole DB file to fingerprint it. That is a close()-on-a-
    # database-file hazard (it cancels this process's POSIX advisory locks --
    # see hermes_cli.sqlite_safe_read), so it must only run once the board has
    # been taken out of service. Every caller reaches here on the corrupt/
    # quarantine path after closing its probe connection, but another
    # SessionDB/kanban connection elsewhere in the process would still be at
    # risk -- so REFUSE rather than warn-and-proceed. Losing a forensic copy
    # is strictly better than corrupting the live database we are trying to
    # rescue.
    from hermes_cli.sqlite_safe_read import has_live_connection

    if has_live_connection(resolved):
        _log.error(
            "refusing to quarantine %s: a connection to it is still open in "
            "this process, and fingerprinting the file would cancel that "
            "connection's POSIX locks. Close all connections first.",
            resolved,
        )
        return None
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    token = digest.hexdigest()[:16]
    candidate = parent / f"{base_name}.corrupt.{token}.bak"
    # Defensive: candidate must still be inside parent after construction.
    if candidate.parent != parent:
        return None
    if not candidate.exists():
        try:
            shutil.copy2(resolved, candidate)
        except OSError:
            return None
        # A NEW backup landed on disk — enforce the retention cap so
        # mutating-corruption loops can't accumulate quarantines forever.
        _prune_corrupt_backups(parent, base_name, keep=candidate)
    for suffix in ("-wal", "-shm"):
        sidecar = parent / (base_name + suffix)
        if sidecar.parent != parent or not sidecar.exists():
            continue
        sidecar_backup = parent / (candidate.name + suffix)
        if sidecar_backup.parent != parent or sidecar_backup.exists():
            continue
        try:
            shutil.copy2(sidecar, sidecar_backup)
        except OSError:
            pass
    return candidate


# Repairable integrity_check error classes. Both shapes are *index-scoped*:
# the table b-tree is intact and only a secondary index disagrees with it,
# which REINDEX rebuilds losslessly from the table data. The index name is
# parsed generically from the message — no hardcoded index list. Any other
# integrity_check message (page corruption, "database disk image is
# malformed", freelist damage, …) is NOT repairable this way and keeps the
# fail-closed behavior.
_REPAIRABLE_INDEX_ERROR_PATTERNS = (
    re.compile(r"^wrong # of entries in index (?P<index>.+)$"),
    re.compile(r"^row \d+ missing from index (?P<index>.+)$"),
)


def _integrity_messages_ok(messages: list[str]) -> bool:
    """True iff ``PRAGMA integrity_check`` output is the single ``ok`` row."""
    return len(messages) == 1 and messages[0].strip().lower() == "ok"


def _run_integrity_check(conn: sqlite3.Connection) -> list[str]:
    """Return all ``PRAGMA integrity_check`` message rows as strings."""
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return [str(row[0]) for row in rows if row is not None and row[0] is not None]


def _repairable_index_names(messages: list[str]) -> Optional[list[str]]:
    """Return the distinct index names iff EVERY message is index-repairable.

    ``None`` when any line falls outside the repairable index-class errors
    (or when there are no messages at all) — the caller must then fail
    closed exactly as before. Order of first appearance is preserved so the
    REINDEX pass is deterministic.
    """
    names: list[str] = []
    saw_any = False
    for raw in messages:
        message = (raw or "").strip()
        if not message:
            continue
        for pattern in _REPAIRABLE_INDEX_ERROR_PATTERNS:
            match = pattern.match(message)
            if match:
                break
        else:
            return None
        saw_any = True
        name = match.group("index").strip()
        if name and name not in names:
            names.append(name)
    if not saw_any or not names:
        return None
    return names


def _attempt_index_reindex_repair(
    path: Path, index_names: list[str],
) -> tuple[bool, list[str]]:
    """REINDEX the named indexes, then re-run ``PRAGMA integrity_check``.

    Tries a per-index ``REINDEX "<name>"`` first (cheapest, most targeted);
    if any per-index statement fails — e.g. the parsed name does not resolve
    because integrity_check reported an internal/auto index — falls back to
    a bare ``REINDEX`` of the whole database. Returns
    ``(clean, post_repair_messages)``; never raises. Callers must hold the
    board's cross-process init flock so no other process connects mid-repair.
    """
    try:
        conn = _sqlite_connect(path)
    except sqlite3.Error as exc:
        return False, [f"could not reopen for REINDEX: {exc}"]
    try:
        try:
            for name in index_names:
                escaped = name.replace('"', '""')
                conn.execute(f'REINDEX "{escaped}"')
        except sqlite3.Error:
            # Per-index rebuild failed (unresolvable parsed name, auto
            # index, …) — bare REINDEX rebuilds every index in the DB.
            conn.execute("REINDEX")
        messages = _run_integrity_check(conn)
    except sqlite3.Error as exc:
        return False, [f"REINDEX failed: {exc}"]
    finally:
        conn.close()
    return _integrity_messages_ok(messages), messages


def _guard_existing_db_is_healthy(path: Path) -> None:
    """Run ``PRAGMA integrity_check`` on an existing non-empty DB file.

    Opens the probe in read/write mode so SQLite can recover or
    checkpoint a healthy WAL/hot-journal DB before we declare it
    corrupt.

    **Narrow auto-repair:** when the integrity failure consists *only* of
    index-scoped errors (``wrong # of entries in index <name>`` / ``row N
    missing from index <name>``), the table b-trees are intact and REINDEX
    rebuilds the damaged indexes losslessly. In that case we take the
    corrupt backup FIRST (same content-addressed quarantine as the
    fail-closed path), run REINDEX under the caller-held init flock,
    re-run ``integrity_check``, and proceed only if it comes back clean.
    Anything else — page corruption, ``malformed`` images, a REINDEX that
    does not produce a clean re-check — fails closed exactly as before:
    copy the file (and any WAL/SHM sidecars) to a backup and raise
    :class:`KanbanDbCorruptError` so callers cannot silently recreate the
    schema on top of a damaged DB.

    Transient lock/busy errors (``sqlite3.OperationalError``) are NOT
    treated as corruption; they propagate raw so the caller sees a
    normal lock failure and no spurious ``.corrupt`` backup is made.

    No-op for missing files, zero-byte files (treated as fresh), and
    paths already proven healthy this process (cache hit).

    Path-trust note: ``path`` arrives via :func:`connect`, which itself
    resolves it from an explicit ``db_path`` argument, the
    :func:`kanban_db_path` env-var chain, or the kanban-home default —
    all sources Hermes treats as user-controlled-but-trusted on the
    user's own machine. We additionally resolve the path here and
    confine all filesystem writes to its parent directory so any
    accidental ``..`` segments are collapsed before any I/O happens.
    """
    # Resolve before any I/O. ``Path.resolve()`` normalizes ``..`` and
    # symlinks, giving us a canonical path whose parent dir we can pin.
    try:
        resolved = path.resolve()
    except OSError:
        return
    try:
        if not resolved.exists() or resolved.stat().st_size == 0:
            return
    except OSError:
        return
    if str(resolved) in _INITIALIZED_PATHS:
        return
    reason: Optional[str] = None
    messages: list[str] = []
    try:
        probe = _sqlite_connect(resolved)
        try:
            messages = _run_integrity_check(probe)
        finally:
            probe.close()
        if not _integrity_messages_ok(messages):
            reason = (
                f"integrity_check returned "
                f"{messages[0] if messages else '<no row>'!r}"
            )
    except sqlite3.OperationalError:
        # Lock contention, busy, transient IO — not corruption. Let it propagate.
        raise
    except sqlite3.DatabaseError as exc:
        reason = f"sqlite refused to open file: {exc}"
    if reason is None:
        return
    # Quarantine FIRST — both the repair path and the fail-closed path
    # preserve the pre-touch bytes before anything mutates the file.
    backup = _backup_corrupt_db(resolved)
    index_names = _repairable_index_names(messages)
    if index_names:
        _log.warning(
            "kanban DB %s failed integrity_check with index-only errors "
            "(%s); pre-repair backup at %s — attempting REINDEX auto-repair.",
            resolved, ", ".join(index_names),
            backup if backup is not None else "<backup failed>",
        )
        repaired, post = _attempt_index_reindex_repair(resolved, index_names)
        if repaired:
            _log.warning(
                "kanban DB %s auto-repaired via REINDEX (%s); "
                "integrity_check now clean. Pre-repair copy kept at %s.",
                resolved, ", ".join(index_names),
                backup if backup is not None else "<backup failed>",
            )
            return
        reason = (
            f"{reason}; REINDEX auto-repair attempted but integrity_check "
            f"still returned {post[0] if post else '<no row>'!r}"
        )
    raise KanbanDbCorruptError(resolved, backup, reason)


@dataclass
class RepairResult:
    """Outcome of :func:`repair_db` for CLI/status reporting.

    ``status`` is one of:

    * ``"ok"``        — integrity_check was already clean; nothing done.
    * ``"repaired"``  — index-only errors found, REINDEX applied, re-check
      clean. ``backup_path`` holds the pre-repair quarantine copy.
    * ``"corrupt"``   — still corrupt: either a non-index error class
      (fail-closed, no repair attempted) or a REINDEX whose re-check did
      not come back clean.
    * ``"missing"``   — no DB file (or zero-byte placeholder); nothing to do.
    """

    status: str
    db_path: Path
    messages: list[str] = field(default_factory=list)
    post_repair_messages: list[str] = field(default_factory=list)
    backup_path: Optional[Path] = None
    reindexed: list[str] = field(default_factory=list)


def repair_db(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> RepairResult:
    """Probe a kanban DB and apply the narrow index-REINDEX repair if needed.

    Shares the exact policy of :func:`_guard_existing_db_is_healthy`: only
    integrity failures composed *entirely* of index-scoped errors are
    repairable; the corrupt bytes are quarantined via
    :func:`_backup_corrupt_db` BEFORE any mutation; the REINDEX runs under
    the board's cross-process init flock; and anything else stays corrupt
    (fail-closed) for the caller to surface. Unlike the guard this never
    raises :class:`KanbanDbCorruptError` — it returns a structured
    :class:`RepairResult` so ``hermes kanban repair`` can report and choose
    its own exit code.

    Transient ``sqlite3.OperationalError`` (locked/busy) still propagates
    raw, exactly like the guard: a locked healthy DB is not corruption and
    must not be quarantined.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        if not resolved.exists() or resolved.stat().st_size == 0:
            return RepairResult(status="missing", db_path=resolved)
    except OSError:
        return RepairResult(status="missing", db_path=resolved)

    with _cross_process_init_lock(resolved):
        messages: list[str] = []
        try:
            probe = _sqlite_connect(resolved)
            try:
                messages = _run_integrity_check(probe)
            finally:
                probe.close()
        except sqlite3.OperationalError:
            # Locked/busy — not corruption; let the caller report it raw.
            raise
        except sqlite3.DatabaseError as exc:
            # Same quarantine the connect-time guard takes for a file
            # sqlite refuses to open at all (e.g. malformed page 1).
            return RepairResult(
                status="corrupt",
                db_path=resolved,
                messages=[f"sqlite refused to open file: {exc}"],
                backup_path=_backup_corrupt_db(resolved),
            )
        if _integrity_messages_ok(messages):
            return RepairResult(status="ok", db_path=resolved, messages=messages)

        # Quarantine FIRST — identical policy to the connect-time guard.
        backup = _backup_corrupt_db(resolved)
        index_names = _repairable_index_names(messages)
        if not index_names:
            return RepairResult(
                status="corrupt",
                db_path=resolved,
                messages=messages,
                backup_path=backup,
            )
        repaired, post = _attempt_index_reindex_repair(resolved, index_names)
        # The file changed on disk; force the next connect() in this process
        # to re-probe instead of trusting the stale healthy-path cache.
        with _INIT_LOCK:
            _INITIALIZED_PATHS.discard(str(resolved))
        return RepairResult(
            status="repaired" if repaired else "corrupt",
            db_path=resolved,
            messages=messages,
            post_repair_messages=post,
            backup_path=backup,
            reindexed=index_names,
        )


def connect(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> sqlite3.Connection:
    """Open (and initialize if needed) the kanban DB.

    WAL mode is enabled on every connection; it's a no-op after the first
    time but keeps the code robust if the DB file is ever re-created.

    The first connection to a given path auto-runs :func:`init_db` so
    fresh installs and test harnesses that construct `connect()`
    directly don't have to remember a separate init step. Subsequent
    connections skip the schema check via a module-level path cache.

    Path resolution:

    * ``db_path`` explicit → used as-is (legacy callers, tests).
    * ``board`` explicit → resolves to that board's DB.
    * Neither → :func:`kanban_db_path` resolves via
      ``HERMES_KANBAN_DB`` env → ``HERMES_KANBAN_BOARD`` env →
      ``<root>/kanban/current`` → ``default``.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: once THIS process has initialized this path, the expensive
    # first-open work (header validation, integrity probe, schema + additive
    # migrations) is already done and cached in _INITIALIZED_PATHS. Acquiring
    # the cross-process init lock on every connect is what let a single stalled
    # holder (e.g. an external `hermes kanban list` mid-integrity-probe) block
    # the long-lived gateway dispatcher's next-tick connect() forever — an
    # unbounded flock with no timeout, no LOCK_NB, no recovery (#36644). On the
    # steady-state path there is nothing for the cross-process lock to protect
    # (no schema/migration writes run), so skip it entirely and just open the
    # connection with WAL/pragmas under the cheap in-process _INIT_LOCK.
    resolved = str(path.resolve())
    if resolved in _INITIALIZED_PATHS:
        conn = _sqlite_connect(path)
        try:
            conn.row_factory = sqlite3.Row
            with _INIT_LOCK:
                from hermes_state import apply_wal_with_fallback
                apply_wal_with_fallback(conn, db_label=f"kanban.db ({path.name})")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA wal_autocheckpoint=100")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("PRAGMA cell_size_check=ON")
        except Exception:
            conn.close()
            raise
        return conn

    with _cross_process_init_lock(path):
        # Read-only file/sidecar preflight (port of kilocode#12508) —
        # repair-or-refuse before the header/integrity probes so a stray
        # read-only kanban.db fails with an actionable message instead of
        # "attempt to write a readonly database" mid-init.
        from hermes_state import preflight_db_writability
        preflight_db_writability(path, db_label=f"kanban.db ({path.name})")
        # Cheap byte-level check first — catches the #29507 TLS-overwrite shape
        # and other invalid-header cases without opening a sqlite connection.
        _validate_sqlite_header(path)
        # Full integrity probe — catches corruption past the header (malformed
        # pages, broken internal metadata). Cached per-path after first success
        # via _INITIALIZED_PATHS so it only runs once per process per path.
        _guard_existing_db_is_healthy(path)
        resolved = str(path.resolve())
        conn = _sqlite_connect(path)
        try:
            conn.row_factory = sqlite3.Row
            with _INIT_LOCK:
                # WAL activation can take an exclusive lock while SQLite creates the
                # sidecar files for a fresh database. Keep it in the same process-local
                # critical section as schema initialization so concurrent gateway
                # startup threads do not race before _INITIALIZED_PATHS is populated.
                # WAL doesn't work on network filesystems (NFS/SMB/FUSE). Shared helper
                # falls back to DELETE with one WARNING so kanban stays usable there.
                # See hermes_state._WAL_INCOMPAT_MARKERS for detection logic.
                from hermes_state import apply_wal_with_fallback
                apply_wal_with_fallback(conn, db_label=f"kanban.db ({path.name})")
                # FULL (was NORMAL): fsync before each checkpoint to narrow the
                # crash window that can leave a b-tree page header torn.
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA wal_autocheckpoint=100")
                conn.execute("PRAGMA foreign_keys=ON")
                # Zero freed pages so a later torn write cannot expose stale
                # cell content; persisted in the DB header for new DBs.
                conn.execute("PRAGMA secure_delete=ON")
                # Surface corrupt cells as read errors instead of silent
                # wrong-data returns.
                conn.execute("PRAGMA cell_size_check=ON")
                needs_init = resolved not in _INITIALIZED_PATHS
                if needs_init:
                    # Idempotent: runs CREATE TABLE IF NOT EXISTS + the additive
                    # migrations. Cached so subsequent connect() calls in the same
                    # process are cheap. The lock prevents same-process dispatcher
                    # threads from racing through the additive ALTER TABLE pass with
                    # stale PRAGMA snapshots during gateway startup.
                    conn.executescript(SCHEMA_SQL)
                    _migrate_add_optional_columns(conn)
                    _INITIALIZED_PATHS.add(resolved)
        except Exception:
            conn.close()
            raise
    return conn


@contextlib.contextmanager
def connect_closing(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
):
    """Open a kanban DB connection and guarantee it is closed on exit.

    Use this instead of ``with kb.connect() as conn:`` — sqlite3's
    built-in connection context manager only commits/rollbacks the
    transaction; it does NOT close the file descriptor. In long-lived
    processes (gateway, dashboard) that route every kanban operation
    through ``connect()`` (e.g. ``run_slash`` dispatching ``/kanban …``
    commands, ``decompose_task_endpoint`` calling
    ``kanban_decompose.decompose_task``), the unclosed connections
    accumulate as open FDs to ``kanban.db`` and ``kanban.db-wal``. After
    enough operations the process hits the kernel FD limit and dies
    with ``[Errno 24] Too many open files``.

    See #33159 for the production incident.

    The ``connect()`` function itself remains unchanged so callers that
    intentionally manage the connection lifetime (tests, long-lived
    callers) continue to work.
    """
    conn = connect(db_path=db_path, board=board)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> Path:
    """Create the schema if it doesn't exist; return the path used.

    Kept as a public entry point so CLI ``hermes kanban init`` and the
    daemon have something explicit to call. Unlike :func:`connect`'s
    first-time auto-init (which caches by path), ``init_db`` always
    re-runs the migration pass. Callers that know the on-disk schema
    may have drifted — tests that write legacy event kinds directly,
    external tools that upgrade an old DB file — can call this to
    force re-migration.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    # Clear the cache entry so the underlying connect() re-runs the
    # schema + migration pass unconditionally.
    with _INIT_LOCK:
        _INITIALIZED_PATHS.discard(resolved)
    with contextlib.closing(connect(path)):
        pass
    return path


def _migrate_add_optional_columns(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after v1 release to legacy DBs.

    Called by ``init_db`` so opening an old DB is always safe.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "tenant" not in cols:
        _add_column_if_missing(conn, "tasks", "tenant", "tenant TEXT")
    if "result" not in cols:
        _add_column_if_missing(conn, "tasks", "result", "result TEXT")
    if "branch_name" not in cols:
        _add_column_if_missing(conn, "tasks", "branch_name", "branch_name TEXT")
    if "project_id" not in cols:
        _add_column_if_missing(conn, "tasks", "project_id", "project_id TEXT")
    if "idempotency_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "idempotency_key", "idempotency_key TEXT"
        )
    # ``idx_tasks_idempotency`` is created unconditionally below alongside
    # the other additive-column indexes — see the block after the
    # legacy-column migration. Creating it here too would be redundant.

    # Refresh after early additive migrations above. Some existing DBs were
    # partially migrated in older releases and can already contain the later
    # columns (for example ``consecutive_failures``) even when this function's
    # initial snapshot did not. Re-snapshot here so the legacy-column migration
    # below is truly idempotent and never re-adds columns that already exist.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}

    # Legacy column migration: ``spawn_failures`` → ``consecutive_failures``
    # and ``last_spawn_error`` → ``last_failure_error``.
    #
    # Avoid ``ALTER TABLE ... RENAME COLUMN`` for two reasons:
    #   1. Primary: very old DBs may never have had ``spawn_failures`` at
    #      all, so RENAME raises OperationalError: no such column (the crash
    #      reported in issue #20842 after the #20410 update).
    #   2. Secondary: SQLite reparses the whole schema on any RENAME, which
    #      fails if related objects (views, triggers) reference the old name.
    #
    # ADD-first-then-copy is tolerant of both shapes and preserves
    # historical counter values when the legacy columns do exist.
    if "consecutive_failures" not in cols:
        added = _add_column_if_missing(
            conn,
            "tasks",
            "consecutive_failures",
            "consecutive_failures INTEGER NOT NULL DEFAULT 0",
        )
        if added and "spawn_failures" in cols:
            conn.execute(
                "UPDATE tasks SET consecutive_failures = COALESCE(spawn_failures, 0)"
            )
    if "worker_pid" not in cols:
        _add_column_if_missing(conn, "tasks", "worker_pid", "worker_pid INTEGER")
    if "last_failure_error" not in cols:
        added = _add_column_if_missing(
            conn, "tasks", "last_failure_error", "last_failure_error TEXT"
        )
        if added and "last_spawn_error" in cols:
            conn.execute(
                "UPDATE tasks SET last_failure_error = last_spawn_error"
            )
    if "max_runtime_seconds" not in cols:
        _add_column_if_missing(
            conn, "tasks", "max_runtime_seconds", "max_runtime_seconds INTEGER"
        )
    if "last_heartbeat_at" not in cols:
        _add_column_if_missing(
            conn, "tasks", "last_heartbeat_at", "last_heartbeat_at INTEGER"
        )
    if "current_run_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_run_id", "current_run_id INTEGER"
        )
    if "workflow_template_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "workflow_template_id", "workflow_template_id TEXT"
        )
    if "current_step_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_step_key", "current_step_key TEXT"
        )
    if "skills" not in cols:
        # JSON array of skill names the dispatcher force-loads into the
        # worker via --skills. NULL is fine for existing rows.
        _add_column_if_missing(conn, "tasks", "skills", "skills TEXT")

    if "max_retries" not in cols:
        # Per-task override for the consecutive-failure circuit breaker.
        # NULL = fall through to the dispatcher-level ``kanban.failure_limit``
        # config, then ``DEFAULT_FAILURE_LIMIT``. Existing rows get NULL,
        # which is the correct default (they keep the global behaviour
        # they were getting before the column existed).
        _add_column_if_missing(conn, "tasks", "max_retries", "max_retries INTEGER")

    if "model_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN model_override TEXT")

    if "provider_override" not in cols:
        # Provider the model_override belongs to. NULL = worker profile's
        # provider resolves the model (the behaviour existing rows had).
        _add_column_if_missing(
            conn, "tasks", "provider_override", "provider_override TEXT"
        )

    if "goal_mode" not in cols:
        # Ralph-style goal loop toggle for the dispatched worker. 0 (the
        # default) = classic single-shot worker, preserving the behaviour
        # existing rows had before the column existed.
        _add_column_if_missing(
            conn, "tasks", "goal_mode", "goal_mode INTEGER NOT NULL DEFAULT 0"
        )

    if "goal_max_turns" not in cols:
        # Per-task goal-loop turn budget. NULL = goals-engine default.
        _add_column_if_missing(
            conn, "tasks", "goal_max_turns", "goal_max_turns INTEGER"
        )

    if "session_id" not in cols:
        # Originating agent/chat session id, populated when the task is
        # created from within an agent loop that propagated
        # ``HERMES_SESSION_ID`` (e.g. ACP). NULL on legacy rows and on any
        # creation path that doesn't set the env var (CLI, dashboard).
        _add_column_if_missing(
            conn, "tasks", "session_id", "session_id TEXT"
        )

    if "block_kind" not in cols:
        # Typed block reason (VALID_BLOCK_KINDS) or NULL for legacy/un-typed
        # blocks. Existing blocked rows get NULL, which is treated as a
        # generic human blocker — same behaviour they had before the column.
        _add_column_if_missing(conn, "tasks", "block_kind", "block_kind TEXT")

    if "block_recurrences" not in cols:
        # Unblock-loop counter. Existing rows start at 0, so the loop breaker
        # only begins counting from the first re-block after this migration.
        _add_column_if_missing(
            conn,
            "tasks",
            "block_recurrences",
            "block_recurrences INTEGER NOT NULL DEFAULT 0",
        )

    # Indexes over additive ``tasks`` columns must be created after the
    # columns exist. Keeping them in SCHEMA_SQL breaks legacy boards: SQLite
    # parses each statement in ``executescript`` against the live schema, so a
    # ``CREATE INDEX`` over a missing column aborts initialization before the
    # additive ``ALTER TABLE`` migrations below can run. Re-running them here
    # is cheap thanks to ``IF NOT EXISTS`` and stays correct on fresh DBs
    # (where the columns already exist from SCHEMA_SQL).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)"
    )

    # task_events gained a run_id column; back-fill it as NULL for
    # historical events (they predate runs and can't be attributed).
    ev_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_events)")}
    if "run_id" not in ev_cols:
        _add_column_if_missing(conn, "task_events", "run_id", "run_id INTEGER")

    # Same ordering rule as the additive ``tasks`` indexes above: create the
    # index after the additive column migration so legacy ``task_events``
    # tables don't fail during SCHEMA_SQL execution before ``run_id`` exists.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_run "
        "ON task_events(run_id, id)"
    )

    # Exactly-once guard for observed worker completions. One terminal run may
    # produce at most one ``worker_completion_observed`` event on this board and
    # the database enforces it, rather than an application-side ledger that a
    # restart or a second consumer could disagree with. Partial, so every other
    # event kind keeps its normal many-rows-per-run shape.
    #
    # Same ordering rule as ``idx_events_run`` directly above: this must be
    # created after the additive ``run_id`` migration, never from SCHEMA_SQL,
    # or a legacy ``task_events`` table fails to open.
    _create_completion_dedup_index(conn)

    # Executor idempotency. One execution claim per run: the second insert
    # violates this index, which is what makes redelivery unable to execute
    # twice. Same partial-UNIQUE pattern and the same post-``run_id`` ordering
    # rule as the completion guard above; deliberately NOT a separate ledger.
    _create_execution_claim_index(conn)

    notify_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='kanban_notify_subs'"
    ).fetchone() is not None
    if notify_table_exists:
        notify_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")
        }
        if "notifier_profile" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "notifier_profile", "notifier_profile TEXT"
            )
        if "chat_type" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "chat_type", "chat_type TEXT"
            )
        if "delivery_metadata" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "delivery_metadata", "delivery_metadata TEXT"
            )

    # One-shot backfill: any task that is 'running' before runs existed
    # had its claim_lock / claim_expires / worker_pid on the task row.
    # Synthesize a matching task_runs row so subsequent end-run / heartbeat
    # calls have something to write to. Wrapped in write_txn to serialize
    # against any concurrent dispatcher, and the per-row UPDATE uses
    # ``current_run_id IS NULL`` as a CAS guard so a racing claim can't
    # produce an orphaned row if it interleaves with the backfill pass.
    runs_exist = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
    ).fetchone() is not None
    if runs_exist:
        with write_txn(conn):
            inflight = conn.execute(
                "SELECT id, assignee, claim_lock, claim_expires, worker_pid, "
                "       max_runtime_seconds, last_heartbeat_at, started_at "
                "FROM tasks "
                "WHERE status = 'running' AND current_run_id IS NULL"
            ).fetchall()
            for row in inflight:
                started = row["started_at"] or int(time.time())
                cur = conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, profile, status,
                        claim_lock, claim_expires, worker_pid,
                        max_runtime_seconds, last_heartbeat_at,
                        started_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], row["assignee"], row["claim_lock"],
                        row["claim_expires"], row["worker_pid"],
                        row["max_runtime_seconds"], row["last_heartbeat_at"],
                        started,
                    ),
                )
                # CAS: only install the pointer if nothing else claimed
                # the task between our SELECT and here (shouldn't happen
                # under the write_txn, but belt-and-suspenders). If the
                # CAS fails we've got an orphan run_row — mark it
                # reclaimed so it doesn't look in-flight.
                upd = conn.execute(
                    "UPDATE tasks SET current_run_id = ? "
                    "WHERE id = ? AND current_run_id IS NULL",
                    (cur.lastrowid, row["id"]),
                )
                if upd.rowcount != 1:
                    conn.execute(
                        "UPDATE task_runs SET status = 'reclaimed', "
                        "    outcome = 'reclaimed', ended_at = ? "
                        "WHERE id = ?",
                        (int(time.time()), cur.lastrowid),
                    )

    # One-shot event-kind rename pass. The old names ("ready", "priority",
    # "spawn_auto_blocked") still worked but were awkward on the wire;
    # rename them in-place so existing DBs migrate cleanly. Fires once
    # per DB because after the UPDATE no rows match the old kinds.
    _EVENT_RENAMES = (
        # (old, new)
        ("ready",              "promoted"),
        ("priority",           "reprioritized"),
        ("spawn_auto_blocked", "gave_up"),
    )
    for old, new in _EVENT_RENAMES:
        conn.execute(
            "UPDATE task_events SET kind = ? WHERE kind = ?",
            (new, old),
        )

    _rebuild_drifted_tables(conn)

    # Worker-session linkage for the control loop. MUST be added after
    # ``_rebuild_drifted_tables`` above: a rebuild recreates ``task_runs`` from
    # the canonical spec in ``_REBUILD_SPECS``, which would drop any column
    # added earlier in this same pass.
    #
    # ``tasks.session_id`` is the session that *created* the task. It is NOT
    # the worker session, and using it as a resume target would hand work to
    # the wrong session. This column records the session a dispatched worker
    # actually ran in, which is the only sound target for a ``continue`` route.
    # Nothing in this slice populates it — the dispatcher spawn path is the
    # producer, and that is a separate change.
    # Guarded on table existence, like the ``kanban_notify_subs`` pass above:
    # this function is also called directly against partial schemas, where an
    # unguarded ALTER raises "no such table".
    run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
    if run_cols and "worker_session_id" not in run_cols:
        _add_column_if_missing(
            conn, "task_runs", "worker_session_id", "worker_session_id TEXT"
        )
    # Provenance for that mapping. A session id alone does not say whether it
    # is trustworthy enough to hand work back to; the source does. Same
    # post-rebuild placement and existence guard as above.
    if run_cols and "worker_session_source" not in run_cols:
        _add_column_if_missing(
            conn, "task_runs", "worker_session_source", "worker_session_source TEXT"
        )


# Legacy DBs defined these tables with a ``TEXT PRIMARY KEY`` id (or, for
# ``kanban_notify_subs``, a nullable ``TEXT last_event_id``). The current
# schema uses ``INTEGER PRIMARY KEY AUTOINCREMENT`` / ``INTEGER NOT NULL
# DEFAULT 0``. ``CREATE TABLE IF NOT EXISTS`` skips existing tables
# regardless of schema and ``_add_column_if_missing`` only adds columns, so
# neither can fix a drifted column type — the table must be rebuilt. See
# #35096.
#
# Each entry pairs the canonical CREATE TABLE with the CREATE INDEX
# statements that DROP TABLE would otherwise take down with it (including
# ``idx_events_run``, added by the additive pass above). To guard against
# this list drifting from SCHEMA_SQL, ``test_rebuilt_schema_matches_fresh``
# asserts a rebuilt legacy DB is byte-identical to a fresh one.
_REBUILD_SPECS = {
    "task_events": (
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,"
        " payload TEXT, created_at INTEGER NOT NULL)",
        (
            "CREATE INDEX idx_events_task ON task_events(task_id, created_at)",
            "CREATE INDEX idx_events_run ON task_events(run_id, id)",
            "CREATE UNIQUE INDEX idx_events_completion_once "
            "ON task_events(run_id, kind) "
            "WHERE kind = 'worker_completion_observed'",
            "CREATE UNIQUE INDEX idx_events_execution_claim_once "
            "ON task_events(run_id, kind) "
            "WHERE kind = 'session_execution_claimed'",
        ),
    ),
    "task_comments": (
        "CREATE TABLE task_comments ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,"
        " created_at INTEGER NOT NULL)",
        ("CREATE INDEX idx_comments_task ON task_comments(task_id, created_at)",),
    ),
    "task_runs": (
        "CREATE TABLE task_runs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, profile TEXT, step_key TEXT,"
        " status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER,"
        " worker_pid INTEGER, max_runtime_seconds INTEGER,"
        " last_heartbeat_at INTEGER, started_at INTEGER NOT NULL,"
        " ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT,"
        " error TEXT)",
        (
            "CREATE INDEX idx_runs_task ON task_runs(task_id, started_at)",
            "CREATE INDEX idx_runs_status ON task_runs(status)",
        ),
    ),
    "kanban_notify_subs": (
        "CREATE TABLE kanban_notify_subs ("
        " task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,"
        " chat_type TEXT, thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,"
        " notifier_profile TEXT, delivery_metadata TEXT, created_at INTEGER NOT NULL,"
        " last_event_id INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (task_id, platform, chat_id, thread_id))",
        ("CREATE INDEX idx_notify_task ON kanban_notify_subs(task_id)",),
    ),
}


def quarantine_duplicate_completion_events(conn: sqlite3.Connection) -> int:
    """Make a board safe for the completion dedup index. Returns rows moved.

    A board that already carries more than one ``worker_completion_observed``
    row for the same ``run_id`` — written by an older or foreign producer —
    would make ``CREATE UNIQUE INDEX`` fail, and because the index is built
    during ``connect()`` that would make the board **unopenable**. Bricking a
    board is a far worse outcome than a missing index.

    The repair is deterministic and lossless: for each duplicated ``run_id``
    keep the **lowest event id** (the earliest observation, which is the one a
    consumer would already have acted on) and re-kind the rest to
    ``worker_completion_observed_duplicate``. Nothing is deleted, and the
    quarantined rows stay queryable under their new kind, so the repair can be
    audited or reversed.

    Rows with ``run_id IS NULL`` are left alone — see
    :func:`_create_completion_dedup_index` for why they are outside the index's
    reach entirely.
    """
    rows = conn.execute(
        "SELECT run_id, MIN(id) AS keep_id, COUNT(*) AS n FROM task_events "
        "WHERE kind = ? AND run_id IS NOT NULL "
        "GROUP BY run_id HAVING n > 1",
        (BROKER_EVENT_WORKER_COMPLETION,),
    ).fetchall()
    if not rows:
        return 0
    moved = 0
    for row in rows:
        cur = conn.execute(
            "UPDATE task_events SET kind = ? "
            "WHERE kind = ? AND run_id = ? AND id != ?",
            (
                BROKER_EVENT_WORKER_COMPLETION_DUPLICATE,
                BROKER_EVENT_WORKER_COMPLETION,
                row["run_id"],
                int(row["keep_id"]),
            ),
        )
        moved += cur.rowcount
    _log.warning(
        "kanban: quarantined %d duplicate %s event(s) across %d run(s) so the "
        "exactly-once index could be created; they are preserved under kind %r",
        moved,
        BROKER_EVENT_WORKER_COMPLETION,
        len(rows),
        BROKER_EVENT_WORKER_COMPLETION_DUPLICATE,
    )
    return moved


def _create_completion_dedup_index(conn: sqlite3.Connection) -> bool:
    """Create the completion dedup index, repairing duplicates first.

    Returns True when the index exists afterwards.

    **Index semantics, stated explicitly.** SQLite treats NULLs as distinct in
    a UNIQUE index, so rows with ``run_id IS NULL`` are *not* constrained by
    it. This slice's writer can never produce one — ``run_id`` is a required
    ``int`` in :func:`validate_broker_event_payload` and
    :func:`record_worker_completion_events` always supplies the run's real id —
    and the consumer reports any NULL-``run_id`` completion row it meets as
    malformed rather than interpreting it. The index constrains real runs; it
    is not a general uniqueness claim over the kind.

    If the index still cannot be created after the repair, the board is left
    **openable without it** and a warning is logged. Dedup then degrades from a
    database guarantee to the writer's ``NOT EXISTS`` guard, which is weaker
    under concurrency — that degradation is logged rather than hidden.
    """
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_completion_once "
            "ON task_events(run_id, kind) "
            "WHERE kind = 'worker_completion_observed'"
        )
        return True
    except sqlite3.IntegrityError:
        pass
    except sqlite3.OperationalError:
        # e.g. a partial-index-unaware SQLite build. Never fatal to opening.
        _log.warning(
            "kanban: could not create %s; this board CANNOT enforce "
            "exactly-once completion folding and is not safe to schedule a "
            "broker consumer against (see broker_health)",
            COMPLETION_DEDUP_INDEX,
        )
        _write_broker_health_marker(conn)
        return False

    quarantine_duplicate_completion_events(conn)
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_completion_once "
            "ON task_events(run_id, kind) "
            "WHERE kind = 'worker_completion_observed'"
        )
        return True
    except sqlite3.Error as exc:
        _log.warning(
            "kanban: %s still unavailable after repair (%s); the board remains "
            "openable but CANNOT enforce exactly-once completion folding and is "
            "not safe to schedule a broker consumer against (see broker_health)",
            COMPLETION_DEDUP_INDEX,
            exc,
        )
        _write_broker_health_marker(conn)
        return False


def _create_execution_claim_index(conn: sqlite3.Connection) -> bool:
    """Create the executor idempotency guard, never bricking the board.

    Same posture as :func:`_create_completion_dedup_index`: if the index cannot
    be built the board still opens, and the degradation is logged rather than
    hidden. A caller that needs the guarantee checks
    :func:`execution_claim_index_present`.
    """
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_execution_claim_once "
            "ON task_events(run_id, kind) "
            "WHERE kind = 'session_execution_claimed'"
        )
        return True
    except sqlite3.Error as exc:
        _log.warning(
            "kanban: could not create %s (%s); executor idempotency cannot be "
            "enforced by the database and execution must not be scheduled",
            EXECUTION_CLAIM_INDEX,
            exc,
        )
        return False


def execution_claim_index_present(conn: sqlite3.Connection) -> bool:
    """Live check for the executor idempotency guard."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name = ?",
        (EXECUTION_CLAIM_INDEX,),
    ).fetchone()
    return row is not None


def _table_has_drifted(conn: sqlite3.Connection, table: str) -> bool:
    """True when ``table`` still carries the legacy (pre-AUTOINCREMENT) shape."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return False  # table absent — nothing to rebuild
    if table == "kanban_notify_subs":
        lei = next((c for c in info if c["name"] == "last_event_id"), None)
        return lei is not None and (lei["type"] or "").upper() != "INTEGER"
    # task_events / task_comments / task_runs: id must be INTEGER and a PK.
    id_col = next((c for c in info if c["name"] == "id"), None)
    if id_col is None:
        return False
    return not ((id_col["type"] or "").upper() == "INTEGER" and id_col["pk"])


def _rebuild_drifted_tables(conn: sqlite3.Connection) -> None:
    """Rebuild any kanban table whose column types drifted from SCHEMA_SQL.

    Old boards crash the gateway notifier (``int(None)`` on a NULL id in
    ``unseen_events_for_sub``) and never match the ``id > cursor`` filter, so
    every kanban notification is silently lost (#35096). Each affected table is
    rebuilt with the standard SQLite pattern — CREATE new → INSERT shared
    columns → DROP old → RENAME — recreating its indexes too (DROP TABLE takes
    them down). The legacy TEXT ids are dropped (they aren't valid integers);
    AUTOINCREMENT assigns fresh ones and ``last_event_id`` cursors reset to 0,
    so the first post-migration tick replays a task's event history once —
    the safe failure mode for a feature that was already fully broken.

    The whole pass runs in one transaction so an interruption can't leave a
    table half-renamed, and under ``connect()``'s init locks so nothing races
    it. Idempotent: a correctly-typed DB skips every table and returns without
    opening a transaction.
    """
    drifted = [t for t in _REBUILD_SPECS if _table_has_drifted(conn, t)]
    if not drifted:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in drifted:
            create_sql, index_sqls = _REBUILD_SPECS[table]
            old_cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({table})")]
            _log.info("kanban migration: rebuilding %s to match current schema", table)
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
            conn.execute(create_sql)
            new_cols = {c["name"] for c in conn.execute(f"PRAGMA table_info({table})")}
            if table == "kanban_notify_subs":
                # Cast the legacy TEXT cursor to INTEGER; NULL / non-numeric → 0.
                shared = [c for c in old_cols if c in new_cols and c != "last_event_id"]
                cols_csv = ", ".join(shared)
                conn.execute(
                    f"INSERT INTO {table} ({cols_csv}, last_event_id) "
                    f"SELECT {cols_csv}, COALESCE(CAST(last_event_id AS INTEGER), 0) "
                    f"FROM {table}_legacy"
                )
            else:
                # Drop the legacy TEXT id; AUTOINCREMENT reassigns it.
                shared = [c for c in old_cols if c in new_cols and c != "id"]
                cols_csv = ", ".join(shared)
                conn.execute(
                    f"INSERT INTO {table} ({cols_csv}) "
                    f"SELECT {cols_csv} FROM {table}_legacy"
                )
            conn.execute(f"DROP TABLE {table}_legacy")
            for index_sql in index_sqls:
                conn.execute(index_sql)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def _check_file_length_invariant(conn: sqlite3.Connection) -> None:
    """Compare SQLite's own page accounting against the file size on disk.

    Raises sqlite3.DatabaseError if the file is shorter than the header claims
    (torn-extend corruption).

    Both sides are read WITHOUT opening the database file. The header side
    comes from ``PRAGMA page_count`` over the existing connection; the on-disk
    side from ``stat()``. An earlier version read the header field with a bare
    ``open(path,"rb")`` -- but ``close()`` cancels every POSIX advisory lock
    this process holds on the file, so that probe silently dropped the locks
    of concurrent writers (and of a running VACUUM) and let other processes
    write into a database a writer still believed it owned. That is the
    documented corruption route in sqlite.org/howtocorrupt.html section 2.2.
    """
    from hermes_cli.sqlite_safe_read import file_length_matches_header

    # In WAL mode a just-committed page can still live in the -wal file, so
    # the main file legitimately lags its page count. Only enforce the
    # invariant under a rollback journal, where every committed page must
    # already be in the main file.
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        journal_mode = str(row[0]).lower() if row and row[0] is not None else ""
    except sqlite3.Error:
        return
    if journal_mode == "wal":
        return

    ok = file_length_matches_header(conn)
    if ok is False:
        raise sqlite3.DatabaseError(
            "torn-extend detected: the database file is shorter than its "
            "header page count claims"
        )


# SQLite's own busy_timeout uses a near-deterministic backoff, so concurrent
# writers re-collide in lockstep under a stampede. A jittered retry on the
# transaction boundary breaks that convoy. Mirrors state.db's _execute_write:
# a fixed 20-150ms jitter band (a 20ms floor prevents a near-zero retry from
# busy-spinning back into the collision). Only BEGIN IMMEDIATE and COMMIT are
# retried -- both are idempotent re-issues that touch no transaction body, so a
# CAS inside write_txn is never replayed. kanban keeps fewer retries than
# state.db (5 vs 15) because its 120s busy_timeout already absorbs most waits;
# the retry is the backstop for the tail SQLite returns BUSY on immediately.
_BUSY_MAX_RETRIES = 5
_BUSY_RETRY_MIN_S = 0.020  # 20ms
_BUSY_RETRY_MAX_S = 0.150  # 150ms


def _is_busy_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in str(exc).lower()
        or "database is busy" in str(exc).lower()
    )


def _execute_boundary_with_retry(conn: sqlite3.Connection, sql: str) -> None:
    for attempt in range(_BUSY_MAX_RETRIES + 1):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or attempt == _BUSY_MAX_RETRIES:
                raise
            time.sleep(random.uniform(_BUSY_RETRY_MIN_S, _BUSY_RETRY_MAX_S))


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction.

    Use for any multi-statement write (creating a task + link, claiming a
    task + recording an event, etc.).  A claim CAS inside this context is
    atomic -- at most one concurrent writer can succeed.

    The explicit ROLLBACK on exception is wrapped in try/except so that
    a SQLite auto-rollback (which leaves no active transaction) does not
    shadow the original exception with a spurious rollback error.
    """
    _assert_not_delegated_child_mutation()
    _execute_boundary_with_retry(conn, "BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # SQLite has already auto-rolled-back the transaction (typical
            # under EIO, lock contention, or corruption). Nothing to undo;
            # do not let this secondary failure shadow the real one.
            pass
        raise
    else:
        try:
            _execute_boundary_with_retry(conn, "COMMIT")
        except Exception:
            # COMMIT exhausted retries with the txn still open; roll back so the
            # connection isn't poisoned for the next BEGIN IMMEDIATE.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        # Post-commit file-length check: header page_count must match actual file pages.
        # A discrepancy means a torn-extend — raise now rather than silently corrupt.
        _check_file_length_invariant(conn)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _new_task_id() -> str:
    """Generate a short, URL-safe task id.

    4 hex bytes = ~4.3B possibilities. At 10k tasks the collision
    probability is ~1.2e-5; at 100k it's ~1.2e-3. Previously we used 2
    hex bytes (65k possibilities) which hit the birthday paradox hard:
    ~5% collision probability at 1k tasks, ~50% at 10k. Callers that
    care about idempotency should pass ``idempotency_key`` to
    :func:`create_task` rather than rely on id uniqueness.
    """
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Task creation / mutation
# ---------------------------------------------------------------------------

def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None,
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
    goal_mode: bool = False,
    goal_max_turns: Optional[int] = None,
    initial_status: str = "running",
    session_id: Optional[str] = None,
    board: Optional[str] = None,
    project_id: Optional[str] = None,
    project_source_task_id: Optional[str] = None,
) -> str:
    """Create a new task and optionally link it under parent tasks.

    Returns the new task id.  Status is ``ready`` when there are no
    parents (or all parents already ``done``), otherwise ``todo``.
    If ``triage=True``, status is forced to ``triage`` regardless of
    parents — a specifier/triager is expected to promote the task to
    ``todo`` once the spec is fleshed out.

    If ``idempotency_key`` is provided and a non-archived task with the
    same key already exists, returns the existing task's id instead of
    creating a duplicate. Useful for retried webhooks / automation that
    should not double-write.

    ``max_runtime_seconds`` caps how long a worker may run before the
    dispatcher SIGTERMs (then SIGKILLs after a grace window) and
    re-queues the task. ``None`` means no cap (default).

    ``skills`` is an optional list of skill names to force-load into
    the worker when dispatched. Stored as JSON; the dispatcher passes
    each name to ``hermes --skills ...``. Use this to pin a task to a
    specialist skill (e.g. ``skills=["translation"]`` so the worker loads the
    translation skill regardless of the profile's default config).

    ``model_override`` / ``provider_override`` pin the worker to a specific
    model (and optionally its provider) without touching the profile's
    config — passed to the worker as ``-m <model> [--provider <name>]``.
    ``provider_override`` requires ``model_override``.

    ``project_source_task_id`` is an internal cross-profile fallback for a
    worker-created child. When the active profile cannot resolve ``project_id``
    in its own projects.db, a matching canonical project-linked task in this
    board can supply the repo and branch convention. Its literal worktree is
    never reused; the new task still gets its own task-id-keyed path.
    """
    model_override = (model_override or "").strip() or None
    provider_override = (provider_override or "").strip() or None
    if provider_override and not model_override:
        raise ValueError("provider_override requires a model_override")
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}"
        )
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")

    # Resolve an optional first-class Project link. A project-linked task is
    # anchored to the project's primary repo as a git worktree, so its branch
    # can be named deterministically (project slug + task id) instead of the
    # random ``wt/<task-id>`` fallback the worker skill applies when no branch
    # is set. Projects live in the creator's per-profile projects.db; the repo
    # path is absolute (profile-independent) and the branch name is pure, so the
    # cross-profile dispatcher needs no projects.db access at dispatch time.
    project_obj = None
    # Primary repo of a project-linked worktree task whose path we still need to
    # derive (a fresh worktree dir under the repo, computed once task_id exists).
    project_repo: Optional[str] = None
    if project_id is not None:
        project_id = str(project_id).strip() or None
    if project_id:
        from hermes_cli import projects_db as _pdb

        try:
            with _pdb.connect_closing() as _pconn:
                project_obj = _pdb.get_project(_pconn, project_id)
        except Exception:
            project_obj = None
        if project_obj is None and project_source_task_id:
            # Worker profiles have their own projects.db, while the Kanban DB is
            # intentionally shared. Recover routing only from a canonical
            # project-linked source task in this same board. This carries the
            # repo + project branch convention forward without copying or
            # opening the creator profile's project store, and without reusing
            # the source task's literal worktree path.
            source_task = get_task(conn, str(project_source_task_id))
            if (
                source_task is not None
                and source_task.project_id == project_id
                and source_task.workspace_kind == "worktree"
                and source_task.workspace_path
            ):
                source_path = Path(source_task.workspace_path)
                if (
                    source_path.is_absolute()
                    and source_path.name == source_task.id
                    and source_path.parent.name == ".worktrees"
                ):
                    project_slug = None
                    if source_task.branch_name:
                        prefix, separator, leaf = source_task.branch_name.partition("/")
                        if separator and (
                            leaf == source_task.id
                            or leaf.startswith(f"{source_task.id}-")
                        ):
                            try:
                                project_slug = _pdb.normalize_slug(prefix)
                            except ValueError:
                                project_slug = None
                    if project_slug is None:
                        try:
                            project_slug = _pdb.normalize_slug(project_id)
                        except ValueError:
                            project_slug = None
                    if project_slug:
                        project_repo = str(source_path.parent.parent)
                        project_obj = _pdb.Project(
                            id=project_id,
                            slug=project_slug,
                            name=project_slug,
                            created_at=0,
                            primary_path=project_repo,
                        )
                        if workspace_kind == "scratch":
                            workspace_kind = "worktree"

        if project_obj is None:
            # A project id/slug that doesn't resolve must not crash task
            # creation or persist a dangling reference — drop the link and
            # create the task as an ordinary (scratch) task.
            project_id = None
        else:
            # Canonicalise (a slug may have been passed) and anchor the
            # worktree under the project's primary repo.
            project_id = project_obj.id
            if workspace_kind == "scratch" and project_obj.primary_path:
                workspace_kind = "worktree"
            if (
                workspace_kind == "worktree"
                and workspace_path is None
                and project_obj.primary_path
            ):
                # Defer the concrete path to the insert loop: it's a fresh
                # ``<repo>/.worktrees/<task-id>`` dir keyed on the new task id.
                project_repo = str(project_obj.primary_path)

    parents = tuple(p for p in parents if p)

    # Normalise + validate skills: strip whitespace, drop empties, dedupe
    # (preserving order). Refuse commas inside a single name so we don't
    # invisibly splatter a comma-joined string into one argv slot — the
    # `hermes --skills X,Y` comma syntax is handled in the dispatcher,
    # not here.
    skills_list: Optional[list[str]] = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        # Collect all toolset-name confusions up front so the user sees the
        # whole list at once. Raising on the first hit is friendly when the
        # input has one mistake, but agents that confuse skills with toolsets
        # usually pass several at once (`skills=["web", "browser", "terminal"]`)
        # and serial-correcting one per failure round-trips wastes tokens.
        toolset_typos: list[str] = []
        for s in skills:
            if not s:
                continue
            name = str(s).strip()
            if not name:
                continue
            if "," in name:
                raise ValueError(
                    f"skill name cannot contain comma: {name!r} "
                    f"(pass a list of separate names instead of a comma-joined string)"
                )
            if name.casefold() in KNOWN_TOOLSET_NAMES:
                toolset_typos.append(name)
                continue
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        if toolset_typos:
            quoted = ", ".join(repr(n) for n in toolset_typos)
            noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
            raise ValueError(
                f"{quoted} {noun}, not skill name(s). "
                "Put toolsets in the assignee profile's `toolsets:` config "
                "instead of per-task skills. Skills are named skill bundles "
                "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
                "capabilities (e.g. `web`, `browser`, `terminal`)."
            )
        skills_list = cleaned

    # Idempotency check — return the existing task instead of creating a
    # duplicate. Done BEFORE entering write_txn to keep the fast path fast
    # and to avoid holding a write lock during the lookup. Race is
    # acceptable: two concurrent creators with the same key might both
    # insert, at which point both rows exist but the next lookup stabilises.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]

    service_gate_source = _parse_service_gate_source_and_family(title, body)
    if service_gate_source is not None:
        source_task_id, gate_family, candidate_text = service_gate_source
        decision = service_gate_dedupe_decision(
            conn,
            source_task_id=source_task_id,
            gate_family=gate_family,
            candidate_text=candidate_text,
        )
        if not decision["create_escalation"]:
            add_comment(
                conn,
                source_task_id,
                created_by or "kanban-service-gate",
                decision["pointer_comment"],
            )
            return decision["active_lane"]["task_id"]

    now = int(time.time())

    # Resolve workspace_path from board-level default_workdir when the
    # caller did not specify one explicitly. Board defaults represent
    # persistent project checkouts, so only persistent workspace kinds may
    # inherit them. Scratch workspaces are auto-deleted on completion and
    # must stay under the per-board scratch root created by
    # ``resolve_workspace``; inheriting ``default_workdir`` for a scratch
    # task would point cleanup at the user's source tree (#28818). The
    # containment guard in ``_cleanup_workspace`` is the safety rail, but
    # we also stop the bad state from being created in the first place.
    if (
        workspace_path is None
        and project_repo is None
        and workspace_kind in {"dir", "worktree"}
    ):
        board_slug = board if board else get_current_board()
        board_meta = read_board_metadata(board_slug)
        board_default = board_meta.get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            with write_txn(conn):
                # Determine task status from parent status, unless the caller
                # parks it directly in blocked for human-ops review or in
                # triage for a specifier.
                if initial_status == "blocked":
                    task_status = "blocked"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                elif triage:
                    task_status = "triage"
                else:
                    task_status = "ready"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                        # If any parent is not yet done, we're todo.
                        rows = conn.execute(
                            "SELECT status FROM tasks WHERE id IN "
                            "(" + ",".join("?" * len(parents)) + ")",
                            parents,
                        ).fetchall()
                        if any(r["status"] != "done" for r in rows):
                            task_status = "todo"
                # Even in triage mode we still need to validate parent ids
                # so the eventual link rows don't dangle.
                if triage and parents:
                    missing = _find_missing_parents(conn, parents)
                    if missing:
                        raise ValueError(f"unknown parent task(s): {', '.join(missing)}")

                # Project-linked worktree: a fresh worktree dir under the repo
                # plus a deterministic branch (project slug + task id). Together
                # these kill the random ``wt/<task-id>`` worker fallback and the
                # unanchored ``.worktrees/<id>`` under the dispatcher's cwd.
                if project_obj is not None and workspace_kind == "worktree":
                    if project_repo and not workspace_path:
                        workspace_path = os.path.join(
                            project_repo, ".worktrees", task_id
                        )
                    if not branch_name:
                        # _pdb was imported above when project_obj was resolved.
                        try:
                            branch_name = _pdb.branch_name_for(
                                project_obj, task_id, title=title or ""
                            )
                        except Exception:
                            branch_name = None

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        max_runtime_seconds,
                        skills, max_retries, model_override, provider_override,
                        goal_mode, goal_max_turns, session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title.strip(),
                        body,
                        assignee,
                        task_status,
                        priority,
                        created_by,
                        now,
                        workspace_kind,
                        workspace_path,
                        branch_name,
                        project_id,
                        tenant,
                        idempotency_key,
                        int(max_runtime_seconds) if max_runtime_seconds is not None else None,
                        json.dumps(skills_list) if skills_list is not None else None,
                        int(max_retries) if max_retries is not None else None,
                        model_override,
                        provider_override,
                        1 if goal_mode else 0,
                        int(goal_max_turns) if goal_max_turns is not None else None,
                        session_id,
                    ),
                )
                for pid in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (pid, task_id),
                    )
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": task_status,
                        "parents": list(parents),
                        "tenant": tenant,
                        "workspace_kind": workspace_kind,
                        "workspace_path": workspace_path,
                        "branch_name": branch_name,
                        "project_id": project_id,
                        "skills": list(skills_list) if skills_list else None,
                        "goal_mode": bool(goal_mode) or None,
                        "model_override": model_override,
                        "provider_override": provider_override,
                    },
                )
                # When the task was created with ``initial_status='blocked'``,
                # emit a ``blocked`` event so ``_has_sticky_block()``
                # recognises it as sticky — without this the dispatcher's
                # ``recompute_ready`` would promote it to ``ready`` on the
                # next tick (t_fc1fdf31).
                if initial_status == "blocked":
                    _append_event(
                        conn, task_id, "blocked",
                        {"reason": "initial_status"},
                    )
                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
            # Retry with a fresh id.
            continue
    raise RuntimeError("unreachable")


def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        parents,
    ).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in parents if p not in present]


def _inherit_notify_subs(
    conn: sqlite3.Connection,
    child_id: str,
    parents: Iterable[str],
    *,
    created_at: Optional[int] = None,
) -> None:
    """Copy gateway notification subscriptions from parent tasks to a child.

    The inherited subscription starts caught up to the child's current event
    cursor. This makes manual `link_tasks(parent, existing_child)` safe: the
    parent chat receives future child terminal events without replaying the
    child's pre-link history.
    """
    parent_ids = tuple(dict.fromkeys(p for p in parents if p))
    if not parent_ids:
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?",
        (child_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    placeholders = ",".join("?" * len(parent_ids))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id,
             notifier_profile, created_at, last_event_id)
        SELECT ?, platform, chat_id, thread_id, user_id, notifier_profile, ?, ?
          FROM kanban_notify_subs
         WHERE task_id IN ({placeholders})
        """,
        (
            child_id,
            int(created_at if created_at is not None else time.time()),
            cursor,
            *parent_ids,
        ),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
}


def list_tasks(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    include_archived: bool = False,
    limit: Optional[int] = None,
    order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
) -> list[Task]:
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if assignee is not None:
        query += " AND assignee = ?"
        params.append(_canonical_assignee(assignee))
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        query += " AND status = ?"
        params.append(status)
    if tenant is not None:
        query += " AND tenant = ?"
        params.append(tenant)
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    if workflow_template_id is not None:
        query += " AND workflow_template_id = ?"
        params.append(workflow_template_id)
    if current_step_key is not None:
        query += " AND current_step_key = ?"
        params.append(current_step_key)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(
                f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}"
            )
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign or reassign a task.  Returns True on success.

    Refuses to reassign a task that's currently running (claim_lock set).
    Reassign after the current run completes if needed.
    """
    profile = _canonical_assignee(profile)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        if row["assignee"] != profile:
            # The retry guard is scoped to the task/profile combination. A
            # human reassigning the task is an explicit recovery action, so the
            # new profile should not inherit the previous profile's streak.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?",
                (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        _append_event(conn, task_id, "assigned", {"assignee": profile})
        return True


def set_model_override(
    conn: sqlite3.Connection,
    task_id: str,
    model: Optional[str],
    provider: Optional[str] = None,
) -> bool:
    """Set (or clear) the per-task model/provider override.

    ``model=None`` (or empty) clears BOTH overrides — the worker falls back
    to its profile's configured model. ``provider`` without ``model`` is
    rejected: a bare provider switch has no defined meaning for the worker
    spawn (``--provider`` alone would re-resolve the profile's model name
    against a different backend, which is exactly the mismatch class this
    feature exists to kill).

    Allowed on any non-archived task, including ``running`` ones — the
    override only takes effect on the NEXT dispatch, so setting it on a
    running task that's about to be reclaimed/retried is the primary
    rate-limit-recovery flow. Returns True on success.
    """
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    if not model:
        provider = None
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["status"] == "archived":
            raise RuntimeError(f"cannot set model override on archived task {task_id}")
        conn.execute(
            "UPDATE tasks SET model_override = ?, provider_override = ? WHERE id = ?",
            (model, provider, task_id),
        )
        _append_event(
            conn, task_id, "model_override_set",
            {"model": model, "provider": provider},
        )
        return True


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def link_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    with write_txn(conn):
        missing = _find_missing_parents(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(
                f"linking {parent_id} -> {child_id} would create a cycle"
            )
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
        # If child was ready but parent is not yet done, demote child to todo.
        parent_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (parent_id,)
        ).fetchone()["status"]
        if parent_status != "done":
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
        _append_event(
            conn, child_id, "linked",
            {"parent": parent_id, "child": child_id},
        )
        _inherit_notify_subs(conn, child_id, (parent_id,))


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """Return True if adding parent->child creates a cycle.

    A cycle exists iff ``parent_id`` is already a descendant of
    ``child_id`` via existing parent->child links.  We walk downward
    from ``child_id`` and check whether we reach ``parent_id``.
    """
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        )
        if cur.rowcount:
            _append_event(
                conn, child_id, "unlinked",
                {"parent": parent_id, "child": child_id},
            )
        removed = cur.rowcount > 0
    if removed:
        # Dependency edge removed — re-evaluate promotion eligibility for the
        # child immediately.  Matches the contract of complete_task and
        # unblock_task; without this the child stays stuck in todo until the
        # next dispatcher tick or a manual `hermes kanban recompute` (issue #22459).
        recompute_ready(conn)
    return removed


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] for r in rows]


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]


def parent_results(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, Optional[str]]]:
    """Return ``(parent_id, result)`` for every done parent of ``task_id``."""
    rows = conn.execute(
        """
        SELECT t.id AS id, t.result AS result
        FROM tasks t
        JOIN task_links l ON l.parent_id = t.id
        WHERE l.child_id = ? AND t.status = 'done'
        ORDER BY t.completed_at ASC
        """,
        (task_id,),
    ).fetchall()
    return [(r["id"], r["result"]) for r in rows]


# ---------------------------------------------------------------------------
# Comments & events
# ---------------------------------------------------------------------------

def add_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str
) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, author.strip(), body.strip(), now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    rows = conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [
        Comment(
            id=r["id"],
            task_id=r["task_id"],
            author=r["author"],
            body=r["body"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def _task_search_text(task: Task, comments: Iterable[Comment] = ()) -> str:
    parts = [task.id, task.title or "", task.body or "", task.assignee or "", task.block_kind or ""]
    parts.extend(c.body or "" for c in comments)
    return "\n".join(parts).casefold()


def _task_is_review_lane(task: Task) -> bool:
    assignee = (task.assignee or "").casefold()
    title_body = f"{task.title or ''}\n{task.body or ''}".casefold()
    return (
        any(marker in assignee for marker in REVIEW_LANE_ASSIGNEE_MARKERS)
        and any(marker in title_body for marker in REVIEW_LANE_TEXT_MARKERS)
    ) or (
        (task.title or "").strip().casefold().startswith("review")
        and any(marker in title_body for marker in REVIEW_LANE_TEXT_MARKERS)
    )


def _task_is_review_required_source(
    conn: sqlite3.Connection,
    task: Task,
    comments: Iterable[Comment],
) -> bool:
    if task.status != "blocked":
        return False
    text = _task_search_text(task, comments)
    event_rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN "
        "('blocked', 'dependency_wait', 'block_loop_detected')",
        (task.id,),
    ).fetchall()
    for row in event_rows:
        text += "\n" + (row["payload"] or "")
    for run in list_runs(conn, task.id):
        text += "\n" + (run.summary or "")
        text += "\n" + (run.error or "")
    text = text.casefold()
    return (
        "review-required" in text
        or "review required" in text
        or "review_verdict" in text
        or "guardian review" in text
        or "os-reviewer" in text
    )


def review_lane_dependency_warning(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """Return a warning payload for reviewer children parented to blocked sources.

    The kernel must continue rejecting tasks with unfinished parents.  This
    helper is read-only advisory logic for dashboards/dry-runs: when a
    reviewer-looking task's only unfinished parent is the blocked source named
    in its own review body, the graph likely inverted the review dependency.
    Implementation/landing/remediation children are intentionally ignored.
    """
    task = get_task(conn, task_id)
    if task is None or not _task_is_review_lane(task):
        return None
    parents = parent_ids(conn, task_id)
    if not parents:
        return None
    unfinished: list[Task] = []
    for parent_id in parents:
        parent = get_task(conn, parent_id)
        if parent is not None and parent.status not in ("done", "archived"):
            unfinished.append(parent)
    if len(unfinished) != 1:
        return None
    source = unfinished[0]
    source_comments = list_comments(conn, source.id)
    if not _task_is_review_required_source(conn, source, source_comments):
        return None
    task_text = _task_search_text(task)
    if source.id not in task_text and "source" not in task_text:
        return None
    return {
        "source_task_id": source.id,
        "source_status": source.status,
        "source_assignee": source.assignee,
        "message": (
            "review task is parented to the blocked review-required source it "
            "is meant to inspect; create an independent reviewer lane or remove "
            "the inverted parent edge after checking for duplicate reviews"
        ),
    }


def review_lane_dependency_warnings(
    conn: sqlite3.Connection, task_ids: Optional[Iterable[str]] = None,
) -> dict[str, dict]:
    ids = list(task_ids) if task_ids is not None else [
        r["id"] for r in conn.execute("SELECT id FROM tasks WHERE status != 'archived'")
    ]
    out: dict[str, dict] = {}
    for task_id in ids:
        warning = review_lane_dependency_warning(conn, task_id)
        if warning:
            out[task_id] = warning
    return out


def _contains_any_marker(text: str, markers: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(marker in folded for marker in markers)


def _gate_family_matches(text: str, gate_family: str) -> bool:
    family = (gate_family or "").strip().casefold()
    if not family:
        return True
    folded = text.casefold()
    tokens = {family, family.replace("_", "-"), family.replace("-", "_")}
    return any(
        token and re.search(rf"(?<![a-z0-9_-]){re.escape(token)}(?![a-z0-9_-])", folded)
        for token in tokens
    )


def _parse_service_gate_source_and_family(
    title: Optional[str], body: Optional[str]
) -> Optional[tuple[str, str, str]]:
    """Extract source task and gate family from a service-gate task candidate."""
    candidate_text = f"{title or ''}\n{body or ''}".strip()
    if not candidate_text or "service-gate" not in candidate_text.casefold():
        return None

    source_match = re.search(
        r"\b(?:source|source_task|source_task_id)\s*[:=]\s*(t_[0-9a-fA-F]+)\b",
        candidate_text,
    )
    if source_match is None:
        source_match = re.search(r"\bsource\s+(t_[0-9a-fA-F]+)\b", candidate_text, re.I)
    family_match = re.search(
        r"\bSERVICE-GATE\s+([a-zA-Z0-9][a-zA-Z0-9_-]*)\b",
        candidate_text,
        re.I,
    )
    if source_match is None or family_match is None:
        return None
    return source_match.group(1), family_match.group(1), candidate_text


def source_has_true_critical_marker(
    conn: sqlite3.Connection,
    source_task_id: str,
    *,
    extra_text: str = "",
) -> bool:
    source = get_task(conn, source_task_id)
    if source is None:
        raise ValueError(f"unknown source task {source_task_id}")
    text = _task_search_text(source, list_comments(conn, source_task_id)) + "\n" + (extra_text or "")
    return _contains_any_marker(text, TRUE_CRITICAL_LIST_MARKERS)


def find_active_service_gate_lane(
    conn: sqlite3.Connection,
    *,
    source_task_id: str,
    gate_family: str,
    include_terminal_approval: bool = False,
    require_approval_packet: bool = False,
) -> Optional[dict]:
    """Find an existing active lane for ``source_task_id`` + gate family.

    This is intentionally conservative: a lane must mention the exact source id
    and the requested gate family in title/body/comments.  It does not infer from
    assignee or age, which prevents unrelated critical blockers from being
    hidden behind a broad de-dup match.
    """
    statuses = SERVICE_GATE_APPROVAL_STATUSES if include_terminal_approval else ACTIVE_SERVICE_GATE_STATUSES
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status != 'archived' AND id != ? ORDER BY created_at DESC, id DESC",
        (source_task_id,),
    ).fetchall()
    for row in rows:
        task = Task.from_row(row)
        if task.status not in statuses:
            continue
        comments = list_comments(conn, task.id)
        text = _task_search_text(task, comments)
        if source_task_id not in text:
            continue
        if not _gate_family_matches(text, gate_family):
            continue
        if require_approval_packet and not (
            "approval packet" in text
            or "approval-packet" in text
            or "operator approval" in text
        ):
            continue
        return {
            "task_id": task.id,
            "status": task.status,
            "assignee": task.assignee,
            "title": task.title,
        }
    return None


def service_gate_dedupe_decision(
    conn: sqlite3.Connection,
    *,
    source_task_id: str,
    gate_family: str,
    candidate_text: str = "",
) -> dict:
    """Plan whether a service-gate scan should create another escalation.

    Returns a small, serialisable decision packet.  ``create_escalation=False``
    means callers should add ``pointer_comment`` to the source instead of
    creating a duplicate card.  True critical-list blockers are never suppressed
    silently: if no matching approval packet exists, the action is
    ``create_approval_packet`` rather than generic de-dup suppression.
    """
    source = get_task(conn, source_task_id)
    if source is None:
        raise ValueError(f"unknown source task {source_task_id}")

    critical = source_has_true_critical_marker(
        conn, source_task_id, extra_text=candidate_text,
    )
    active = find_active_service_gate_lane(
        conn,
        source_task_id=source_task_id,
        gate_family=gate_family,
        include_terminal_approval=critical,
        require_approval_packet=critical,
    )
    if active:
        decision = "hold_for_existing_approval_packet" if critical else "dedupe_to_active_lane"
        pointer = (
            f"delegated: SERVICE-GATE-DEDUPE source={source_task_id} "
            f"active_lane={active['task_id']} lane_status={active['status']} "
            f"owner={active.get('assignee') or '-'} decision=watch "
            f"next_evidence=follow existing {gate_family} lane "
            "no_duplicate_escalation=true"
        )
        return {
            "create_escalation": False,
            "decision": decision,
            "critical_list_blocker": critical,
            "active_lane": active,
            "pointer_comment": pointer,
        }

    if critical:
        return {
            "create_escalation": True,
            "decision": "create_approval_packet",
            "critical_list_blocker": True,
            "active_lane": None,
            "pointer_comment": None,
        }

    return {
        "create_escalation": True,
        "decision": "create_triage_or_service_gate_lane",
        "critical_list_blocker": False,
        "active_lane": None,
        "pointer_comment": None,
    }


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

# The attachment size cap is the module-level ``KANBAN_ATTACHMENT_MAX_BYTES``
# (defined near the top of this file) — one constant shared by the dashboard
# HTTP endpoint, the agent toolset, and the CLI so the limit cannot drift
# between surfaces.


class AttachmentTooLarge(ValueError):
    """Raised when an attachment exceeds the configured size cap.

    Subclasses :class:`ValueError` so generic ``except ValueError`` handlers
    (e.g. the dashboard's 400 fallback) still catch it, while callers that
    want a distinct user-facing message (the tool/CLI 413-equivalent) can
    catch it specifically.
    """


def _safe_attachment_name(raw: str) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Strips any directory components (both separators) so a malicious
    ``../../etc/passwd`` or ``C:\\x`` collapses to its leaf. Drops control
    chars and leading dots so we never write a dotfile or a name with
    embedded NULs/newlines. Rejects empty / dotfile-only names. The result
    is only ever joined under the per-task attachments dir, never used
    verbatim as a path from the client.

    Raises :class:`ValueError` on an unusable name; HTTP callers map that
    to a 400.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        raise ValueError("invalid attachment filename")
    return name[:200]


def _collision_free_path(dest_dir: Path, safe_name: str) -> Path:
    """Return a path under ``dest_dir`` that doesn't clobber an existing file.

    ``foo.pdf`` → ``foo.pdf``, then ``foo (1).pdf``, ``foo (2).pdf``, …
    ``safe_name`` must already be sanitised via :func:`_safe_attachment_name`.
    """
    stem, dot, ext = safe_name.partition(".")
    candidate = safe_name
    n = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return dest_dir / candidate


def store_attachment_bytes(
    conn: sqlite3.Connection,
    task_id: str,
    filename: str,
    data: bytes,
    *,
    content_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    board: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> int:
    """Validate, size-check, persist a blob, and record its metadata row.

    This is the single write path shared by the dashboard endpoint, the
    agent toolset (``kanban_attach`` / ``kanban_attach_url``), and the CLI
    (``hermes kanban attach``) so name-sanitisation, the size cap, and the
    collision-resolution all behave identically everywhere.

    Steps: enforce ``max_bytes``, sanitise ``filename`` to a safe basename,
    write the bytes under :func:`task_attachments_dir` with a
    collision-free name, then insert the ``task_attachments`` row via
    :func:`add_attachment`. Returns the new attachment id.

    Raises :class:`AttachmentTooLarge` when ``data`` exceeds ``max_bytes``,
    or :class:`ValueError` for a bad filename / unknown task. On any failure
    after the blob is written (e.g. the task disappeared) the orphaned blob
    is removed before re-raising.
    """
    if max_bytes is None:
        max_bytes = KANBAN_ATTACHMENT_MAX_BYTES
    if len(data) > max_bytes:
        raise AttachmentTooLarge(
            f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
        )
    safe_name = _safe_attachment_name(filename)
    dest_dir = task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _collision_free_path(dest_dir, safe_name)
    dest_path.write_bytes(data)
    try:
        return add_attachment(
            conn,
            task_id,
            filename=dest_path.name,
            stored_path=str(dest_path.resolve()),
            content_type=content_type,
            size=len(data),
            uploaded_by=uploaded_by,
        )
    except Exception:
        # Don't leave an orphan blob if the metadata insert fails (most
        # commonly: the task id doesn't exist).
        try:
            dest_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def add_attachment(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    filename: str,
    stored_path: str,
    content_type: Optional[str] = None,
    size: int = 0,
    uploaded_by: Optional[str] = None,
) -> int:
    """Record a file attachment for a task. Returns the new attachment id.

    The caller is responsible for writing the blob to ``stored_path``
    first (under :func:`task_attachments_dir`); this only persists the
    metadata row and appends an ``attached`` event.
    """
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                filename.strip(),
                stored_path,
                content_type,
                int(size),
                uploaded_by,
                now,
            ),
        )
        _append_event(
            conn,
            task_id,
            "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    rows = conn.execute(
        "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    return [
        Attachment(
            id=r["id"],
            task_id=r["task_id"],
            filename=r["filename"],
            stored_path=r["stored_path"],
            content_type=r["content_type"],
            size=r["size"] or 0,
            uploaded_by=r["uploaded_by"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute(
        "SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if r is None:
        return None
    return Attachment(
        id=r["id"],
        task_id=r["task_id"],
        filename=r["filename"],
        stored_path=r["stored_path"],
        content_type=r["content_type"],
        size=r["size"] or 0,
        uploaded_by=r["uploaded_by"],
        created_at=r["created_at"],
    )


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete an attachment row and its on-disk blob. Returns the removed row.

    Returns ``None`` when no row matched. The blob is removed best-effort
    (a missing file is not an error); the metadata row is the source of
    truth for whether an attachment "exists".
    """
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        _append_event(
            conn, att.task_id, "attachment_removed", {"filename": att.filename}
        )
    try:
        p = Path(att.stored_path)
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    return att


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            Event(
                id=r["id"],
                task_id=r["task_id"],
                kind=r["kind"],
                payload=payload,
                created_at=r["created_at"],
                run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
            )
        )
    return out


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: Optional[dict] = None,
    *,
    run_id: Optional[int] = None,
) -> None:
    """Record an event row.  Called from within an already-open txn.

    ``run_id`` is optional: pass the current run id so UIs can group
    events by attempt. For events that aren't scoped to a single run
    (task created/edited/archived, dependency promotion) leave it None
    and the row carries NULL.
    """
    now = int(time.time())
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, pl, now),
    )


def _end_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
) -> Optional[int]:
    """Close the currently-active run for ``task_id`` and clear the pointer.

    ``outcome`` is the semantic result (completed / blocked / crashed /
    timed_out / spawn_failed / gave_up / reclaimed). ``status`` is the
    run-row status (usually just ``outcome``, but callers can pass it
    explicitly). Returns the closed run_id or ``None`` if no active run
    existed (e.g. a CLI user calling ``hermes kanban complete`` on a
    task that was never claimed).
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row or not row["current_run_id"]:
        return None
    run_id = int(row["current_run_id"])
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_lock    = NULL,
               claim_expires = NULL,
               worker_pid    = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (
            status or outcome,
            outcome,
            summary,
            error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now,
            run_id,
        ),
    )
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,),
    )
    return run_id


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


def _synthesize_ended_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    """Insert a zero-duration, already-closed run row.

    Used when a terminal transition happens on a task that was never
    claimed (CLI user calling ``hermes kanban complete <ready-task>
    --summary X``, or dashboard "mark done" on a ready task). Without
    this, the handoff fields (summary / metadata / error) would be
    silently dropped: ``_end_run`` is a no-op because there's no
    current run.

    The synthetic run has ``started_at == ended_at == now`` so it
    shows up in attempt history as "instant" and doesn't skew elapsed
    stats. Caller is responsible for leaving ``current_run_id`` NULL
    (or for clearing it elsewhere in the same txn) since this
    function does NOT touch the tasks row.
    """
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key,
            outcome, outcome,
            summary, error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Dependency resolution (todo -> ready)
# ---------------------------------------------------------------------------

def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True when ``task_id`` is sticky-blocked by an explicit
    worker/operator ``kanban_block`` call (#28712).

    A ``blocked`` status can come from two very different sources:

    * **Worker- or operator-initiated** — a worker called
      ``kanban_block(reason="review-required: ...")`` (or somebody ran
      ``hermes kanban block <id>``).  This is a deliberate handoff that
      should stay blocked until an operator unblocks it.  The block tool
      emits a ``"blocked"`` event row in ``task_events``.

    * **Circuit-breaker** — ``_record_task_failure`` tripped after
      repeated crashes / spawn failures / timeouts.  This emits
      ``"gave_up"``, *not* ``"blocked"``, and is meant to recover
      automatically once the underlying conditions change (e.g. parents
      finish, transient infra error clears).

    The cheapest signal that distinguishes the two is the most recent
    ``"blocked"`` / ``"unblocked"`` event for the task.  If the most
    recent one is ``"blocked"`` (or there is a ``"blocked"`` event and
    no ``"unblocked"`` event has fired since), the task is sticky and
    ``recompute_ready`` must *not* auto-promote it.

    Returns ``False`` when there is no such event at all (e.g. the task
    was set to ``status='blocked'`` by the circuit breaker or by direct
    DB manipulation) — preserves the pre-#28712 auto-recover semantics
    for that path.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] == "blocked"


def recompute_ready(
    conn: sqlite3.Connection, failure_limit: int = None,
) -> int:
    """Promote ``todo`` tasks to ``ready`` when all parents are ``done`` or ``archived``.

    Returns the number of tasks promoted.  Safe to call inside or outside
    an existing transaction; it opens its own IMMEDIATE txn.

    ``blocked`` tasks are also considered for promotion (so a task
    blocked purely by a parent dependency unblocks itself when the
    parent completes), *except* in two cases:

    1. The most recent block event was a worker-initiated
       ``kanban_block`` — those stay blocked until an explicit
       ``kanban_unblock`` (#28712).

    2. The task's ``consecutive_failures`` has reached the effective
       failure limit.  This prevents infinite retry loops when a task
       repeatedly exhausts its iteration budget: without this guard the
       counter would reset on every recovery cycle and the circuit
       breaker could never trip (#35072).

    The effective failure limit resolves in the same order as the
    circuit breaker in ``_record_task_failure`` so the two never
    disagree about when a task is permanently blocked:

      1. per-task ``max_retries`` if set
      2. caller-supplied ``failure_limit`` (the dispatcher passes the
         ``kanban.failure_limit`` config value through ``dispatch_once``)
      3. ``DEFAULT_FAILURE_LIMIT``
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id, status, consecutive_failures, max_retries, block_kind "
            "FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            row_block_kind = row["block_kind"] if "block_kind" in row.keys() else None
            if cur_status == "blocked":
                if _has_sticky_block(conn, task_id):
                    # Worker / operator asked for human review — do not
                    # silently auto-recover.  ``unblock_task`` is the only
                    # legitimate exit (it emits ``"unblocked"`` which flips
                    # this predicate back).
                    continue
                # Blind-spot guard (t_6009ccaa): status='blocked' with no
                # 'blocked' event row (direct status write / verdict-router
                # hold / approval-hold hook without a blocked event). Must
                # NOT be silently auto-promoted to 'ready' when parents are
                # done. The circuit-breaker case (failures ≥ limit set by
                # _record_task_failure) is also caught here and stays blocked,
                # preserving the existing fall-through behaviour at lines
                # 4062-4069 (both paths agree: stay blocked).
                continue
            parents = conn.execute(
                "SELECT t.status FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id "
                "WHERE l.child_id = ?",
                (task_id,),
            ).fetchall()
            if all(p["status"] in ("done", "archived") for p in parents):
                if cur_status == "blocked":
                    # Don't auto-recover tasks that have hit the
                    # circuit-breaker failure limit.  Without this
                    # guard, a task that repeatedly exhausts its
                    # iteration budget would cycle forever:
                    # block → auto-recover → respawn → budget
                    # exhausted → block → …  The counter must also
                    # be preserved so the breaker can accumulate
                    # across recovery cycles.
                    failures = int(row["consecutive_failures"] or 0)
                    task_limit = row["max_retries"]
                    effective_limit = (
                        int(task_limit) if task_limit is not None
                        else int(failure_limit)
                    )
                    if failures >= effective_limit:
                        continue
                    conn.execute(
                        "UPDATE tasks SET status = 'ready' "
                        "WHERE id = ? AND status = 'blocked'",
                        (task_id,),
                    )
                else:
                    # ``todo`` rows whose block_kind is NOT ``dependency``
                    # got there via a non-dependency routing path (triage
                    # reset, approval-auto-clear, direct status write). If the
                    # card was ever sticky-blocked, honour that gate here too
                    # — otherwise a blocked→todo reset escapes the sticky
                    # guard and the dispatcher reclaims it (live evidence:
                    # t_jarvis_autopromote_20260728, 2026-07-28 20:26Z and the
                    # 6-wake loop that followed). ``dependency`` kind is the
                    # intentional auto-recovery path and is exempt.
                    if row_block_kind != "dependency" and _has_sticky_block(
                        conn, task_id
                    ):
                        continue
                    conn.execute(
                        "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                        (task_id,),
                    )
                _append_event(conn, task_id, "promoted", None)
                promoted += 1
    return promoted


# Approval markers recognised by the fleet relapse detector
# (~/.hermes/scripts/kanban-approve-block-lockgate.py) -- kept in sync.
_APPROVAL_NEGATED_RE = re.compile(
    r"\b(no|not|without|don't|do not)\b[^\n]{0,40}REVIEW_VERDICT",
    re.IGNORECASE,
)
_APPROVAL_REOPEN_RE = re.compile(
    r"REVIEW_VERDICT=(CHANGES_REQUESTED|REJECT)|\bre-?open(ed)?\b",
    re.IGNORECASE,
)


def apply_approvals(conn: sqlite3.Connection) -> list:
    """Auto-clear approved-but-stuck cards (t_6009ccaa).

    Scans tasks in (``blocked``, ``review``, ``scheduled``). A card
    auto-clears to ``todo`` (``recompute_ready`` then promotes it to
    ``ready`` once parents are done) when ALL hold:

    * a non-negated approval marker comment exists
      (``REVIEW_VERDICT=APPROVED`` / ``REVIEW_VERDICT: APPROVED``);
    * no reviewer re-open (CHANGES_REQUESTED / REJECT / re-open) comment
      was posted AFTER that approval;
    * the approval comment has not already auto-cleared this card once
      (idempotence — see below);
    * no open parent dependency.

    Idempotence (t_jarvis_autopromote_20260728): an approval verdict
    addresses the thing it reviewed (usually code correctness). A card
    that re-blocks afterwards for a DIFFERENT reason (e.g. a 24h soak
    gate, a needs_input/capability park) must NOT be re-cleared by the
    same stale approval comment — otherwise every post-approval block
    is defeated ~instantly on the next dispatch tick, which is exactly
    the auto-promote-defeats-gates class of bug this family of fixes
    exists to close. We therefore skip an approval comment_id that has
    already produced an ``approval-auto-clear`` unblocked event on this
    card. A NEW approval comment posted after the re-block (fresh
    verdict on the new state) has a new comment_id and clears normally.

    Appends an ``unblocked`` event with reason ``approval-auto-clear`` so
    :func:`_has_sticky_block` flips off durably. Returns cleared task ids.
    """
    cleared = []
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, status FROM tasks "
            "WHERE status IN ('blocked', 'review', 'scheduled')"
        ).fetchall()
        for row in rows:
            task_id = row["id"]
            approval = conn.execute(
                "SELECT id, body FROM task_comments WHERE task_id = ? AND ("
                "body LIKE '%REVIEW_VERDICT=APPROVED%' "
                "OR body LIKE '%REVIEW_VERDICT: APPROVED%') "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if approval is None:
                continue
            if _APPROVAL_NEGATED_RE.search(approval["body"] or ""):
                continue  # "No REVIEW_VERDICT=APPROVED..." is a denial
            already_fired = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'unblocked' "
                "AND json_extract(payload, '$.reason') = 'approval-auto-clear' "
                "AND json_extract(payload, '$.comment_id') = ? LIMIT 1",
                (task_id, approval["id"]),
            ).fetchone()
            if already_fired is not None:
                continue  # same approval already cleared this card once;
                # a later re-block is a new gate the stale verdict must not defeat
            reopen = conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ? AND id > ?",
                (task_id, approval["id"]),
            ).fetchall()
            if any(_APPROVAL_REOPEN_RE.search(r["body"] or "") for r in reopen):
                continue  # reviewer re-opened after the approval
            undone = conn.execute(
                "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                "WHERE l.child_id = ? "
                "AND p.status NOT IN ('done', 'archived') LIMIT 1",
                (task_id,),
            ).fetchone()
            if undone is not None:
                continue  # open parent dep -- leave for parent gating
            conn.execute(
                "UPDATE tasks SET status = 'todo', current_run_id = NULL, "
                "consecutive_failures = 0, last_failure_error = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status IN ('blocked', 'review', 'scheduled')",
                (task_id,),
            )
            _append_event(
                conn,
                task_id,
                "unblocked",
                {"reason": "approval-auto-clear", "comment_id": approval["id"]},
            )
            cleared.append(task_id)
    return cleared


# ---------------------------------------------------------------------------
# Claim / complete / block
# ---------------------------------------------------------------------------

def claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        # Structural invariant: never transition ready -> running while any
        # parent is not yet 'done'. This is the single enforcement point
        # regardless of which writer (create_task, link_tasks, unblock_task,
        # release_stale_claims, manual SQL) set status='ready'. If a racy
        # writer promoted a task with undone parents, demote it back to
        # 'todo' here — recompute_ready will re-promote when the parents
        # actually finish. See RCA at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        undone = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
            (task_id,),
        ).fetchone()
        if undone:
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'",
                (task_id,),
            )
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "parents_not_done"},
            )
            return None
        # Defensive: if a prior run somehow leaked (invariant violation from
        # an unknown code path), close it as 'reclaimed' so we don't strand
        # it when the CAS resets the pointer below. No-op when the invariant
        # holds (the common case).
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'ready'",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on re-claim'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'ready'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        # Look up the current task row so we can populate the run with
        # its assignee / step / runtime cap.
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id},
            run_id=run_id,
        )
        claimed = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_claimed",
        task_id,
        board=get_current_board(),
        assignee=claimed.assignee if claimed else None,
        run_id=run_id,
    )
    return claimed


def claim_review_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``review -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``review`` status).

    Unlike ``claim_task`` (which handles ``ready -> running``), this
    does NOT check parent dependencies — the task already passed that
    gate on its original ``todo -> ready -> running`` transition.

    Creates a new run entry so the review agent's lifecycle is tracked
    independently from the original worker run.
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'review'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id,
             "source_status": "review"},
            run_id=run_id,
        )
        return get_task(conn, task_id)


def heartbeat_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim.  Returns True if we still own it.

    Workers that know they'll exceed 15 minutes should call this every
    few minutes to keep ownership.
    """
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?",
            (expires, task_id, lock),
        )
        if cur.rowcount == 1:
            run_id = _current_run_id(conn, task_id)
            if run_id is not None:
                conn.execute(
                    "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                    (expires, run_id),
                )
            return True
        return False


class ClaimLeaseLost(RuntimeError):
    """An external executor no longer owns the native Hermes task claim."""


def require_claim_heartbeat(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    claimer: str,
    expected_run_id: int,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Renew one exact native claim or fail closed.

    This is intentionally synchronous and transport-agnostic: a real executor
    owns the periodic loop and must call it on a cadence safely below its TTL,
    including immediately before accepting an external result.  A false return
    from the native CAS is a lost lease, never a reason to continue or apply a
    stale provider result.
    """
    if not isinstance(claimer, str) or not claimer.strip():
        raise ValueError("claimer must be a non-empty string")
    if isinstance(expected_run_id, bool) or not isinstance(expected_run_id, int) or expected_run_id <= 0:
        raise ValueError("expected_run_id must be a positive integer")
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ? "
            "AND status = 'running' AND claim_lock = ? AND current_run_id = ?",
            (expires, task_id, claimer, expected_run_id),
        )
        if cur.rowcount == 1:
            run = conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE id = ? AND task_id = ? "
                "AND status = 'running'",
                (expires, expected_run_id, task_id),
            )
            if run.rowcount == 1:
                return
        # Raise *inside* the transaction so the preceding task-row renewal
        # rolls back too.  A split lease (task renewed, run not renewed) is not
        # safe evidence of ownership.
        raise ClaimLeaseLost(
            f"lost Hermes claim for task {task_id!r}; refusing external result"
        )


def register_claim_process(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    claimer: str,
    expected_run_id: int,
    pid: int,
) -> None:
    """Bind one real executor process to the exact live Hermes claim.

    This is deliberately stricter than the legacy spawn helper: a process is
    recorded only while the named task, lock, and run still match.  A reclaim
    makes the next executor heartbeat fail, and the executor's local cleanup
    terminates its own process group rather than trusting a later task owner.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    with write_txn(conn):
        task = conn.execute(
            "UPDATE tasks SET worker_pid=? WHERE id=? AND status='running' "
            "AND claim_lock=? AND current_run_id=?",
            (pid, task_id, claimer, expected_run_id),
        )
        run = conn.execute(
            "UPDATE task_runs SET worker_pid=? WHERE id=? AND task_id=? "
            "AND status='running' AND claim_lock=?",
            (pid, expected_run_id, task_id, claimer),
        )
        if task.rowcount != 1 or run.rowcount != 1:
            raise ClaimLeaseLost(
                f"lost Hermes claim for task {task_id!r}; refusing process registration"
            )
        _append_event(
            conn, task_id, "external_executor_started",
            {"pid": pid, "run_id": expected_run_id, "claimer": claimer},
            run_id=expected_run_id,
        )


def release_stale_claims(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> int:
    """Reset any ``running`` task whose claim has expired.

    A stale-by-TTL claim whose host-local worker PID is still alive is
    *extended* (with a ``claim_extended`` event) instead of being
    reclaimed. Reclaiming a live worker mid-flight produces the spawn-
    then-immediately-reclaim loop seen on slow models that spend longer
    than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM
    call (#23025): no tool calls means no ``kanban_heartbeat``, even
    though the subprocess is healthy.

    Backstop (#29747 gap 3): if the worker's PID is still alive but its
    ``last_heartbeat_at`` is stale by more than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has
    been making no observable progress and we reclaim anyway — even if
    ``_pid_alive`` is still true. This catches the wedged-in-a-logic-loop
    case where the process is technically running but accomplishing
    nothing. ``_touch_activity`` (run_agent.py) bridges chunk-level
    liveness into ``last_heartbeat_at`` via #31752, so any genuinely
    active worker keeps its heartbeat fresh as a side effect of normal
    API traffic. ``enforce_max_runtime`` and ``detect_crashed_workers``
    remain the upper bounds for genuinely wedged or dead workers.

    Returns the number of stale claims actually reclaimed (live-pid
    extensions don't count). Safe to call often.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, claim_expires, last_heartbeat_at "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?",
        (now,),
    ).fetchall()
    for row in stale:
        lock = row["claim_lock"] or ""
        host_local = lock.startswith(host_prefix)
        hb = row["last_heartbeat_at"]
        # Heartbeat staleness backstop: if we have a heartbeat at all
        # and it's older than the max-stale threshold, the worker is
        # not making observable progress.  Reclaim instead of extending,
        # even if the PID is still alive (it's likely in a logic loop).
        heartbeat_stale = (
            hb is not None
            and (now - int(hb)) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        )
        if (
            host_local
            and row["worker_pid"]
            and _pid_alive(row["worker_pid"])
            and not heartbeat_stale
        ):
            new_expires = now + _resolve_claim_ttl_seconds()
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET claim_expires = ? "
                    "WHERE id = ? AND status = 'running' "
                    "  AND claim_lock IS ? "
                    "  AND claim_expires IS NOT NULL "
                    "  AND claim_expires < ?",
                    (new_expires, row["id"], row["claim_lock"], now),
                )
                if cur.rowcount != 1:
                    continue
                run_id = _current_run_id(conn, row["id"])
                if run_id is not None:
                    conn.execute(
                        "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                        (new_expires, run_id),
                    )
                _append_event(
                    conn, row["id"], "claim_extended",
                    {
                        "reason": "pid_alive",
                        "worker_pid": int(row["worker_pid"]),
                        "claim_lock": row["claim_lock"],
                        "claim_expires_was": int(row["claim_expires"]),
                        "claim_expires_now": new_expires,
                        "last_heartbeat_at": (
                            int(row["last_heartbeat_at"])
                            if row["last_heartbeat_at"] is not None
                            else None
                        ),
                    },
                    run_id=run_id,
                )
            continue

        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn,
        )
        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], row["claim_lock"], now, termination,
                reason="ttl_expired_worker_alive",
            )
            continue
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ?",
                (row["id"], row["claim_lock"], now),
            )
            if cur.rowcount != 1:
                continue
            run_id = _end_run(
                conn, row["id"],
                outcome="reclaimed", status="reclaimed",
                error=f"stale_lock={row['claim_lock']}",
                metadata=termination,
            )
            payload = {
                "stale_lock": row["claim_lock"],
                "worker_pid": (
                    int(row["worker_pid"])
                    if row["worker_pid"] is not None else None
                ),
                "claim_expires": int(row["claim_expires"]),
                "last_heartbeat_at": (
                    int(row["last_heartbeat_at"])
                    if row["last_heartbeat_at"] is not None else None
                ),
                "now": now,
                "host_local": host_local,
                "heartbeat_stale": bool(heartbeat_stale),
            }
            payload.update(termination)
            _append_event(
                conn, row["id"], "reclaimed",
                payload,
                run_id=run_id,
            )
            reclaimed += 1
    return reclaimed


def reclaim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    signal_fn=None,
) -> bool:
    """Operator-driven reclaim: release the claim and reset to ``ready``.

    Unlike :func:`release_stale_claims` which only acts on tasks whose
    ``claim_expires`` has passed, this function reclaims immediately
    regardless of TTL. Intended for the dashboard/CLI recovery flow
    when an operator wants to abort a running worker without waiting
    for the TTL to expire (e.g. after seeing a hallucination warning).

    Returns True if a reclaim happened, False if the task isn't in a
    reclaimable state (not running, or doesn't exist).
    """
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    termination = _terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn,
    )
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ?",
            (task_id, prev_lock),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            error=(
                f"manual_reclaim: {reason}" if reason
                else f"manual_reclaim lock={prev_lock}"
            ),
            metadata=termination,
        )
        payload = {
            "manual": True,
            "reason": reason,
            "prev_lock": prev_lock,
        }
        payload.update(termination)
        _append_event(
            conn, task_id, "reclaimed",
            payload,
            run_id=run_id,
        )
    # Operator intervention — they've looked at the task, so the
    # consecutive-failures counter is now stale. Give the next retry
    # a fresh budget. (_clear_failure_counter opens its own write_txn,
    # so it runs after the enclosing one commits.)
    _clear_failure_counter(conn, task_id)
    return True


def reassign_task(
    conn: sqlite3.Connection,
    task_id: str,
    profile: Optional[str],
    *,
    reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign a task, optionally reclaiming a stuck running worker first.

    This is the recovery path for "this profile's model is broken, try
    a different one". If ``reclaim_first`` is True, any active claim is
    released (via :func:`reclaim_task`) before the reassign happens;
    otherwise the function refuses to reassign a currently-running task
    and returns False (caller can retry with ``reclaim_first=True``).

    Returns True if the reassign landed. ``profile`` may be ``None`` to
    unassign entirely.
    """
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _verify_created_cards(
    conn: sqlite3.Connection,
    completing_task_id: str,
    claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom).

    A card is "verified" iff a row exists in ``tasks`` AND at least one
    of the following holds:

    * ``created_by`` matches the completing task's ``assignee`` profile
      (the common case: worker A spawns a card via ``kanban_create``,
      which stamps ``created_by=A``).
    * ``created_by`` matches the completing task's id (edge case where
      a worker passed its own task id as the ``created_by`` value).
    * The card is linked as a ``task_links.child`` of the completing
      task — i.e. the worker explicitly called ``kanban_create`` with
      ``parents=[<current_task>]``. This accepts cards created through
      the dashboard/CLI by a different principal but then attached to
      the completing task by the worker.

    ``phantom`` returns ids that either don't exist at all, or exist
    but don't satisfy any of the three trust conditions. The caller
    decides what to do with each bucket; this helper never mutates.
    """
    claimed = [str(x).strip() for x in (claimed_ids or []) if str(x).strip()]
    if not claimed:
        return [], []
    # Dedupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in claimed:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)

    row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,),
    ).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})",
        tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        if created_by is None:
            phantom.append(cid)
            continue
        # Accept if any of the three trust conditions holds.
        if completing_assignee and created_by == completing_assignee:
            verified.append(cid)
        elif created_by == completing_task_id:
            verified.append(cid)
        elif cid in linked_children:
            verified.append(cid)
        else:
            phantom.append(cid)
    return verified, phantom


# Task-id pattern used both by ``kanban_create`` (``t_<12 hex>``) and
# ``_new_task_id`` below. Kept permissive on length for forward compat:
# accept 8+ hex chars after the ``t_`` prefix.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(
    conn: sqlite3.Connection,
    text: str,
) -> list[str]:
    """Regex-scan free-form text for ``t_<hex>`` references; return the
    ones that don't exist in ``tasks``.

    Used as a non-blocking advisory check on completion summaries. An
    empty return means "no suspicious references found" — either the
    text had no IDs at all, or every ID it mentioned resolves to a real
    task. Duplicates are deduped.
    """
    if not text:
        return []
    matches = _TASK_ID_PROSE_RE.findall(text)
    if not matches:
        return []
    # Dedupe preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    placeholders = ",".join(["?"] * len(unique))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        tuple(unique),
    ).fetchall()
    existing = {r["id"] for r in rows}
    return [m for m in unique if m not in existing]


class HallucinatedCardsError(ValueError):
    """Raised by ``complete_task`` when ``created_cards`` contains ids
    that don't exist or weren't created by the completing worker.

    The phantom list is attached as ``.phantom`` for callers that want
    structured access. Kept as ``ValueError`` subclass so existing
    tool-error handlers treat it as a recoverable user error.
    """

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class ArtifactPreservationError(RuntimeError):
    """Raised when a declared scratch deliverable cannot be preserved."""


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    expected_run_id: Optional[int] = None,
    a3_guard: bool = False,
) -> bool:
    """Transition ``running|ready -> done`` and record ``result``.

    Accepts a task that is merely ``ready`` too, so a manual CLI
    completion (``hermes kanban complete <id>``) works without requiring
    a claim/start/complete sequence.

    ``summary`` and ``metadata`` are stored on the closing run (if any)
    and surfaced to downstream children via :func:`build_worker_context`.
    When ``summary`` is omitted we fall back to ``result`` so single-run
    callers do not have to pass both. ``metadata`` is a free-form dict
    (e.g. ``{"changed_files": [...], "tests_run": [...]}``) — workers
    are encouraged to use it for structured handoff facts.

    ``created_cards`` is an optional list of task ids the completing
    worker claims to have created. Each id is verified against
    ``tasks.created_by``. If any id is phantom (does not exist or was
    not created by this worker's assignee profile), completion is blocked
    with a ``HallucinatedCardsError`` and a
    ``completion_blocked_hallucination`` event is emitted so the rejected
    attempt is auditable. When all ids verify, they are recorded on the
    ``completed`` event payload.

    After a successful completion, ``summary`` and ``result`` are scanned
    for prose references like ``t_deadbeefcafe`` that do not resolve.
    Any suspected phantom references are recorded as a
    ``suspected_hallucinated_references`` event. This pass is advisory
    and never blocks.
    """
    now = int(time.time())

    # Gate: verify created_cards BEFORE the main write txn. A rejected
    # completion still needs an auditable event, so we emit it in a
    # tiny dedicated txn, then raise. The caller is responsible for
    # surfacing HallucinatedCardsError to the worker; this function
    # never mutates task state on a phantom-card rejection.
    if created_cards:
        verified_cards, phantom_cards = _verify_created_cards(
            conn, task_id, created_cards
        )
        if phantom_cards:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_hallucination",
                    {
                        "phantom_cards": phantom_cards,
                        "verified_cards": verified_cards,
                        "summary_preview": (
                            (summary or result or "").strip().splitlines()[0][:200]
                            if (summary or result)
                            else None
                        ),
                    },
                )
            raise HallucinatedCardsError(phantom_cards, task_id)
    else:
        verified_cards = []

    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    with write_txn(conn):
        # Terminal-time kill guard (default OFF — existing callers are
        # unaffected). Evaluated INSIDE this transaction, alongside the
        # expected_run_id CAS, so a latch landing between an earlier check and
        # this write cannot slip through. Raising here rolls the transaction
        # back, so no partial mutation survives.
        if a3_guard and a3_revocation_latched(conn, task_id):
            raise ExecutionNotPermitted(
                f"A3 revocation is latched for {task_id}; refusing terminal "
                "board mutation"
            )
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                """,
                (result, now, task_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                   AND current_run_id = ?
                """,
                (result, now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        if isinstance(metadata, dict):
            _persist_scratch_completion_artifacts(conn, task_id, metadata)
            for stored_path in metadata.pop("_staged_artifacts", []):
                path = Path(stored_path)
                _insert_completion_attachment(
                    conn,
                    task_id,
                    filename=path.name,
                    stored_path=str(path),
                    size=path.stat().st_size,
                    created_at=now,
                )
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="done",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )
        # If complete_task was called on a never-claimed task (ready or
        # blocked → done with no run in flight), synthesize a
        # zero-duration run so the handoff fields are persisted in
        # attempt history instead of silently lost.
        if run_id is None and (summary or metadata or result):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=summary if summary is not None else result,
                metadata=metadata,
            )
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        completed_payload: dict = {
            "result_len": len(result) if result else 0,
            "summary": ev_summary or None,
        }
        if verified_cards:
            completed_payload["verified_cards"] = verified_cards
        # Carry artifact paths in the event payload so the gateway
        # notifier can upload them as native attachments alongside the
        # completion message. Workers pass these via
        # ``kanban_complete(artifacts=[...])`` which stashes the list in
        # ``metadata["artifacts"]`` — we promote it onto the event so
        # consumers don't have to fetch the run row to find it.
        if isinstance(metadata, dict):
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    completed_payload["artifacts"] = cleaned_artifacts
        _append_event(
            conn, task_id, "completed",
            completed_payload,
            run_id=run_id,
        )
    # Prose-scan the summary + result for t_<hex> references that do
    # not resolve. Advisory — does not block the completion. Runs in
    # its own txn so the completion itself is already durable by the
    # time we emit the warning.
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        # Drop any phantom refs that were already flagged as verified
        # above (shouldn't happen — verified means they exist — but
        # belt-and-suspenders).
        phantom_refs = [p for p in phantom_refs if p not in set(verified_cards)]
        if phantom_refs:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "suspected_hallucinated_references",
                    {
                        "phantom_refs": phantom_refs,
                        "source": "completion_summary",
                    },
                    run_id=run_id,
                )
    # Successful completion — wipe the consecutive-failures counter.
    # Failure history stays on the event log for audit; the counter
    # just tracks "is there a current pathology the breaker should
    # care about", and a success resets that question.
    _clear_failure_counter(conn, task_id)
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    # Clean up the scratch workspace and any stale tmux session for the worker.
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_completed",
        task_id,
        board=get_current_board(),
        assignee=_done_task.assignee if _done_task else None,
        run_id=run_id,
        summary=(summary if summary is not None else result),
    )
    return True


# ---------------------------------------------------------------------------
# Workspace / tmux cleanup
# ---------------------------------------------------------------------------


def _merge_completion_prose_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict],
    *,
    summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Promote existing scratch files named in legacy completion prose.

    ``artifacts=[...]`` is preferred. Older workers only wrote an absolute
    deliverable path in ``summary``/``result``; discover it while scratch still
    exists so cleanup cannot erase the file the user was promised.
    """
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return metadata
    workspace = Path(row["workspace_path"]).expanduser()
    if not _is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated


def _persist_scratch_completion_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return

    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return

    workspace = Path(row["workspace_path"]).expanduser()
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return

    try:
        workspace_root = workspace.resolve()
    except OSError:
        return

    attachment_dir = task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    changed = False

    def _discard_copies() -> None:
        for copied in used_destinations:
            try:
                copied.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            attachment_dir.rmdir()
        except OSError:
            pass

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue

        if not resolved_src.is_relative_to(workspace_root):
            persisted.append(artifact)
            continue

        if not src.is_file():
            _discard_copies()
            raise ArtifactPreservationError(
                f"declared scratch artifact is unavailable or not a regular file: {artifact}"
            )

        size = resolved_src.stat().st_size
        if size > KANBAN_ATTACHMENT_MAX_BYTES:
            _discard_copies()
            raise ArtifactPreservationError(
                f"declared scratch artifact exceeds the "
                f"{KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )

        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            with resolved_src.open("rb") as source_file, dest.open("xb") as destination_file:
                copied = 0
                while chunk := source_file.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > KANBAN_ATTACHMENT_MAX_BYTES:
                        raise ArtifactPreservationError(
                            f"declared scratch artifact grew beyond the size limit: {artifact}"
                        )
                    destination_file.write(chunk)
        except Exception as exc:
            if dest is not None:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            _discard_copies()
            if isinstance(exc, ArtifactPreservationError):
                raise
            raise ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc

        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        changed = True

    if changed:
        metadata["artifacts"] = persisted
        metadata["_staged_artifacts"] = [
            path for path in persisted if path.startswith(str(attachment_dir.resolve()))
        ]


def _insert_completion_attachment(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    filename: str,
    stored_path: str,
    size: int,
    created_at: int,
) -> None:
    """Record a worker-produced artifact in the existing attachment table."""
    conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
        "VALUES (?, ?, ?, NULL, ?, 'kanban_complete', ?)",
        (task_id, filename, stored_path, size, created_at),
    )
    _append_event(
        conn,
        task_id,
        "attached",
        {"filename": filename, "size": size, "by": "kanban_complete"},
    )


def _unique_attachment_path(directory: Path, filename: str, used: set[Path]) -> Path:
    """Return a non-conflicting path under ``directory`` for ``filename``."""
    safe_name = Path(filename).name or "artifact"
    candidate = directory / safe_name
    if candidate not in used and not candidate.exists():
        return candidate

    stem = Path(safe_name).stem or "artifact"
    suffix = Path(safe_name).suffix
    idx = 1
    while True:
        candidate = directory / f"{stem}_{idx}{suffix}"
        if candidate not in used and not candidate.exists():
            return candidate
        idx += 1


def _managed_scratch_path_info(p: Path) -> tuple[bool, Optional[str]]:
    """Return whether *p* is managed scratch storage and the matching board."""
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False, None
    roots: list[tuple[Path, Optional[str]]] = []
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        try:
            roots.append((Path(override).expanduser().resolve(strict=False), None))
        except OSError:
            pass
    try:
        home = kanban_home()
    except OSError:
        home = None
    if home is not None:
        try:
            roots.append(((home / "kanban" / "workspaces").resolve(strict=False), DEFAULT_BOARD))
        except OSError:
            pass
        try:
            boards_parent = (home / "kanban" / "boards").resolve(strict=False)
        except OSError:
            boards_parent = None
        if boards_parent is not None:
            try:
                entries = list(boards_parent.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                try:
                    roots.append(((entry / "workspaces").resolve(strict=False), entry.name))
                except OSError:
                    continue
    for root, board in roots:
        if p_abs == root:
            continue
        try:
            if p_abs.is_relative_to(root):
                return True, board
        except ValueError:
            continue
    return False, None


def _is_managed_scratch_path(p: Path) -> bool:
    """Return True iff *p* is a strict descendant of a kanban-managed scratch root.

    A managed root is exclusively a ``workspaces/`` directory — never the
    broader kanban home, a board root, or sibling subtrees like ``logs/`` or
    ``boards/<slug>/`` itself. Allowed roots:

    * ``HERMES_KANBAN_WORKSPACES_ROOT`` when set (worker-side override
      injected by the dispatcher).
    * ``<kanban_home>/kanban/workspaces`` — legacy default-board scratch root.
    * ``<kanban_home>/kanban/boards/<slug>/workspaces`` for each board slug
      that currently exists on disk.

    The check requires strict descendancy: a path equal to one of these
    roots is NOT managed (deleting the workspaces root would wipe every
    task's scratch dir at once), and a path that resolves to ``<kanban_home>
    /kanban`` itself, ``<kanban_home>/kanban/logs``, or
    ``<kanban_home>/kanban/boards/<slug>`` is rejected because those
    subtrees hold Hermes' own DB, metadata, and logs, not task workspaces.

    Used by :func:`_cleanup_workspace` to refuse to ``shutil.rmtree`` paths
    outside Hermes-managed storage. A board ``default_workdir`` pointing at a
    real source tree can otherwise pair with ``workspace_kind='scratch'`` and
    cause task completion to delete user data (#28818).
    """
    is_managed, _board = _managed_scratch_path_info(p)
    return is_managed


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir and kill its stale tmux session.

    Called from :func:`complete_task` after the DB transaction commits.
    Best-effort — any error is swallowed so cleanup never blocks task completion.
    Only ``scratch`` workspaces are removed; ``worktree`` and ``dir`` workspaces
    are intentionally preserved.
    """
    try:
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return
        kind: Optional[str] = row["workspace_kind"]
        path: Optional[str] = row["workspace_path"]
        if kind != "scratch" or not path:
            # This task's own workspace isn't a removable scratch dir, but its
            # completion may still unblock a deferred parent scratch cleanup
            # (e.g. a 'dir' child whose scratch parent was waiting on it). #33774
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        # Check if this task has children that still need the workspace.
        # If any child is not yet done/archived, defer cleanup so the
        # child can read handoff artifacts from the scratch dir (#33774).
        _active_children = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
            "LIMIT 1",
            (task_id,),
        ).fetchone()
        if _active_children:
            _log.debug(
                "Deferring scratch workspace cleanup for task %s: "
                "active children still need workspace at %s",
                task_id, path,
            )
            return
        import shutil
        wp = Path(path)
        if wp.is_dir():
            # Containment guard (#28818): a board's ``default_workdir`` can
            # pair ``workspace_kind='scratch'`` with a user-supplied path
            # pointing at a real source tree. Without this check, task
            # completion would unconditionally ``shutil.rmtree`` that path
            # and silently delete the user's source data.
            if _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Removed scratch workspace: %s", wp)
            else:
                _log.warning(
                    "Refusing to remove out-of-scratch workspace for task %s: %s "
                    "(workspace_kind='scratch' but path is outside any "
                    "kanban-managed workspaces root)",
                    task_id, wp,
                )
        # Also kill the tmux session for the worker that owned this task,
        # if the tmux session is now dead (worker process exited).
        _cleanup_worker_tmux(conn, task_id)
        # After cleaning up this task's workspace, check if any parent
        # tasks now have all children done — their deferred cleanup can
        # proceed (#33774).
        _try_cleanup_parent_workspaces(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _try_cleanup_parent_workspaces(conn: sqlite3.Connection, task_id: str) -> None:
    """Clean up parent scratch workspaces now that *task_id* completed.

    When a parent task's cleanup was deferred because it had active children,
    this function is called after each child completes.  If all children of a
    parent are now done/archived/failed/cancelled, the parent's scratch
    workspace is removed (#33774).
    """
    try:
        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        for (parent_id,) in parents:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
                continue
            # Check if ALL children of this parent are terminal
            active = conn.execute(
                "SELECT 1 FROM task_links l "
                "JOIN tasks t ON t.id = l.child_id "
                "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
                "LIMIT 1",
                (parent_id,),
            ).fetchone()
            if active:
                continue  # still has active children
            # All children done — safe to clean up parent workspace
            import shutil
            wp = Path(row["workspace_path"])
            if wp.is_dir() and _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Deferred cleanup: removed parent %s scratch workspace: %s", parent_id, wp)
    except Exception:
        pass  # best-effort


def _cleanup_worker_tmux(conn: sqlite3.Connection, task_id: str) -> None:
    """Kill the tmux session associated with a task's assignee, if dead."""
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or not row["assignee"]:
            return
        assignee: str = row["assignee"]
        # Workers named swarm1-12 use tmux sessions named swarm-swarm1 etc.
        session = f"swarm-{assignee}"
        # Check if session exists and pane is dead before killing
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
        )
        if out.stdout.strip() == "1":
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True, timeout=5,
            )
            _log.debug("Killed stale tmux session: %s", session)
    except Exception:
        pass  # best-effort — never block completion


# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------
#
# Scratch workspaces are intentionally ephemeral — ``_cleanup_workspace``
# removes them as soon as ``complete_task`` runs.  New users often don't
# realize that and lose worker output (community report, May 2026).  The
# behavior is right; the lack of warning is the bug.
#
# On the FIRST scratch workspace materialization across the whole install
# we:
#   1. Log a warning line on the dispatcher logger.
#   2. Append a ``tip_scratch_workspace`` event on the task so it's visible
#      via ``hermes kanban show <id>`` and the dashboard.
#   3. Touch a sentinel file under ``kanban_home() / '.scratch_tip_shown'``
#      so we don't repeat the tip — once you know, you know.
#
# Scope is per-install, not per-board: a user creating a second board
# already learned the lesson on board #1.

_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"

_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're deleted when the task "
    "completes. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip has already been emitted on this
    install. Best-effort — any error means we re-emit, which is the safer
    failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent.

    Best-effort: a failure here just means the tip might appear once more,
    which is preferable to crashing dispatch over a help message.
    """
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip exactly once per install.

    Called from the dispatcher right after a scratch workspace is
    materialized. No-op for ``worktree`` / ``dir`` workspaces (they're
    preserved by design) and no-op after the sentinel exists.
    """
    if (workspace_kind or "scratch") != "scratch":
        return
    if _scratch_tip_shown():
        return
    try:
        _log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with write_txn(conn):
            _append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


def edit_completed_task_result(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: str,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Backfill the user-visible result for an already completed task."""
    handoff_summary = summary if summary is not None else result
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if not row or row["status"] != "done":
            return False
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (result, task_id),
        )
        run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        run_id = int(run["id"]) if run else None
        if run_id is None:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=handoff_summary,
                metadata=metadata,
            )
        else:
            conn.execute(
                "UPDATE task_runs SET summary = ? WHERE id = ?",
                (handoff_summary, run_id),
            )
            if metadata is not None:
                conn.execute(
                    "UPDATE task_runs SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), run_id),
                )
        ev_summary = (
            handoff_summary.strip().splitlines()[0][:400]
            if handoff_summary else ""
        )
        _append_event(
            conn, task_id, "edited",
            {
                "fields": (
                    ["result", "summary"]
                    + (["metadata"] if metadata is not None else [])
                ),
                "result_len": len(result) if result else 0,
                "summary": ev_summary or None,
            },
            run_id=run_id,
        )
    return True


def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    kind: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    a3_guard: bool = False,
) -> bool:
    """Transition ``running``/``ready`` → ``blocked`` (or route elsewhere).

    ``kind`` (one of :data:`VALID_BLOCK_KINDS`, or ``None`` for a legacy
    un-typed block) drives routing instead of every block landing in one
    undifferentiated ``blocked`` bucket:

    * ``dependency`` — the task is only waiting on another task. It does NOT
      sit in ``blocked`` (where a cron would keep "unblocking" it); it goes to
      ``todo`` so the existing parent-gating / ``recompute_ready`` machinery
      promotes it automatically once its parents finish. No human, no cron, no
      retry storm. This is Dale's "Type 2 — dependency blocked".

    * ``needs_input`` / ``capability`` / ``None`` — "truly blocked" (Dale's
      "Type 1"). Lands in ``blocked`` for a human. BUT: each time such a task
      is re-blocked for the SAME kind after having been unblocked, the
      unblock-loop counter (``block_recurrences``) increments. When it reaches
      :data:`BLOCK_RECURRENCE_LIMIT`, the task is routed to ``triage`` instead
      of ``blocked`` — breaking the cron-unblock ↔ worker-re-block loop and
      forcing a human-in-the-loop triage decision.

    * ``transient`` — treated like a generic block for routing, but a worker
      can use it to signal "this might clear on its own"; it still participates
      in the loop breaker so a forever-flaky task eventually escalates.

    Returns True on any successful transition (to ``blocked``, ``todo``, or
    ``triage``), False when the task wasn't in a blockable state.
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(
            f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None"
        )
    routed_to = "blocked"
    recurrences = 0
    with write_txn(conn):
        # Terminal-time kill guard (default OFF — existing callers are
        # unaffected). Evaluated INSIDE this transaction, alongside the
        # expected_run_id CAS, so a latch landing between an earlier check and
        # this write cannot slip through. Raising here rolls the transaction
        # back, so no partial mutation survives.
        if a3_guard and a3_revocation_latched(conn, task_id):
            raise ExecutionNotPermitted(
                f"A3 revocation is latched for {task_id}; refusing terminal "
                "board mutation"
            )
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
        prev_kind = cur_row["block_kind"] if "block_kind" in cur_row.keys() else None
        prev_recurrences = (
            int(cur_row["block_recurrences"])
            if "block_recurrences" in cur_row.keys()
            and cur_row["block_recurrences"] is not None
            else 0
        )

        # Dependency blocks never enter the human ``blocked`` bucket — they
        # wait in ``todo`` and let ``recompute_ready`` gate on parents. Routing
        # here (rather than ``blocked``) is what keeps a cron from ever seeing
        # a dependency-wait as something to "unblock".
        if kind == "dependency":
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'todo',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       block_kind    = ?
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + ("" if expected_run_id is None else " AND current_run_id = ?"),
                (kind, task_id) if expected_run_id is None
                else (kind, task_id, int(expected_run_id)),
            )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
            )
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="blocked", summary=reason,
                )
            _append_event(
                conn, task_id, "dependency_wait",
                {"reason": reason, "kind": kind}, run_id=run_id,
            )
            routed_to = "todo"
            _blocked_task = get_task(conn, task_id)
            _fire_kanban_lifecycle_hook(
                "kanban_task_blocked",
                task_id,
                board=get_current_board(),
                assignee=_blocked_task.assignee if _blocked_task else None,
                run_id=run_id,
                reason=reason,
            )
            return True

        # Truly-blocked kinds. Increment the unblock-loop counter when this is a
        # re-block for the SAME reason after a prior unblock. block_task only
        # fires from running/ready (i.e. AFTER an unblock returned the task to
        # the work pool), so a stored block_kind that matches the incoming kind
        # means: blocked → unblocked → about-to-re-block for the same cause.
        # An un-typed (None) block compares as "same" to a prior un-typed block.
        same_cause = prev_kind == kind
        recurrences = prev_recurrences + 1 if same_cause else 1

        if recurrences >= BLOCK_RECURRENCE_LIMIT:
            # Loop detected — stop letting the unblocker spin this task. Route
            # to triage for a human-in-the-loop decision instead of blocked.
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'triage',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       block_kind    = ?,
                       block_recurrences = ?
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + ("" if expected_run_id is None else " AND current_run_id = ?"),
                (kind, recurrences, task_id) if expected_run_id is None
                else (kind, recurrences, task_id, int(expected_run_id)),
            )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
            )
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="blocked", summary=reason,
                )
            _append_event(
                conn, task_id, "block_loop_detected",
                {
                    "reason": reason,
                    "kind": kind,
                    "recurrences": recurrences,
                    "limit": BLOCK_RECURRENCE_LIMIT,
                },
                run_id=run_id,
            )
            routed_to = "triage"
        else:
            if expected_run_id is None:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'blocked',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND status IN ('running', 'ready')
                    """,
                    (kind, recurrences, task_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'blocked',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND status IN ('running', 'ready')
                       AND current_run_id = ?
                    """,
                    (kind, recurrences, task_id, int(expected_run_id)),
                )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
            )
            # Synthesize a run when blocking a never-claimed task so the
            # reason is preserved in attempt history.
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id,
                    outcome="blocked",
                    summary=reason,
                )
            _append_event(
                conn, task_id, "blocked",
                {"reason": reason, "kind": kind, "recurrences": recurrences},
                run_id=run_id,
            )
        _blocked_task = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_blocked",
        task_id,
        board=get_current_board(),
        assignee=_blocked_task.assignee if _blocked_task else None,
        run_id=run_id,
        reason=reason,
    )
    return True



def promote_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Manually promote a `todo` or `blocked` task to `ready`.

    Mirrors the automatic promotion done by ``recompute_ready`` but
    drives it from a deliberate operator action with an audit-trail
    entry. Refuses to promote if any parent dep is not in a terminal
    state (`done`/`archived`) unless ``force=True``. Does NOT change
    assignee or claim state. Returns ``(True, None)`` on success and
    ``(False, reason)`` if refused. ``dry_run=True`` validates the
    promotion would succeed without mutating state.
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"

    cur_status = row["status"]
    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    if not force:
        parents = conn.execute(
            "SELECT t.id, t.status FROM tasks t "
            "JOIN task_links l ON l.parent_id = t.id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        unsatisfied = [
            p["id"] for p in parents
            if p["status"] not in ("done", "archived")
        ]
        if unsatisfied:
            return False, (
                f"unsatisfied parent dependencies: "
                f"{', '.join(unsatisfied)} (use --force to override)"
            )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')",
            (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(
            conn,
            task_id,
            "promoted_manual",
            {"actor": actor, "reason": reason, "forced": force},
        )

    return True, None


def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``blocked``/``scheduled`` -> ready or todo.

    Defensively closes any stale ``current_run_id`` pointer before flipping
    status. In the common path (``block_task`` closed the run already) this
    is a no-op. If a future or external write left the pointer dangling,
    the leaked run is closed as ``reclaimed`` inside the same txn so the
    runs invariant (``current_run_id IS NULL`` ⇔ run row in terminal
    state) holds for the rest of this function's lifetime.
    """
    now = int(time.time())
    with write_txn(conn):
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on unblock'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        # Re-gate on parent completion before flipping 'blocked' back to
        # 'ready'. Unconditionally setting status='ready' here bypasses the
        # parent-completion invariant (the dispatcher trusts that column);
        # if parents are still in progress the task must wait in 'todo'
        # until recompute_ready picks it up. RCA: Bug 2 at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        undone_parents = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
            (task_id,),
        ).fetchone()
        new_status = "todo" if undone_parents else "ready"
        # NOTE: deliberately does NOT touch ``block_recurrences`` or
        # ``block_kind``. Resetting the recurrence counter on unblock is exactly
        # the amnesia that let a cron unblock → worker re-block loop run
        # unbounded (Dale's report). The counter survives the unblock so that a
        # subsequent same-cause ``block_task`` can detect the loop and route to
        # triage at ``BLOCK_RECURRENCE_LIMIT``. It is reset to 0 only on a
        # successful completion (see ``complete_task``). ``consecutive_failures``
        # (the *dispatcher* spawn/crash/timeout counter — a different signal) is
        # still reset here, which is correct: a deliberate unblock is a fresh
        # start for the dispatcher's retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "unblocked",
            {"status": new_status} if new_status != "ready" else None,
        )
        return True


def specify_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
) -> bool:
    """Flesh out a triage task and promote it to ``todo``.

    Atomically updates ``title`` / ``body`` / ``assignee`` (when provided)
    and transitions ``status: triage -> todo`` in a single write txn. Returns
    False when the task is missing or not in the ``triage`` column — callers
    should surface that as "nothing to specify" rather than an error.

    ``todo`` (not ``ready``) is the correct landing column: ``recompute_ready``
    promotes parent-free / parent-done todos to ``ready`` on the next
    dispatcher tick, which keeps the normal parent-gating behaviour intact
    for specified tasks that happen to have open parents.

    ``author`` is recorded on an audit comment only when at least one of
    ``title`` / ``body`` / ``assignee`` actually changed — avoids noisy
    comment spam for status-only promotions.
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'",
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Inline INSERT (rather than ``add_comment``) because we're
            # already inside this function's write_txn — nested BEGIN
            # IMMEDIATE would raise OperationalError. We also skip the
            # 'commented' event that ``add_comment`` emits, since the
            # 'specified' event below already records the change.
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Specified — updated "
                    + ", ".join(changed_fields)
                    + " and promoted to todo.",
                    int(time.time()),
                ),
            )
        _append_event(
            conn,
            task_id,
            "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Outside the write_txn above, so we don't nest BEGIN IMMEDIATE — the
    # ready-promotion pass opens its own IMMEDIATE txn. This runs the same
    # logic the dispatcher would on its next tick, so a specified task
    # with no open parents flips straight to 'ready' here instead of
    # idling in 'todo' until the next sweep.
    recompute_ready(conn)
    return True


def decompose_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_assignee: Optional[str],
    children: list[dict],
    author: Optional[str] = None,
    auto_promote: bool = True,
) -> Optional[list[str]]:
    """Fan a triage task out into child tasks and promote the root to ``todo``.

    The root task stays alive and becomes the parent of every child —
    when all children reach ``done``, the root promotes to ``ready`` and
    its assignee (typically the orchestrator profile) wakes back up to
    judge completion or spawn more work.

    ``children`` is a list of dicts, each shaped like::

        {
            "title": "...",
            "body": "...",                     # optional
            "assignee": "profile-name",        # optional, None -> default fallback
            "parents": [0, 2],                 # indices into this same children list
        }

    Returns the list of created child task ids (in input order) on
    success. Returns ``None`` when:
      - The root task does not exist
      - The root task is not in ``triage``
      - A cycle would result (caller built a bad graph)

    Validation of titles/assignees happens inside the same write_txn as
    the inserts so a malformed entry aborts the whole decomposition
    cleanly (no orphan children).
    """
    if not children:
        return None
    if root_assignee is not None:
        root_assignee = _canonical_assignee(root_assignee)

    # Pre-validate the children list shape outside the txn. Cheap checks
    # that don't need DB access. Bad input aborts before we touch the DB.
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(
                    f"child[{idx}].parents[{p}] is not a valid index into children"
                )
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    # Detect cycles in the sibling parent graph (Kahn's topological sort).
    # link_tasks() calls _would_cycle() for every new edge; here we check
    # the entire sibling graph before touching the DB.  A cycle silently
    # deadlocks every involved child in 'todo' because recompute_ready()
    # can never promote them.
    _in_deg = [0] * len(children)
    _adj: list[list[int]] = [[] for _ in range(len(children))]
    for _i, _c in enumerate(children):
        for _p in (_c.get("parents") or []):
            _adj[_p].append(_i)
            _in_deg[_i] += 1
    _queue = [_i for _i in range(len(children)) if _in_deg[_i] == 0]
    _seen = 0
    while _queue:
        _node = _queue.pop()
        _seen += 1
        for _nb in _adj[_node]:
            _in_deg[_nb] -= 1
            if _in_deg[_nb] == 0:
                _queue.append(_nb)
    if _seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")

    # We do the full decomposition in a SINGLE write_txn so it's
    # atomic: either every child is created AND the root flips to
    # ``todo``, or nothing changes. We deliberately do NOT call any
    # kb helper that opens its own write_txn (create_task, link_tasks,
    # add_comment) from inside this block — see architecture.md
    # write_txn pitfalls. Instead we inline the INSERTs and
    # _append_event calls.
    now = int(time.time())
    child_ids: list[str] = []
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT id, status, tenant, workspace_kind, workspace_path "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if root_row is None:
            return None
        if root_row["status"] != "triage":
            return None
        tenant = root_row["tenant"]
        # Children inherit the root's workspace by default so a fan-out
        # of a code-gen task lands in the parent's project dir/worktree
        # rather than throwaway scratch tmp dirs. A child dict can still
        # override with its own 'workspace_kind' / 'workspace_path'.
        root_ws_kind = root_row["workspace_kind"] or "scratch"
        root_ws_path = root_row["workspace_path"]

        # Create children. Status is 'todo' regardless of parents — we
        # link them under the root AFTER creation so the dispatcher
        # sees a coherent state, and recompute_ready() at the end
        # promotes parent-free children to 'ready'.
        for idx, child in enumerate(children):
            new_id = _new_task_id()
            title = child["title"].strip()
            body = child.get("body")
            assignee = _canonical_assignee(child.get("assignee"))
            # Per-child override wins; otherwise inherit the root's
            # workspace. A child that sets workspace_kind without a path
            # falls back to the root path only when kinds match (so a
            # child can't accidentally point a 'dir' at the root's
            # worktree path or vice versa).
            child_ws_kind = child.get("workspace_kind") or root_ws_kind
            if child.get("workspace_path"):
                child_ws_path = child.get("workspace_path")
            elif child_ws_kind == "worktree":
                # Never share one worktree checkout between siblings: the
                # root's literal path would put every child in the same
                # directory on the first-dispatched sibling's branch, with
                # no lock — siblings can be promoted and dispatched
                # concurrently. Leave the path unset so dispatch
                # materializes a fresh <repo>/.worktrees/<child-id> per
                # child from the board anchor.
                child_ws_path = None
            elif child_ws_kind == root_ws_kind:
                child_ws_path = root_ws_path
            else:
                child_ws_path = None
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, workspace_kind, "
                " workspace_path, tenant, created_at, created_by) "
                "VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?)",
                (
                    new_id,
                    title,
                    body if isinstance(body, str) else None,
                    assignee,
                    child_ws_kind,
                    child_ws_path,
                    tenant,
                    now,
                    (author or "decomposer"),
                ),
            )
            _append_event(
                conn, new_id, "created",
                {"by": author or "decomposer", "from_decompose_of": task_id},
            )
            _inherit_notify_subs(conn, new_id, (task_id,), created_at=now)
            child_ids.append(new_id)

        # Link children to their sibling parents (within the decomposed graph).
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id = child_ids[p_idx]
                child_id = child_ids[idx]
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                    "VALUES (?, ?)",
                    (parent_id, child_id),
                )
                _append_event(
                    conn, child_id, "linked",
                    {"parent": parent_id, "child": child_id},
                )

        # Link the ROOT task as a child of every leaf child — i.e. the
        # root waits for the whole graph. Simpler than computing leaves:
        # link root under every child. Cycle-free because the root is
        # only ever a child here, never a parent of children.
        for cid in child_ids:
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                "VALUES (?, ?)",
                (cid, task_id),
            )

        # Flip the root: triage -> todo, set assignee to the orchestrator.
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        params.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

        # Audit comment + event on the root so the timeline shows the fan-out.
        if author and author.strip():
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Decomposed into "
                    + ", ".join(child_ids)
                    + ". Root will wake when all children complete.",
                    now,
                ),
            )
        _append_event(
            conn, task_id, "decomposed",
            {
                "child_ids": child_ids,
                "root_assignee": root_assignee,
            },
        )

    # Outside the write_txn: promote parent-free children to 'ready'
    # so the dispatcher picks them up on its next tick. Same pattern
    # specify_triage_task uses.  When auto_promote is False children
    # stay in 'todo' until the user manually promotes them — useful
    # for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    return child_ids


def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # If archive happened while a run was still in flight (e.g. user
        # archived a running task from the dashboard), close that run with
        # outcome='reclaimed' so attempt history isn't orphaned.
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    # ``archived`` parents no longer block children, same as ``done``.
    # Promote newly-unblocked dependents immediately instead of waiting
    # for a later dispatcher tick.
    recompute_ready(conn)
    return True


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Permanently remove an already-archived task and its related rows.

    Safety guard: only archived tasks can be deleted. Active / blocked / done
    tasks must be explicitly archived first so accidental data loss requires a
    second deliberate action.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row or row["status"] != "archived":
            return False
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
            (task_id, task_id),
        )
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete a task and cascade to all related rows.

    Because the schema does not use ``ON DELETE CASCADE`` foreign keys,
    we explicitly delete from child tables first, then the task row.
    This keeps the operation atomic (single ``write_txn``).

    Returns ``True`` if the task existed and was deleted, ``False``
    if the task was not found.
    """
    with write_txn(conn):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
    recompute_ready(conn)
    return True


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel containing ``path``, or ``None`` if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    try:
        return Path(out).expanduser().resolve()
    except Exception:
        return Path(out).expanduser()


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch_name}"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _git_common_dir(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    return Path(out).expanduser().resolve(strict=False)


def _git_dir(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-dir"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    return Path(out).expanduser().resolve(strict=False)


def _git_current_branch(path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    branch = (result.stdout or "").strip()
    return branch or None


def _is_linked_worktree_checkout(path: Path) -> bool:
    git_dir = _git_dir(path)
    common_dir = _git_common_dir(path)
    if git_dir is None or common_dir is None:
        return False
    return git_dir != common_dir


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for_worktree_target(path: Path) -> Optional[Path]:
    current = _nearest_existing_path(path).resolve(strict=False)
    while True:
        repo_root = _git_toplevel(current)
        if repo_root is not None:
            return repo_root
        if current == current.parent:
            return None
        current = current.parent


def _ensure_git_worktree(repo_root: Path, target: Path, branch_name: str) -> None:
    """Materialize ``target`` as a linked git worktree under ``repo_root``."""
    target = target.expanduser()
    repo_common = _git_common_dir(repo_root)
    if target.exists() and repo_common is not None:
        target_common = _git_common_dir(target)
        if target_common == repo_common:
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    if _git_branch_exists(repo_root, branch_name):
        cmd = ["git", "-C", str(repo_root), "worktree", "add", str(target), branch_name]
    else:
        cmd = [
            "git", "-C", str(repo_root), "worktree", "add", "-b", branch_name,
            str(target), "HEAD",
        ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )


def _resolve_worktree_workspace(
    task: Task, *, board: Optional[str] = None
) -> tuple[Path, str]:
    """Resolve + materialize a linked git worktree for ``task``.

    When ``task.workspace_path`` is unset, the anchor is the board's
    ``default_workdir`` (a persistent project checkout). This keeps every
    worktree task under a meaningful, board-owned repo — ``<repo>/.worktrees/
    <task-id>`` — instead of silently landing under the dispatcher's current
    working directory (which is whatever directory the gateway happened to be
    launched from, e.g. the Hermes checkout). If no anchor is configured
    anywhere, we fail loudly rather than guess.
    """
    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if not task.workspace_path:
        # Anchor on the board's configured default_workdir, not Path.cwd().
        # The dispatcher's CWD is incidental (gateway launch dir) and using it
        # scatters worktrees under whatever repo the gateway started in.
        board_slug = board if board else get_current_board()
        board_default = (read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>."
            )
        anchor = Path(board_default).expanduser()
        if not anchor.is_absolute():
            raise ValueError(
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo"
            )
        repo_root = _git_toplevel(anchor)
        if repo_root is None:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo"
            )
        target = repo_root / ".worktrees" / task.id
        _ensure_git_worktree(repo_root, target, branch_name)
        return target, branch_name

    requested = Path(task.workspace_path).expanduser()
    if not requested.is_absolute():
        raise ValueError(
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path"
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and _is_linked_worktree_checkout(requested):
        actual_branch = _git_current_branch(requested)
        if actual_branch == branch_name:
            return requested_resolved, actual_branch
        # The requested path is an existing checkout of a DIFFERENT
        # task's branch. Decompose children inherit the root's
        # workspace_path verbatim, so siblings all point here; reusing
        # the checkout as-is would run this task on the other task's
        # branch — silent cross-task provenance corruption, and unsafe
        # when siblings run concurrently. Fall back to a fresh worktree
        # of our own under the same repo.
        fallback_root = _repo_root_for_worktree_target(requested.parent)
        if fallback_root is not None:
            fallback = fallback_root / ".worktrees" / task.id
            if fallback.resolve(strict=False) != requested_resolved:
                _ensure_git_worktree(fallback_root, fallback, branch_name)
                return fallback.resolve(strict=False), branch_name
        # No repo to anchor a fallback on (or the occupied path IS this
        # task's own canonical worktree): keep the legacy reuse rather
        # than failing dispatch.
        return requested_resolved, actual_branch or branch_name

    repo_root = _git_toplevel(requested)
    if repo_root is not None and requested_resolved == repo_root:
        target = repo_root / ".worktrees" / task.id
        _ensure_git_worktree(repo_root, target, branch_name)
        return target, branch_name

    repo_root = _repo_root_for_worktree_target(requested.parent)
    if repo_root is None:
        raise ValueError(
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root"
        )
    _ensure_git_worktree(repo_root, requested, branch_name)
    return requested, branch_name


def resolve_workspace(task: Task, *, board: Optional[str] = None) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    - ``scratch``: a fresh dir under ``<board-root>/workspaces/<id>/``,
      where ``<board-root>`` is the active board's root. The path is the
      same for the dispatcher and every profile worker, so handoff is
      path-stable.
    - ``dir:<path>``: the path stored in ``workspace_path``.  Created
      if missing.  MUST be absolute — relative paths are rejected to
      prevent confused-deputy traversal where ``../../../tmp/attacker``
      resolves against the dispatcher's CWD instead of a meaningful
      root.  Users who want a kanban-root-relative workspace should
      compute the absolute path themselves.
    - ``worktree``: a real linked git worktree. If ``workspace_path`` names
      a repo root, Hermes treats it as an anchor and materializes a linked
      worktree at ``<repo>/.worktrees/<task-id>``. If ``workspace_path`` names
      a concrete target path, Hermes creates/reuses that linked worktree. With
      no ``workspace_path``, Hermes anchors on the board's ``default_workdir``
      and materializes ``<repo>/.worktrees/<task-id>`` per task; if no
      ``default_workdir`` is configured it raises rather than guessing from the
      dispatcher's CWD. When ``branch_name`` is empty, Hermes uses
      ``wt/<task-id>``.

    Persist the resolved path back to the task row via ``set_workspace_path``
    so subsequent runs reuse the same directory.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "scratch":
        if task.workspace_path:
            # Legacy scratch tasks that were set to an explicit path get the
            # same absolute-path guard as dir: — consistent with the
            # threat model.
            p = Path(task.workspace_path).expanduser()
            if not p.is_absolute():
                raise ValueError(
                    f"task {task.id} has non-absolute workspace_path "
                    f"{task.workspace_path!r}; workspace paths must be absolute"
                )
        else:
            p = workspaces_root(board=board) / task.id
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "dir":
        if not task.workspace_path:
            raise ValueError(
                f"task {task.id} has workspace_kind=dir but no workspace_path"
            )
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "worktree":
        p, _branch_name = _resolve_worktree_workspace(task, board=board)
        return p
    raise ValueError(f"unknown workspace_kind: {kind}")


def set_workspace_path(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(path), task_id),
        )


def set_branch_name(
    conn: sqlite3.Connection, task_id: str, branch_name: str
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET branch_name = ? WHERE id = ?",
            (str(branch_name), task_id),
        )


# ---------------------------------------------------------------------------
def schedule_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park a task in ``scheduled`` so it is waiting on time, not human input.

    ``scheduled`` tasks are intentionally not dispatchable; an external cron,
    human action, or automation can later call ``unblock_task`` to re-gate them
    to ``ready`` (or ``todo`` if parents are still incomplete).
    """
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="scheduled", status="scheduled",
            summary=reason,
        )
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="scheduled",
                summary=reason,
            )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


# Dispatcher (one-shot pass)
# ---------------------------------------------------------------------------

# After this many consecutive non-success attempts on a task/profile, the
# dispatcher stops retrying and parks the task in ``blocked`` with a reason so
# a human can investigate. Prevents retry storms when a worker repeatedly times
# out, crashes, or cannot spawn.
DEFAULT_FAILURE_LIMIT = 2
# Legacy alias — callers / tests still reference the old name.
DEFAULT_SPAWN_FAILURE_LIMIT = DEFAULT_FAILURE_LIMIT

# Max bytes to keep in a single worker log file. The dispatcher truncates
# and rotates on spawn if the file is larger than this at spawn time.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and call kanban_block/kanban_complete before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|auth\w*|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before the dispatcher
# re-spawns the worker. Without this, a task released by the rate-limit path
# would be re-spawned on the very next tick and immediately bounce off the
# same quota wall, burning a worker slot every tick for hours. The cooldown
# spaces retries out so the board keeps cheaply probing whether quota is back
# without thrashing. Overridable via ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``
# for operators who want a tighter/looser probe cadence.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

# Pattern matching a GitHub PR URL in task comments.
_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass."""

    reclaimed: int = 0
    promoted: int = 0
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """List of ``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids skipped because they have no assignee at all.
    Operator-actionable — usually a misfiled task waiting for routing."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Task ids that were unassigned in the DB and had
    ``kanban.default_assignee`` applied this tick before spawning (#27145).
    Surfaces the auto-assignment to telemetry / CLI / dashboard so the
    operator can see when the dispatcher is acting on the fallback rule
    rather than on explicit per-task assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids skipped because their assignee names a control-plane
    lane (a Claude Code terminal like ``orion-cc``) rather than a Hermes
    profile. Expected steady-state on multi-lane setups; NOT an
    operator-actionable failure. Tracked separately so health telemetry
    can distinguish "real stuck" (nothing spawned but spawnable work
    available) from "correctly idle" (nothing spawnable in the queue)."""
    skipped_reviewer_incapable: list[str] = field(default_factory=list)
    """Review / REWORK / RISK-VERDICT task ids skipped because their assignee
    is a real Hermes profile but lacks the ``terminal`` toolset, so it cannot
    run the verification work the card requires. Distinct from
    skipped_nonspawnable (assignee is not a Hermes profile at all) and from
    skipped_unassigned (no assignee). Operator-actionable ONLY as a routing
    signal: reassign the card to a terminal-capable reviewer (t_a2ef2ea2)."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """Tasks deferred this tick because their assignee is already at
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is
    ``(task_id, assignee, current_running_count)``. NOT an
    operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so
    telemetry / dashboards can show "this profile is busy" vs
    "task is genuinely stuck"."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed because no progress (heartbeat) was seen
    within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """Tasks skipped by the respawn guard, as ``(task_id, reason)`` pairs.

    Reasons: ``"blocker_auth"`` (quota/auth error — also auto-blocked),
    ``"recent_success"`` (completed run within guard window),
    ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released back to ``ready`` WITHOUT
    counting a failure. These never trip the circuit breaker — a long quota
    window just makes the task bounce cheaply until the window clears."""
    skipped_locked: bool = False
    """True when this tick was skipped because another process already held
    the board's dispatch lock (issue #35240). A losing dispatcher does no
    DB writes this tick — the lock holder is making progress on the same
    board. This is the steady-state signal that a single-writer guard is
    actively preventing two dispatchers from racing on ``kanban.db``."""
    skipped_block_gate: list[str] = field(default_factory=list)
    """Ready task ids skipped because the task has an unresolved block gate
    (t_fc1fdf31). A task that was worker/operator-blocked but somehow
    reached the ready queue (manual DB edit, missed event, code-path bug)
    is caught here: an audit event is logged and no spawn is attempted.
    This is the defense-in-depth guard for the dispatch tick."""


# Bounded registry of recently-reaped worker child exits, populated by the
# reap loop at the top of ``dispatch_once`` and consulted by
# ``detect_crashed_workers`` to classify a dead-pid task.
#
# Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``. We keep raw status
# so both ``os.WIFEXITED`` / ``os.WEXITSTATUS`` and ``os.WIFSIGNALED`` can
# be consulted. Entries are trimmed by age (and total size cap as a
# belt-and-braces against unbounded growth on exotic platforms).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status for later classification.

    Called from the reap loop in ``dispatch_once``. Safe to call many
    times; duplicate pids overwrite (pids can cycle, latest wins).
    """
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    # Age-based trim: drop entries older than the TTL.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    # Size cap as a final guard.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """Classify a recently-reaped worker by pid.

    Returns ``(kind, code)`` where ``kind`` is one of:

    * ``"clean_exit"`` — ``WIFEXITED`` with ``WEXITSTATUS == 0``. When the
      task is still ``running`` in the DB, this is a protocol violation
      (worker exited without calling ``kanban_complete`` / ``kanban_block``)
      and should be auto-blocked immediately — retrying will just loop.
    * ``"rate_limited"`` — ``WIFEXITED`` with status
      ``KANBAN_RATE_LIMIT_EXIT_CODE``. The worker bailed because the
      provider rate-limited / exhausted quota, NOT because the task failed.
      ``detect_crashed_workers`` releases the task back to ``ready`` without
      counting a failure, so a long quota window can't trip the breaker.
    * ``"nonzero_exit"`` — ``WIFEXITED`` with non-zero status. Real error.
    * ``"signaled"`` — ``WIFSIGNALED`` (OOM killer, SIGKILL, etc). Real crash.
    * ``"unknown"`` — pid was not in the reap registry (either reaped by
      something else, or died between reap tick and liveness check). Fall
      back to existing crashed-counter behavior.

    ``code`` is the exit status (for ``clean_exit`` / ``rate_limited`` /
    ``nonzero_exit``) or the signal number (for ``signaled``), or ``None``
    for ``unknown``.
    """
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    try:
        if os.WIFEXITED(raw):
            code = os.WEXITSTATUS(raw)
            if code == 0:
                return ("clean_exit", 0)
            if code == KANBAN_RATE_LIMIT_EXIT_CODE:
                return ("rate_limited", code)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw):
            return ("signaled", os.WTERMSIG(raw))
    except Exception:
        pass
    return ("unknown", None)


def reap_worker_zombies() -> "list[int]":
    """Reap all zombie children of this process without blocking.

    Returns the list of reaped PIDs. Safe to call when there are no
    children (returns []). No-op on Windows.
    """
    reaped: "list[int]" = []
    if os.name != "nt":
        try:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                _record_worker_exit(pid, status)
                reaped.append(pid)
        except Exception:
            pass
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Cross-platform: uses ``OpenProcess`` + ``WaitForSingleObject`` on
    Windows (via ``gateway.status._pid_exists``) and ``os.kill(pid, 0)``
    on POSIX. Returns False for falsy PIDs or on any OS error.

    **DO NOT** use ``os.kill(pid, 0)`` directly on Windows — Python's
    Windows ``os.kill`` treats ``sig=0`` as ``CTRL_C_EVENT`` (bpo-14484)
    and will broadcast it to the target's console group, potentially
    killing unrelated processes.

    **Zombie handling:** the existence check succeeds against zombie
    processes (post-exit, pre-reap) because the process table entry
    still exists. A worker that exits without being reaped by its
    parent would stay "alive" to the dispatcher forever. Dispatcher
    workers are started via ``start_new_session=True`` + intentional
    Popen handle abandonment, so init reaps them quickly — but during
    the window between exit and reap, we'd otherwise see stale "alive"
    signals. On Linux we peek at ``/proc/<pid>/status`` and treat
    ``State: Z`` as dead. On macOS we ask ``ps`` for the BSD ``stat``
    field and treat values containing ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    # Still here → process exists. Check for zombie on platforms
    # where we have a cheap, deterministic process-state probe.
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            # PermissionError shouldn't happen for our own children but
            # be defensive.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths."""
    import signal

    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info

    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    if not str(claim_lock).startswith(host_prefix):
        return info
    info["host_local"] = True

    kill = signal_fn if signal_fn is not None else (
        os.kill if hasattr(os, "kill") else None
    )
    if kill is None:
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Process is already gone — that's a successful termination, not a
        # survival. Leaving terminated=False here would make the reclaim guard
        # misread a dead worker as still-alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    for _ in range(10):
        if not _pid_alive(pid):
            info["terminated"] = True
            return info
        time.sleep(0.5)

    if _pid_alive(pid):
        try:
            # signal.SIGKILL doesn't exist on Windows; fall back to SIGTERM
            # (which maps to TerminateProcess via the stdlib shim).
            _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            kill(int(pid), _sigkill)
            info["sigkill"] = True
        except (ProcessLookupError, OSError):
            return info

    info["terminated"] = not _pid_alive(pid)
    return info


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming in this state would release the claim and let the dispatcher
    spawn a second worker while the first is still running — the duplication
    loop. Only host-local workers we actually signalled count: a non-local
    claim lock or a no-op attempt (no ``os.kill`` available) must fall through
    to the normal release path, since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("termination_attempted")
        and termination.get("host_local")
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records a ``reclaim_deferred``
    event so the hold is visible in ``hermes kanban tail``. The next dispatch
    tick retries the kill; this is self-correcting because not spawning a
    duplicate is what lets the throttled worker finally die.
    """
    grace = now + RECLAIM_DEFER_GRACE_SECONDS
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                (grace, run_id),
            )
        payload = {
            "reason": reason,
            "claim_lock": claim_lock,
            "claim_expires_now": grace,
        }
        payload.update(termination)
        _append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Called by long-running workers as a liveness signal orthogonal to
    the PID check. A worker that forks a long-lived child (train loop,
    video encode, web crawl) can have its Python still alive while the
    actual work process is stuck; periodic heartbeats catch that.

    Returns True on success, False if the task is not in a state that
    should be heartbeating (not running, or claim expired).
    """
    now = int(time.time())
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, task_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running' AND current_run_id = ?",
                (now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?",
                (now, run_id),
            )
        _append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    Sends SIGTERM, waits a short grace window, then SIGKILL. Emits a
    ``timed_out`` event and drops the task back to ``ready`` so the next
    dispatcher tick re-spawns it — unless the spawn-failure circuit
    breaker has already given up, in which case the task stays blocked
    where ``_record_spawn_failure`` parked it.

    Runs host-local: only tasks claimed by this host are candidates
    (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a
    test hook; defaults to ``os.kill`` on POSIX.
    """
    import signal
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt, not lifetime-of-task. ``tasks.started_at``
        # intentionally records the first time a task ever started, so retries
        # must be measured from the active task_runs row when present.
        elapsed = now - int(row["active_started_at"])
        if elapsed < int(row["max_runtime_seconds"]):
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        # SIGTERM then SIGKILL. Keep it simple: 5 s grace. Workers that
        # want a cleaner shutdown can install their own SIGTERM handler
        # before the grace expires.
        killed = False
        kill = signal_fn if signal_fn is not None else (
            os.kill if hasattr(os, "kill") else None
        )
        if kill is not None:
            try:
                kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            # Short polling wait — no time.sleep on the write txn.
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
            if _pid_alive(pid):
                try:
                    # signal.SIGKILL doesn't exist on Windows.
                    _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
                    kill(pid, _sigkill)
                    killed = True
                except (ProcessLookupError, OSError):
                    pass

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": int(row["max_runtime_seconds"]),
                    "sigkill": killed,
                }
                run_id = _end_run(
                    conn, tid,
                    outcome="timed_out", status="timed_out",
                    error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                    metadata=payload,
                )
                _append_event(
                    conn, tid, "timed_out", payload, run_id=run_id,
                )
                timed_out.append(tid)
        # Increment the unified failure counter. Outside the write_txn
        # above because ``_record_task_failure`` opens its own. If the
        # breaker trips, this flips the task ``ready → blocked`` and
        # emits a ``gave_up`` event on top of the ``timed_out`` we
        # already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed},
            )
    return timed_out


# Heartbeat staleness heartbeat gap — if a running task hasn't sent a
# heartbeat in this many seconds it's considered inactive regardless of
# the ``dispatch_stale_timeout_seconds`` threshold.  Hardcoded at 1 hour
# to match the original spec (">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks that show no progress (heartbeat) within the
    staleness window.

    A task is considered stale when BOTH of these hold:

    1. It has been running for longer than ``stale_timeout_seconds``
       (measured from the active run's ``started_at``, falling back to
       ``tasks.started_at`` on older runs).
    2. Its ``last_heartbeat_at`` is older than
       ``_STALE_HEARTBEAT_GAP_SECONDS`` (or NULL — never sent a heartbeat).

    On reclaim the task is reset to ``ready``, the run is closed with
    ``outcome='stale'``, and the host-local worker (if still running) is
    terminated.

    Only considers ``status='running'`` tasks. Blocked tasks are never
    candidates.  Returns the list of reclaimed task IDs.

    ``stale_timeout_seconds=0`` disables the check entirely (returns ``[]``
    immediately).  ``signal_fn`` is a test hook; defaults to ``os.kill``
    on POSIX.
    """
    if stale_timeout_seconds <= 0:
        return []


    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        # Skip if no started_at (shouldn't happen for running, but be safe).
        if row["active_started_at"] is None:
            continue

        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue  # not old enough to check

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue  # recent heartbeat → still alive

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        # Terminate the worker if it's still host-local.
        termination = _terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn,
        )

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": (
                    int(last_hb) if last_hb is not None else None
                ),
                "heartbeat_age_seconds": (
                    int(hb_age) if hb_age is not None else None
                ),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
            }
            payload.update(termination)

            run_id = _end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _append_event(
                conn, tid, "stale", payload, run_id=run_id,
            )
            reclaimed.append(tid)

        # Intentionally NOT calling _record_task_failure here. Stale reclaim
        # is dispatcher-side detection of an absent heartbeat; the task is
        # going straight back to ``ready`` for re-dispatch. Counting it as
        # a worker failure would let two legitimately-long-running tasks
        # (>4h without explicit heartbeat) trip the circuit breaker and
        # auto-block, even though no worker actually failed. The 'stale'
        # event already lives in task_events for auditability; that's the
        # right surface for "this happened" without conflating with the
        # spawn_failed / timed_out / crashed counters.

    return reclaimed


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message for grouping identical failures.

    Strips host-specific details (PIDs, timestamps) so that errors
    with the same root cause produce the same fingerprint.
    """
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# Empirically ~96% of "clean exit without a terminal tool call" tasks complete
# on a later run (a goal-mode finalize nudge, or the model simply emitting the
# tool call next time), so a protocol violation is NOT deterministic — give it a
# bounded retry before the breaker trips instead of blocking on the first hit.
#
# The budget is a violation-only STREAK, not a share of the unified
# ``consecutive_failures`` counter: it counts consecutive clean-exit protocol
# violations (derived from run history by ``_protocol_violation_streak``), so
# earlier timeouts / nonzero exits neither consume nor extend it, and a
# below-budget violation does not tick the unified counter either. A per-task
# ``max_retries`` overrides this bound — the same "task override wins"
# precedence ``_record_task_failure`` documents for every other failure kind.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# How far back to walk a task's closed runs when counting the violation
# streak. The streak trips at a handful of violations, so anything beyond a
# few dozen rows (violations interleaved with neutral rate-limited requeues)
# can only mean "way past the bound" anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks the task's closed runs newest-first — including the violation run
    ``detect_crashed_workers`` just closed — and counts how many in a row were
    clean-exit protocol violations:

    * ``rate_limited`` runs are neutral and skipped: a quota wall says nothing
      about the task, exactly as it is neutral for the unified
      ``consecutive_failures`` counter.
    * Any other closed run (completed, plain crash, timeout, spawn failure,
      reclaim, …) breaks the streak, so the bounded retry budget counts ONLY
      protocol violations — mixed failure kinds can neither consume nor
      extend it.

    Violation runs are recognized by the ``protocol_violation`` marker that
    ``detect_crashed_workers`` stamps into the run metadata; the violation
    error text is matched as a fallback for runs recorded before the marker
    existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "rate_limited":
            continue
        if outcome == "crashed":
            is_violation = False
            raw_meta = row["metadata"]
            if raw_meta:
                try:
                    is_violation = bool(
                        json.loads(raw_meta).get("protocol_violation")
                    )
                except (ValueError, TypeError):
                    is_violation = False
            if not is_violation:
                is_violation = "protocol violation" in (row["error"] or "")
            if is_violation:
                streak += 1
                continue
        break
    return streak


def detect_crashed_workers(conn: sqlite3.Connection) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Appends a ``crashed`` event and drops the task back to ``ready``.
    Different from ``release_stale_claims``: this checks liveness
    immediately rather than waiting for the claim TTL.

    Only considers tasks claimed by *this host* — PIDs from other hosts
    are meaningless here. The host-local check is enough because
    ``_default_spawn`` always runs the worker on the same host as the
    dispatcher (the whole design is single-host).

    When the reap registry shows the worker exited cleanly (rc=0) but
    the task was still ``running`` in the DB, treat it as a protocol
    violation (worker answered conversationally without calling
    ``kanban_complete`` / ``kanban_block``) and trip the circuit breaker
    on the first occurrence — retrying a worker whose CLI keeps
    returning 0 without a terminal transition just loops forever.

    When the reap registry shows the worker exited with the rate-limit
    sentinel (``KANBAN_RATE_LIMIT_EXIT_CODE``), the worker bailed on a
    provider quota wall, NOT a task failure. Such tasks are released back
    to ``ready`` WITHOUT counting a failure (so a long quota window can't
    trip the breaker) and stamped with a quota-blocker error so
    ``check_respawn_guard`` defers their respawn until the window clears.
    The ids are returned via the ``_last_rate_limited`` function attribute
    (the public return stays the crashed-only ``list[str]``).
    """
    crashed: list[str] = []
    rate_limited: list[str] = []
    # Per-crash details collected inside the main txn, used after it
    # closes to run ``_record_task_failure`` (which needs its own
    # write_txn so can't nest). ``protocol_violation`` flags the
    # clean-exit-but-still-running case, which is accounted against its
    # own bounded violation streak instead of the unified failure
    # counter (see the post-txn loop below).
    crash_details: list[tuple[str, int, str, bool, str]] = []
    # (task_id, pid, claimer, protocol_violation, error_text)
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, claim_lock, started_at FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
        for row in rows:
            # Only check liveness for claims owned by this host.
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Skip liveness check inside the launch-window grace period
            # so a freshly-spawned worker isn't reclaimed before its PID
            # is visible on /proc.
            started_at = row["started_at"] if "started_at" in row.keys() else None
            if started_at is not None:
                grace = _resolve_crash_grace_seconds()
                if time.time() - started_at < grace:
                    continue
            if _pid_alive(row["worker_pid"]):
                continue

            pid = int(row["worker_pid"])
            kind, code = _classify_worker_exit(pid)
            rate_limited_exit = False
            if kind == "clean_exit":
                # Worker subprocess returned 0 but its task is still
                # ``running`` in the DB — it exited without calling
                # ``kanban_complete`` / ``kanban_block``. Overwhelmingly the
                # work itself succeeded and only the paperwork was skipped, so
                # a retry usually completes; the corrective sentence below is
                # surfaced to the retry worker via the prior-attempt error in
                # ``build_worker_context`` (guidance approach from #61817).
                protocol_violation = True
                error_text = (
                    "worker exited cleanly (rc=0) without calling "
                    "kanban_complete or kanban_block — protocol violation. "
                    "If the prior run already did the work, verify it and "
                    "report the result via kanban_complete; a run that ends "
                    "without a terminal kanban call counts as failed no "
                    "matter what it did."
                )
                event_kind = "protocol_violation"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                    # Durable marker for _protocol_violation_streak: _end_run
                    # copies this payload into the run metadata, which is how
                    # the violation-only retry budget is derived later.
                    "protocol_violation": True,
                }
            elif kind == "rate_limited":
                # Worker bailed because the provider rate-limited / exhausted
                # quota (EX_TEMPFAIL sentinel). This is NOT a task failure —
                # the task is fine, the account just hit a wall. Release it
                # back to ``ready`` so the respawn guard defers it until the
                # quota window clears, and crucially do NOT count a failure
                # (skip ``_record_task_failure``) so a long quota window can't
                # trip the circuit breaker and permanently block the card.
                protocol_violation = False
                rate_limited_exit = True
                error_text = (
                    f"pid {pid} exited rate-limited (quota wall) — "
                    f"requeued without counting a failure"
                )
                event_kind = "rate_limited"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                }
            else:
                protocol_violation = False
                if kind == "nonzero_exit":
                    error_text = f"pid {pid} exited with code {code}"
                elif kind == "signaled":
                    error_text = f"pid {pid} killed by signal {code}"
                else:
                    error_text = f"pid {pid} not alive"
                event_kind = "crashed"
                event_payload = {"pid": pid, "claimer": row["claim_lock"]}
                if code is not None and kind != "unknown":
                    event_payload["exit_kind"] = kind
                    event_payload["exit_code"] = code

            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                # Rate-limited requeues are a clean release, not a crash —
                # record the run outcome as ``rate_limited`` so the board
                # history doesn't show a phantom crash for a quota wall.
                _run_outcome = "rate_limited" if rate_limited_exit else "crashed"
                run_id = _end_run(
                    conn, row["id"],
                    outcome=_run_outcome, status=_run_outcome,
                    error=error_text,
                    metadata=dict(event_payload),
                )
                _append_event(
                    conn, row["id"], event_kind,
                    event_payload,
                    run_id=run_id,
                )
                if rate_limited_exit:
                    # Stamp the failure-error column so ``check_respawn_guard``
                    # recognizes this as a quota blocker and defers the
                    # respawn until the window clears — WITHOUT touching
                    # ``consecutive_failures`` (that's the whole point: no
                    # breaker trip on a throttle).
                    conn.execute(
                        "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                        (error_text[:500], row["id"]),
                    )
                    rate_limited.append(row["id"])
                else:
                    if protocol_violation:
                        # Stamp the failure error now: a below-budget
                        # violation never reaches ``_record_task_failure``
                        # (which stamps this column for every other failure
                        # kind), yet the board UI and the retry worker's
                        # context still need the violation message + the
                        # corrective guidance it carries.
                        conn.execute(
                            "UPDATE tasks SET last_failure_error = ? "
                            "WHERE id = ?",
                            (error_text[:500], row["id"]),
                        )
                    crashed.append(row["id"])
                    crash_details.append(
                        (row["id"], pid, row["claim_lock"],
                         protocol_violation, error_text)
                    )
    # Outside the main txn: account each crashed task and maybe trip the
    # breaker (the task transitions ready → blocked with a ``gave_up`` event
    # on top of the event we already emitted).
    #
    # Protocol-violation crashes (clean exit, no terminal tool call) get a
    # BOUNDED retry, not an immediate trip: empirically ~96% of these tasks
    # complete on a later run (a goal-mode finalize nudge, or the model simply
    # emitting kanban_complete/kanban_block next time), so blocking on the first
    # occurrence just churned them through the respawn cycle. The retry budget
    # is a violation-only streak (``_protocol_violation_streak``): earlier
    # timeouts / nonzero exits neither consume nor extend it, and a
    # below-budget violation does not tick the unified
    # ``consecutive_failures`` counter, so the two budgets stay independent.
    # A per-task ``max_retries`` overrides the violation bound with the same
    # top precedence it has for every other failure kind. Systemic same-error
    # crashes still trip immediately.
    auto_blocked: list[str] = []
    if crash_details:
        # Fingerprint errors to detect systemic failures.
        _fp_counts: dict[str, int] = {}
        for _, _, _, _, err_text in crash_details:
            fp = _error_fingerprint(err_text)
            _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
        for tid, pid, claimer, protocol_violation, error_text in crash_details:
            if protocol_violation:
                streak = _protocol_violation_streak(conn, tid)
                trow = conn.execute(
                    "SELECT max_retries FROM tasks WHERE id = ?", (tid,),
                ).fetchone()
                if trow is None:
                    continue  # task deleted mid-loop
                task_override = (
                    trow["max_retries"] if "max_retries" in trow.keys() else None
                )
                violation_limit = (
                    int(task_override)
                    if task_override is not None
                    else _PROTOCOL_VIOLATION_FAILURE_LIMIT
                )
                if streak < violation_limit:
                    # Below budget: the task is already back at ``ready``
                    # (respawn allowed) with ``last_failure_error`` stamped.
                    # Deliberately no ``_record_task_failure`` call — a
                    # below-budget violation must not consume the unified
                    # failure budget, just as other failure kinds don't
                    # consume this one.
                    continue
                # Streak reached the bound: trip the breaker. ``force_trip``
                # skips the threshold resolution inside
                # ``_record_task_failure`` because the decision — including
                # the per-task ``max_retries`` override — was already made
                # against the violation streak above.
                tripped = _record_task_failure(
                    conn, tid,
                    error=error_text,
                    outcome="crashed",
                    failure_limit=violation_limit,
                    force_trip=True,
                    release_claim=False,
                    end_run=False,
                    event_payload_extra={
                        "pid": pid,
                        "claimer": claimer,
                        "protocol_violations": streak,
                        "protocol_violation_limit": violation_limit,
                    },
                )
                if tripped:
                    auto_blocked.append(tid)
                continue
            fp = _error_fingerprint(error_text)
            is_systemic = _fp_counts.get(fp, 0) >= 3
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer},
            )
            if tripped:
                auto_blocked.append(tid)
    # Stash auto-blocked ids on the function for the dispatch loop to pick up.
    # Keeps the public return type (``list[str]``) stable for direct callers
    # and tests that destructure the result; ``dispatch_once`` reads this
    # side-channel attribute to populate ``DispatchResult.auto_blocked``.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    # Same side-channel for rate-limited requeues — these did NOT count a
    # failure and are NOT crashes, so they stay out of the ``crashed`` return.
    detect_crashed_workers._last_rate_limited = rate_limited  # type: ignore[attr-defined]
    return crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
) -> bool:
    """Record a non-success outcome (spawn_failed / crashed / timed_out)
    and maybe trip the circuit breaker.

    Unified replacement for the old spawn-only ``_record_spawn_failure``.
    Every path that ends a task with a non-success outcome funnels
    through here so the ``consecutive_failures`` counter and the
    auto-block threshold stay consistent.

    Returns True when the task was auto-blocked (counter reached
    ``failure_limit``), False when it was just updated in place.

    Modes:

    * ``release_claim=True, end_run=True`` — spawn-failure path.
      Caller has a running task with an open run; this transitions
      it back to ``ready`` (or ``blocked`` when the breaker trips),
      releases the claim, and closes the run with ``outcome=<outcome>``.

    * ``release_claim=False, end_run=False`` — timeout/crash path.
      Caller has ALREADY flipped the task to ``ready`` and closed the
      run with the appropriate outcome. This just increments the
      counter; if the breaker trips, the task is re-transitioned
      ``ready → blocked`` and a ``gave_up`` event is emitted.

    ``event_payload_extra`` merges into the ``gave_up`` event payload
    when the breaker trips, so callers can include outcome-specific
    context (e.g. pid on crash, elapsed on timeout).

    Resolution order for the effective threshold:
      1. per-task ``max_retries`` if set (nothing else overrides)
      2. caller-supplied ``failure_limit`` (gateway passes the config
         value from ``kanban.failure_limit``; tests pass fixed values)
      3. ``DEFAULT_FAILURE_LIMIT``

    ``force_trip=True`` trips the breaker unconditionally, skipping the
    counter-vs-threshold comparison (the resolution order above is then
    only reported in the ``gave_up`` payload, not re-evaluated). Callers
    use it when they have already applied their own bounded-retry policy
    — e.g. the clean-exit protocol-violation streak in
    ``detect_crashed_workers``, which resolves the per-task
    ``max_retries`` override against the violation streak itself. The
    failure is still counted into ``consecutive_failures``.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    blocked = False
    with write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        failures = int(row["consecutive_failures"]) + 1
        cur_status = row["status"]

        # Per-task override wins over both caller-supplied and default
        # thresholds. None (the common case) falls through.
        task_override = (
            row["max_retries"] if "max_retries" in row.keys() else None
        )
        if task_override is not None:
            effective_limit = int(task_override)
            limit_source = "task"
        else:
            effective_limit = int(failure_limit)
            limit_source = "dispatcher"

        if force_trip or failures >= effective_limit:
            # Trip the breaker.
            if release_claim:
                # Spawn path: still running, also clear claim state.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('running', 'ready')",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready``
                # with claim cleared; just flip to blocked + update
                # counter fields.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('ready', 'running')",
                    (failures, error[:500], task_id),
                )
            run_id = None
            if end_run:
                # Only the spawn path has an open run to close.
                run_id = _end_run(
                    conn, task_id,
                    outcome="gave_up", status="gave_up",
                    error=error[:500],
                    metadata={
                        "failures": failures,
                        "trigger_outcome": outcome,
                        "effective_limit": effective_limit,
                        "limit_source": limit_source,
                    },
                )
            payload = {
                "failures": failures,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
                "error": error[:500],
                "trigger_outcome": outcome,
            }
            if event_payload_extra:
                payload.update(event_payload_extra)
            _append_event(
                conn, task_id, "gave_up", payload, run_id=run_id,
            )
            blocked = True
        else:
            # Below threshold.
            if release_claim:
                # Spawn path: transition running → ready + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready`` via
                # its own UPDATE. Just bookkeep the counter + last error.
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error[:500], task_id),
                )
            if end_run:
                # Spawn path: close the open run with outcome.
                run_id = _end_run(
                    conn, task_id,
                    outcome=outcome, status=outcome,
                    error=error[:500],
                    metadata={"failures": failures},
                )
                _append_event(
                    conn, task_id, outcome,
                    {"error": error[:500], "failures": failures},
                    run_id=run_id,
                )
            # Timeout/crash path's caller already emitted its own event.
    return blocked


# Backward-compat alias. Old name is referenced from tests and possibly
# third-party callers. New code should call ``_record_task_failure``.
def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    failure_limit: int = None,
) -> bool:
    return _record_task_failure(
        conn, task_id, error,
        outcome="spawn_failed",
        failure_limit=failure_limit,
        release_claim=True,
        end_run=True,
    )


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + emit a ``spawned`` event.

    The event's payload carries the pid so a human reading ``hermes kanban
    tail`` can correlate log lines with OS-level traces without opening
    the drawer.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (int(pid), task_id),
        )
        run_id = _current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (int(pid), run_id),
            )
        _append_event(conn, task_id, "spawned", {"pid": int(pid)}, run_id=run_id)


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on successful completion — a fresh
    success means the task + profile combination is working and any
    past failures are history. NOT called on spawn success anymore:
    a successful spawn proves the worker could start but says nothing
    about whether the run will succeed, so we need to let timeouts and
    crashes accumulate across spawn boundaries.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


# Legacy alias for test-code and anything else that still imports it.
_clear_spawn_failures = _clear_failure_counter


def check_respawn_guard(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready task in ``dispatch_once`` before any claim attempt.
    Returning a reason defers the spawn this tick; the task stays in
    ``ready`` and gets another chance on the next dispatcher tick.

    Checks in priority order:

    ``"rate_limit_cooldown"``
        The task's most recent run ended with the ``rate_limited`` outcome
        (a worker bailed on a provider quota wall via the EX_TEMPFAIL
        sentinel) within ``_resolve_rate_limit_cooldown_seconds()``. The
        quota almost certainly hasn't reset yet, so defer the respawn until
        the cooldown elapses — then allow a cheap probe. This is checked
        BEFORE ``blocker_auth`` because the rate-limit requeue stamps a
        quota-flavored ``last_failure_error`` that would otherwise match the
        auth-blocker regex and park the task forever (the rate-limit path
        never increments ``consecutive_failures``, so the breaker can't free
        it). Once the cooldown elapses the task falls through and respawns.

    ``"blocker_auth"``
        The task's last failure error matches a quota / authentication
        pattern. Retrying immediately is unlikely to help (rate limits
        reset on a timer; auth needs human action), so we defer to the
        next tick. The existing ``consecutive_failures`` counter still
        trips the auto-block circuit breaker after ``failure_limit``
        consecutive failures, so a persistent auth error eventually
        blocks via the normal path — but a transient 429 gets a few
        ticks of recovery first.

    ``"recent_success"``
        A completed run exists within ``_RESPAWN_GUARD_SUCCESS_WINDOW``
        seconds.  Useful work already succeeded for this task; wait for
        human review rather than immediately re-spawning. Bypassed when an
        explicit re-queue event (status change, promote, unblock, reclaim)
        arrives AFTER that completion — that's a deliberate re-run request.

    ``"active_pr"``
        A GitHub PR URL appears in a recent task comment (within
        ``_RESPAWN_GUARD_PR_WINDOW`` seconds).  A prior worker already
        opened a PR; re-spawning risks a duplicate PR on the same task.

    Stale / dead claim locks are NOT a guard reason — they are handled
    by ``release_stale_claims`` and ``detect_crashed_workers`` which
    reset the task to ``ready`` only after verifying the lock is
    genuinely dead (no live PID on this host).
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 1. Rate-limit cooldown. The most recent run ended ``rate_limited``
    #    (quota wall) — defer while inside the cooldown window, then allow a
    #    cheap probe. Must run BEFORE the blocker_auth regex check, because a
    #    rate-limit requeue stamps a quota-flavored last_failure_error that
    #    the regex would otherwise match → defer forever (no failure counter
    #    increment on this path means the breaker can never free it).
    #
    #    We look at the LATEST run only (ORDER BY ended_at DESC LIMIT 1): if a
    #    newer crash/completion superseded the rate-limit run, this guard
    #    no longer applies and the normal paths take over.
    rl_cooldown = _resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if (
        latest_run is not None
        and latest_run["outcome"] == "rate_limited"
    ):
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, and skip the
            # blocker_auth regex so the stamped rate-limit text doesn't
            # re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — allow the respawn. Return early so the
        # blocker_auth check below doesn't catch the rate-limit text we
        # stamped on the task; this path intentionally retries forever
        # (cheaply, spaced by the cooldown) until quota returns or a real
        # crash/completion supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.
    err = row["last_failure_error"]
    if err and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # 3. Completed run within guard window — proof of recent success.
    #    Exception: an explicit re-queue AFTER that success (an operator
    #    dragging done→ready, a dependency re-promotion, an unblock, a
    #    reclaim) is a deliberate "run it again" — honor it instead of
    #    deferring. Without this, a manual done→ready just sits there,
    #    silently held by the guard, until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, pr_cutoff),
    ).fetchall():
        if c["body"] and _RESPAWN_GUARD_PR_URL_RE.search(c["body"]):
            return "active_pr"

    return None


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one ready+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Used by the gateway- and CLI-embedded dispatchers' health telemetry to
    decide whether ``0 spawned`` is a "stuck" condition (real spawnable
    work waiting) or a "correctly idle" condition (only control-plane
    lanes like ``orion-cc`` / ``orion-research`` waiting on terminals
    that pull tasks via ``claim_task`` directly).

    Falls back to "any ready+assigned" if ``profile_exists`` is not
    importable (e.g. partial install) — preserves the old behavior so
    the warning still fires in degraded environments.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'ready' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one review+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Mirror of :func:`has_spawnable_ready` for the review column —
    used by the health telemetry to decide whether the dispatcher
    should have spawned a review agent.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'review' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Thin wrapper around :func:`_dispatch_once_locked`. It acquires a
    non-blocking, board-scoped dispatch lock (issue #35240) so that two
    dispatchers pointed at the same ``kanban.db`` — e.g. the service-
    managed gateway and a shell-spawned orphan that escaped the service
    cgroup — can never run a reclaim/spawn/write tick concurrently and
    race on WAL frames. The losing dispatcher returns an empty
    ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes;
    the holder is already making progress on the same board.

    The lock is keyed off the board's resolved DB path, so unrelated
    boards tick in parallel. See :func:`_dispatch_tick_lock` for the
    cross-process / cross-platform mechanics.
    """
    try:
        db_path = kanban_db_path(board=board)
    except Exception:
        # Path resolution should never fail, but if it somehow does we
        # must not lose the tick — fall through to an unguarded dispatch
        # rather than dropping work.
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
    with _dispatch_tick_lock(db_path) as held:
        if not held:
            return DispatchResult(skipped_locked=True)
        result = _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
        # Still under the dispatch lock: opportunistically truncate the WAL
        # at a coarse interval so it cannot grow unbounded between restarts.
        _maybe_checkpoint_wal(conn, db_path)
        return result


def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
) -> DispatchResult:
    """Run one dispatcher tick.

    Steps:
      1. Reclaim stale running tasks (TTL expired).
      2. Reclaim stale running tasks (no recent heartbeat).
      3. Reclaim crashed running tasks (host-local PID no longer alive).
      3. Promote todo -> ready where all parents are done.
      4. For each ready task with an assignee, atomically claim and call
         ``spawn_fn(task, workspace_path, board) -> Optional[int]``. The
         return value (if any) is recorded as ``worker_pid`` so subsequent
         ticks can detect crashes before the TTL expires.

    Spawn failures are counted per-task. After ``failure_limit`` consecutive
    failures the task is auto-blocked with the last error as its reason —
    prevents the dispatcher from thrashing forever on an unfixable task.

    ``max_spawn`` is a **live concurrency cap**, not a per-tick spawn budget:
    it counts tasks already in ``status='running'`` plus this tick's spawns
    against the limit. So ``max_spawn=4`` means "at most 4 workers running
    at any time across the whole board" — matching the gateway's stated
    intent ("limit concurrent kanban tasks"). With a per-tick interpretation
    a 60-second tick interval could grow concurrency by N every minute on a
    busy board and accumulate without bound.

    ``spawn_fn`` defaults to ``_default_spawn``. Tests pass a stub.
    ``board`` pins workspace/log/db resolution for this tick to a specific
    board. When omitted, the current-board resolution chain is used.
    """
    # Reap zombie children from previously spawned workers. See
    # reap_worker_zombies() for the full rationale.
    reap_worker_zombies()

    result = DispatchResult()
    result.reclaimed = release_stale_claims(conn)
    result.stale = detect_stale_running(
        conn, stale_timeout_seconds=stale_timeout_seconds,
    )
    result.crashed = detect_crashed_workers(conn)
    # detect_crashed_workers stashes protocol-violation auto-blocks on
    # itself so the public list-return stays stable. Pull them into the
    # DispatchResult here so telemetry / tests see the trip.
    _crash_auto_blocked = getattr(
        detect_crashed_workers, "_last_auto_blocked", []
    )
    if _crash_auto_blocked:
        result.auto_blocked.extend(_crash_auto_blocked)
    # Rate-limited requeues (quota wall, no failure counted) — surface for
    # telemetry / tests. These tasks went back to ``ready`` and the respawn
    # guard will defer them until the quota window clears.
    _crash_rate_limited = getattr(
        detect_crashed_workers, "_last_rate_limited", []
    )
    if _crash_rate_limited:
        result.rate_limited.extend(_crash_rate_limited)
    result.timed_out = enforce_max_runtime(conn)
    # Approval auto-clear (t_6009ccaa): promote approved-but-stuck cards out
    # of blocked/review/scheduled BEFORE dependency promotion, so a card
    # carrying REVIEW_VERDICT=APPROVED can never strand in ``blocked`` while
    # the fleet lock-gate flags it as a relapse every tick.
    apply_approvals(conn)
    result.promoted = recompute_ready(conn, failure_limit=failure_limit)

    # Count tasks already running so max_spawn enforces concurrency rather
    # than a per-tick spawn budget. See the docstring above for the full
    # rationale; the short version is that a 60-second tick interval with a
    # per-tick budget of N would grow concurrency by N every tick on a busy
    # board, since "running" tasks aren't reclaimed by completion alone —
    # they sit in status='running' until the worker calls
    # kanban_complete/kanban_block (or the dispatcher TTL-reclaims them).
    running_count = 0
    if max_spawn is not None:
        running_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )

    ready_rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    # Honour kanban.max_in_progress: if the board already has enough running
    # tasks, skip spawning this tick so slow workers (local LLMs,
    # resource-constrained hosts) can finish what they have before more tasks
    # pile up and time out.
    if max_in_progress is not None and ready_rows:
        in_progress = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()[0]
        if in_progress >= max_in_progress:
            return result
        # Only spawn enough to reach the cap, respecting max_spawn too.
        remaining = max_in_progress - in_progress
        if max_spawn is None or max_spawn > remaining:
            max_spawn = remaining
    spawned = 0
    # Per-profile concurrency cap (#21582): when set, track how many
    # workers each assignee already has in flight, and refuse to spawn
    # when this would push that assignee past the cap. Prevents
    # fan-out workloads from melting a single profile's local model /
    # API quota / browser pool while leaving other profiles idle.
    # Tasks blocked this way go to skipped_per_profile_capped (not
    # skipped_unassigned — the operator-actionable signal is different:
    # "this profile is busy, try again later" not "this needs routing").
    _per_profile_cap = max_in_progress_per_profile if (
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    _per_profile_running: dict[str, int] = {}
    if _per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            _per_profile_running[prow["assignee"]] = int(prow["n"])
    # Normalize default_assignee once: empty/whitespace string → None so the
    # rest of the loop can use ``if default_assignee:`` as a single check.
    # We also resolve profile_exists once here for the same reason.
    _default_assignee = (default_assignee or "").strip() or None
    _default_assignee_resolved = False
    if _default_assignee:
        try:
            from hermes_cli.profiles import profile_exists as _pe
            _default_assignee_resolved = bool(_pe(_default_assignee))
        except Exception:
            # Profiles module not importable (test stubs, exotic envs).
            # Trust the operator's config and try the assignment; the
            # downstream profile_exists check on the assigned row will
            # bucket it as nonspawnable if the profile genuinely isn't
            # there, with the existing diagnostic.
            _default_assignee_resolved = True
    for row in ready_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break

        # Block-gate audit (t_fc1fdf31): defense-in-depth — if a ready
        # task has an unresolved block gate (worker/operator ``kanban_block``
        # without a subsequent unblock), something went wrong: manual DB
        # edit, missed event, or a code-path bug. Log an audit event and
        # skip the spawn so no claim is sent for a blocked card.
        if _has_sticky_block(conn, row["id"]):
            result.skipped_block_gate.append(row["id"])
            if not dry_run:
                at = _claimer_id()
                with write_txn(conn):
                    _append_event(
                        conn, row["id"], "block_gate_audit",
                        {"origin": at, "task_id": row["id"]},
                    )
            continue

        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee: when the dispatcher hits an
            # unassigned ready task and an operator-configured fallback
            # exists, persist the assignment and proceed. This removes the
            # dashboard footgun where a task created without an assignee
            # parks in 'ready' forever even though the operator's intent
            # ("default") was perfectly clear (#27145). Mutating the row
            # (not just the in-memory view) keeps diagnostics and the
            # board state consistent: the task is now legitimately owned
            # by ``kanban.default_assignee``, not "unassigned but secretly
            # routed".
            if _default_assignee and _default_assignee_resolved:
                # Dry-run: show what WOULD happen (auto-assign + spawn) without
                # mutating the DB. Real run: mutate the row + emit the
                # 'assigned' event so the board state matches what just happened.
                if not dry_run:
                    try:
                        with write_txn(conn):
                            conn.execute(
                                "UPDATE tasks SET assignee = ? WHERE id = ? "
                                "AND (assignee IS NULL OR assignee = '')",
                                (_default_assignee, row["id"]),
                            )
                            _append_event(
                                conn, row["id"], "assigned",
                                {
                                    "assignee": _default_assignee,
                                    "source": "kanban.default_assignee",
                                },
                            )
                    except Exception:
                        _log.debug(
                            "kanban dispatch: failed to apply default_assignee=%r "
                            "to task %s",
                            _default_assignee, row["id"], exc_info=True,
                        )
                        result.skipped_unassigned.append(row["id"])
                        continue
                row_assignee = _default_assignee
                result.auto_assigned_default.append(row["id"])
            else:
                result.skipped_unassigned.append(row["id"])
                continue
        # Skip ready tasks whose assignee is not a real Hermes profile.
        # `_default_spawn` invokes ``hermes -p <assignee>`` which fails
        # with "Profile 'X' does not exist" when the assignee names a
        # control-plane lane (e.g. an interactive Claude Code terminal
        # like ``orion-cc`` / ``orion-research``) rather than a Hermes
        # profile. Those task lanes are pulled by terminals via
        # ``claim_task`` directly and should NEVER auto-spawn — the
        # subprocess would crash on startup, get reaped as a zombie,
        # the task would loop back to ``ready`` on next tick, and we'd
        # burn CPU forever (#kanban-dispatcher-crash-loop 2026-05-05).
        try:
            from hermes_cli.profiles import profile_exists  # local import: avoids cycle
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row_assignee):
            # Bucket separately from skipped_unassigned: the operator
            # cannot fix this by assigning a profile (the assignee IS the
            # intended owner — a terminal lane). Health telemetry uses
            # this distinction to suppress spurious "stuck" warnings on
            # multi-lane setups where the ready queue is steadily full
            # of human-pulled work.
            result.skipped_nonspawnable.append(row["id"])
            continue
        # Per-profile concurrency cap (#21582): even if there's global
        # headroom, refuse to spawn for an assignee that's already at
        # its in-flight cap. Prevents one profile's local model / API
        # quota / browser pool from being overwhelmed by a fan-out
        # while the global max_in_progress / max_spawn caps still allow
        # work on OTHER profiles.
        if _per_profile_cap is not None:
            current = _per_profile_running.get(row_assignee, 0)
            if current >= _per_profile_cap:
                result.skipped_per_profile_capped.append(
                    (row["id"], row_assignee, current)
                )
                continue
        # Respawn guard: refuse to re-spawn when useful work is already
        # in-flight/recent, or when the last failure is a deterministic
        # blocker (quota / auth). The guard defers the spawn this tick so
        # the task gets a chance to clear (rate limits often reset in
        # seconds-to-minutes); the existing consecutive_failures counter
        # still trips the auto-block circuit breaker after failure_limit
        # consecutive failures, so a persistent auth error eventually
        # blocks via the normal path rather than on first occurrence.
        guard_reason = check_respawn_guard(conn, row["id"])
        if guard_reason is not None:
            result.respawn_guarded.append((row["id"], guard_reason))
            # Emit an event so operators can see why the task was
            # skipped when reading `hermes kanban tail` — without
            # this the task appears stuck in ready with no diagnosis.
            if not dry_run:
                with write_txn(conn):
                    _append_event(
                        conn, row["id"], "respawn_guarded",
                        {"reason": guard_reason},
                    )
            continue
        if dry_run:
            result.spawned.append((row["id"], row_assignee, ""))
            # Increment per-profile counter even in dry_run so the cap
            # check sees the would-be spawn on subsequent iterations.
            # Without this, dry_run reports every task as spawnable and
            # under-reports the capped subset (#21582).
            if _per_profile_cap is not None and row_assignee:
                _per_profile_running[row_assignee] = (
                    _per_profile_running.get(row_assignee, 0) + 1
                )
            continue
        claimed = claim_task(conn, row["id"], ttl_seconds=ttl_seconds)
        if claimed is None:
            continue
        try:
            resolved_branch_name = None
            if claimed.workspace_kind == "worktree":
                workspace, resolved_branch_name = _resolve_worktree_workspace(claimed, board=board)
            else:
                workspace = resolve_workspace(claimed, board=board)
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        if claimed.workspace_kind == "worktree":
            set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            # Back-compat: older spawn_fn signatures accept only
            # (task, workspace). Test stubs in the suite rely on that.
            # Introspect the callable and pass `board` only when supported.
            import inspect
            try:
                sig = inspect.signature(_spawn)
                if "board" in sig.parameters:
                    pid = _spawn(claimed, str(workspace), board=board)
                else:
                    pid = _spawn(claimed, str(workspace))
            except (TypeError, ValueError):
                pid = _spawn(claimed, str(workspace))
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
            # NOTE: we intentionally do NOT reset consecutive_failures
            # here. A successful spawn proves the worker can start but
            # doesn't prove the run will succeed. Under unified
            # failure counting, resetting on spawn would let a task
            # that keeps timing out after spawn loop forever. The
            # counter is cleared only on successful completion (see
            # complete_task).
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
            # Track the new in-flight count for this profile so later
            # iterations in this same tick respect the per-profile cap
            # (#21582). Subsequent ticks re-query from the DB.
            if _per_profile_cap is not None and claimed.assignee:
                _per_profile_running[claimed.assignee] = (
                    _per_profile_running.get(claimed.assignee, 0) + 1
                )
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)

    # ---- review column dispatch ----
    # Review tasks are tasks that a worker moved to 'review' after
    # creating a PR.  The dispatcher spawns a review agent (loading
    # sdlc-review skill) that verifies the PR and either merges (→ done)
    # or rejects (→ back to running for the worker to fix).
    #
    # Same concurrency model as ready dispatch: review spawns count
    # against max_spawn alongside ready tasks, so the total number of
    # running workers stays bounded.
    review_rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'review' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    for row in review_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        try:
            from hermes_cli.profiles import profile_exists
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row["assignee"]):
            result.skipped_nonspawnable.append(row["id"])
            continue
        # Terminal-capability gate (t_a2ef2ea2): a review card needs a
        # worker that can actually run the verification (pytest/psql/gh).
        # The pre-existing profile_exists() check is blind to capability; a
        # real-but-terminal-less reviewer would spawn, fail on capability,
        # and re-block (fleet health 2026-07-28, Defect #1). Refuse and
        # emit an audit event instead.
        try:
            from hermes_cli.profiles import profile_has_terminal
        except Exception:
            profile_has_terminal = None  # type: ignore[assignment]
        if profile_has_terminal is not None and not profile_has_terminal(row["assignee"]):
            if not dry_run:
                with write_txn(conn):
                    _append_event(
                        conn, row["id"], "reviewer_capability",
                        {"assignee": row["assignee"],
                         "reason": "review card assigned to non-terminal profile"},
                    )
            result.skipped_reviewer_incapable.append(row["id"])
            continue
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            continue
        claimed = claim_review_task(conn, row["id"], ttl_seconds=ttl_seconds)
        if claimed is None:
            continue
        try:
            resolved_branch_name = None
            if claimed.workspace_kind == "worktree":
                workspace, resolved_branch_name = _resolve_worktree_workspace(claimed, board=board)
            else:
                workspace = resolve_workspace(claimed, board=board)
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        if claimed.workspace_kind == "worktree":
            set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        # Force-load the sdlc-review skill for review agents — it carries
        # the review logic (AC verification, merge, etc.). The mandatory
        # kanban lifecycle is already injected into every worker's system
        # prompt via KANBAN_GUIDANCE, so this is the only extra skill the
        # review agent needs.
        claimed.skills = ["sdlc-review"]
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            import inspect
            try:
                sig = inspect.signature(_spawn)
                if "board" in sig.parameters:
                    pid = _spawn(claimed, str(workspace), board=board)
                else:
                    pid = _spawn(claimed, str(workspace))
            except (TypeError, ValueError):
                pid = _spawn(claimed, str(workspace))
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.

    Defaults preserve the historical behavior: rotate at 2 MiB and keep one
    backup generation (``.log.1``). Operators with long-running workers can
    raise either value from ``config.yaml`` without changing dispatcher code.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    max_bytes = _positive_int(
        (kanban_cfg or {}).get("worker_log_rotate_bytes"),
        DEFAULT_LOG_ROTATE_BYTES,
        minimum=1,
    )
    backup_count = _positive_int(
        (kanban_cfg or {}).get("worker_log_backup_count"),
        DEFAULT_LOG_BACKUP_COUNT,
        minimum=0,
    )
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``.

    ``backup_count=1`` preserves the legacy single-generation behavior:
    ``<log>`` moves to ``<log>.1`` and any previous ``.1`` is replaced.
    Higher values shift older generations up to ``backup_count``.
    """
    try:
        if not log_path.exists():
            return
        if log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(
            backup_count,
            DEFAULT_LOG_BACKUP_COUNT,
            minimum=0,
        )
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            try:
                src.rename(_rotated_log_path(log_path, generation + 1))
            except OSError:
                pass
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Return the interpreter-bound Hermes CLI invocation."""
    # ``hermes_cli.main`` is the console-script target declared in
    # pyproject.toml, NOT a top-level ``hermes`` package — there is no
    # ``hermes`` package to import.
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    exts = [ext for ext in raw.split(";") if ext]
    return [command + ext for ext in exts]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    ``shutil.which`` follows platform search behavior. On Windows that can
    include the current directory before PATH for bare names, which is not a
    safe dispatcher primitive. This resolver only considers explicit PATH
    entries and skips empty / ``.`` entries.
    """
    path_env = os.environ.get("PATH", "")
    for raw_dir in path_env.split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if not os.path.isfile(candidate):
                continue
            if _IS_WINDOWS or os.access(candidate, os.X_OK):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """Return argv for a resolved Hermes executable path.

    Windows batch shims (`.cmd` / `.bat`) are not safe as argv[0] for
    worker launches because the argument vector includes task-derived
    values. Prefer the interpreter-bound module form whenever the resolved
    executable is only a shell shim.
    """
    if _IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv parts for ``Popen``.

    Tries in order:

    1. ``$HERMES_BIN`` — explicit operator override. Path-like values are
       normalized to absolute paths; bare command names keep normal PATH
       semantics and never prefer a same-directory file before ``PATH``.
    2. ``shutil.which("hermes")`` — the console-script shim, normalized to
       an absolute path. On Windows, ``which`` can return a relative
       ``.\\hermes.CMD`` when the current directory is on ``PATH``; directly
       launching batch shims is also unsafe with task-derived argv. The
       dispatcher therefore falls back to the interpreter-bound module form
       for implicit ``.cmd`` / ``.bat`` shims.
    3. ``sys.executable -m hermes_cli.main`` — fallback for setups where
       Hermes is launched from a venv and the ``hermes`` shim is not on
       the dispatcher's ``$PATH`` (cron, systemd ``User=`` services,
       launchd jobs, detached processes, etc.). Goes through the running
       interpreter so the result is independent of ``$PATH``.

    Mirrors ``gateway.run._resolve_hermes_bin`` for the same reason. Kept
    local (not imported from gateway) because ``hermes_cli`` sits below
    ``gateway`` in the dependency order.
    """
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    hermes_bin = _safe_which_no_cwd("hermes") if _IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    Kanban's ``max_runtime_seconds`` bounds the whole worker attempt. The
    terminal tool has its own default timeout via ``TERMINAL_TIMEOUT``; when
    the worker runtime is longer, raise only the child process default so a
    long command is not killed by the generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Dispatcher-spawned workers are launched from a long-lived gateway process,
    then the child re-enters the CLI with ``-p <assignee>``. Resolve the
    assignee profile's CLI tool surface at dispatch time and pass it as an
    explicit ``--toolsets`` pin so worker startup cannot fall back to a stale
    root/active-profile config or a profile whose top-level ``toolsets`` entry
    is only the kanban orchestrator surface. ``model_tools`` still appends the
    task-scoped kanban lifecycle tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        token = set_hermes_home_override(hermes_home)
        try:
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        finally:
            reset_hermes_home_override(token)
        return toolsets or None
    except Exception as exc:
        _log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


def _default_spawn(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the spawned child's PID so the dispatcher can detect crashes
    before the claim TTL expires. The child's completion is still observed
    via the ``complete`` / ``block`` transitions the worker writes itself;
    the PID check is a safety net for crashes, OOM kills, and Ctrl+C.

    ``board`` pins the child's kanban context to that board: the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root env
    vars all resolve to the same board the dispatcher claimed the task
    from. Workers cannot accidentally see other boards.
    """
    import subprocess
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name

    profile_arg = normalize_profile_name(task.assignee)

    prompt = f"work kanban task {task.id}"
    env = dict(os.environ)
    # The dispatcher is detached from every conversation. Its worker must never
    # inherit routing mirrored by a previous gateway turn, even before the first
    # session binds ContextVars in this process.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml
    # (fallback_providers, toolsets, agent settings, etc.) instead of the root
    # config.  Without this, `env = dict(os.environ)` copies only the parent's
    # env, and when the child process starts `hermes -p <name>` the
    # _apply_profile_override() runs *before* hermes_constants is imported.
    # If HERMES_HOME is absent from the child's env, get_hermes_home() falls
    # back to Path.home() / ".hermes" (the DEFAULT profile root), ignoring the
    # profile-specific config entirely.  Fixes profile-scoped fallback_providers
    # being invisible to kanban workers.
    from hermes_cli.profiles import resolve_profile_env
    try:
        env["HERMES_HOME"] = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # Profile dir doesn't exist — defer resolution to the CLI's
        # _apply_profile_override() via HERMES_PROFILE (set below).
        # This only happens in test fixtures where the isolated
        # HERMES_HOME never had profiles created.
        pass
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and
    # context-file loader anchor on the workspace, not whatever cwd the
    # dispatching gateway happened to export. The worker subprocess is already
    # launched with cwd=workspace, but TERMINAL_CWD takes precedence over the
    # process cwd in both file_tools._resolve_base_dir (#41312 — relative
    # write_file paths were landing in the gateway user's home) and
    # build_context_files_prompt (#34619 — workers loaded the dispatching
    # gateway's AGENTS.md instead of the task's). Setting it to the workspace
    # fixes both: the workspace is where the task's work actually happens.
    # Only pin a real, absolute directory — file_tools rejects relative /
    # sentinel TERMINAL_CWD values, so a non-dir workspace must NOT be set
    # here (leave the inherited value rather than write a meaningless one).
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode: the worker reads these and wraps its run in the
    # Ralph-style /goal judge loop (see cli.py quiet-mode path). Only set
    # when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    terminal_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_TIMEOUT"),
    )
    if terminal_timeout is not None:
        env["TERMINAL_TIMEOUT"] = terminal_timeout
    foreground_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"),
    )
    if foreground_timeout is not None:
        env["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = foreground_timeout
    # Pin the shared board + workspaces root the dispatcher resolved, so
    # that even when the worker activates a profile (`hermes -p <name>`
    # rewrites HERMES_HOME), its kanban paths still match the
    # dispatcher's. Belt-and-braces with the `get_default_hermes_root()`
    # resolution in `kanban_home()` — symmetric resolution is the norm,
    # but unusual symlink / Docker layouts are caught here too.
    env["HERMES_KANBAN_DB"] = str(kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(workspaces_root(board=board))
    # Board slug — the final defense-in-depth pin. If the worker ever
    # resolves kanban paths without the DB / workspaces env vars, the
    # board slug still forces it to the right directory.
    resolved_board = _normalize_board_slug(board) or get_current_board()
    env["HERMES_KANBAN_BOARD"] = resolved_board
    # HERMES_PROFILE is the author the kanban_comment tool defaults to.
    # `hermes -p <assignee>` activates the profile, but the env var is
    # what the tool reads — set it explicitly here so comments are
    # attributed correctly regardless of how the child loads config.
    env["HERMES_PROFILE"] = profile_arg

    # A worker must NEVER boot the interactive TUI: an inherited HERMES_TUI=1
    # or a `display.interface: tui` in the profile's config would send the
    # quiet chat run into the Ink TUI, whose no-TTY bail-out exits 0 without
    # doing the task → "protocol violation" on every attempt. `--cli` is the
    # highest-precedence interface override; dropping the env var covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        "--cli",
        # Worker subprocesses switch to a profile-scoped HERMES_HOME above,
        # so they see that profile's shell-hook allowlist instead of the
        # dispatcher's root allowlist. Pass --accept-hooks explicitly so
        # profile-local worker sessions still register configured hooks.
        "--accept-hooks",
    ]
    # Per-task force-loaded skills. Each name goes in its own
    # `--skills X` pair rather than a single comma-joined arg: the CLI
    # accepts both forms (action='append' + comma-split), but
    # per-name pairs are easier to read in `ps` output and avoid any
    # quoting ambiguity if a skill name ever contains unusual chars.
    if task.skills:
        for sk in task.skills:
            if sk:
                cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too when the override names one, so the worker
        # resolves the model against the intended backend instead of the
        # profile's configured provider (mixing model X with provider Y is
        # the classic mis-set that stalls a board).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    worker_toolsets = _resolve_worker_cli_toolsets(env.get("HERMES_HOME"))
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend([
        "chat",
        "-q", prompt,
    ])
    if task.goal_mode:
        # Goal-mode workers must take the fully-quiet single-query path:
        # the kanban goal-loop hook (_run_kanban_goal_loop_q) only runs in
        # cli.py's quiet branch. Without -Q the worker gets exactly one
        # turn, prints text, exits rc=0, and the dispatcher records a
        # protocol violation (incident 2026-06-09 t_d9cbe312).
        cmd.append("-Q")
    # Redirect output to a per-task log under <board-root>/logs/.
    # Anchored at the board root (not the shared kanban root), so
    # `hermes kanban log` on a specific board reads its own file and
    # logs don't collide across boards that happen to share task ids.
    log_dir = worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)

    # Use 'a' so a re-run on unblock appends rather than overwrites.
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    # NOTE: we intentionally do NOT close log_f here — we want Popen's
    # child process to keep writing after this function returns.  The
    # handle is kept alive by the child's inheritance.  The parent's
    # reference goes out of scope and is GC'd, but the OS-level FD stays
    # open in the child until the child exits.
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds. Exits cleanly
    on SIGINT / SIGTERM so ``hermes kanban daemon`` is systemd-friendly.
    ``stop_event`` (a :class:`threading.Event`) and ``on_tick`` (a
    callable receiving the :class:`DispatchResult`) are test hooks.
    """
    import signal
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only when running on the main thread — tests call
    # this inline from worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle)
                except (ValueError, OSError):
                    pass

    while not stop_event.is_set():
        try:
            with contextlib.closing(connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                try:
                    on_tick(res)
                except Exception:
                    pass
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# ---------------------------------------------------------------------------
# Worker context builder (what a spawned worker sees)
# ---------------------------------------------------------------------------

def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Return the full text a worker should read to understand its task.

    Order:
      1. Task title (mandatory).
      2. Task body (optional opening post, capped at 8 KB).
      3. Prior attempts on THIS task (most recent ``_CTX_MAX_PRIOR_ATTEMPTS``
         shown; older attempts collapsed into a one-line summary).
         Each attempt's ``summary`` / ``error`` / ``metadata`` capped at
         ``_CTX_MAX_FIELD_BYTES`` each.
      4. Structured handoff results of every done parent task. Prefers
         ``run.summary`` / ``run.metadata`` when the parent was executed
         via a run; falls back to ``task.result`` for older data. Same
         per-field cap.
      5. Cross-task role history for the assignee (most recent 5
         completed runs on other tasks).
      6. Comment thread (most recent ``_CTX_MAX_COMMENTS`` shown, older
         collapsed).

    All caps exist so worker prompts stay bounded even on pathological
    boards (retry-heavy tasks, comment storms). The per-field char cap
    prevents a single 1 MB summary from dominating context.
    """
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")

    # Single clock reading shared by every relative-age stamp below, so all
    # ages in one rendering are consistent ("3h ago" / "3h ago", not drifting
    # by the seconds it takes to build the block).
    _now = int(time.time())

    def _cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
        """Truncate a string to `limit` chars with a visible ellipsis."""
        if not s:
            return ""
        s = s.strip()
        if len(s) <= limit:
            return s
        return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"

    lines: list[str] = []
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        terminal_timeout = _worker_terminal_timeout_env(
            task.max_runtime_seconds,
            os.environ.get("TERMINAL_TIMEOUT"),
        )
        effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if effective_terminal_timeout:
            lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    lines.append("")

    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")

    # Attachments — files uploaded to this task (PDFs, source docs,
    # images). Surface the absolute on-disk path so the worker, which has
    # full file-tool access, can read them directly (read_file, terminal
    # `pdftotext`, etc.). On the local terminal backend the path resolves
    # as-is; remote backends need the kanban attachments dir mounted.
    attachments = list_attachments(conn, task_id)
    if attachments:
        lines.append("## Attachments")
        lines.append(
            "Files attached to this task. Read them with the file/terminal "
            "tools at the absolute paths below:"
        )
        for att in attachments:
            size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
            size_str = f", {size_kb} KB" if size_kb else ""
            ctype = f", {att.content_type}" if att.content_type else ""
            lines.append(f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`")
        lines.append("")

    # Prior attempts — show closed runs so a retrying worker sees the
    # history. Skip the currently-active run (that's this worker).
    # Cap at _CTX_MAX_PRIOR_ATTEMPTS most-recent closed runs; older
    # attempts get collapsed into a one-line marker so the worker knows
    # more exist without bloating the prompt.
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    # list_runs returns ascending by started_at; "most recent" = last N
    if len(all_prior) > _CTX_MAX_PRIOR_ATTEMPTS:
        omitted = len(all_prior) - _CTX_MAX_PRIOR_ATTEMPTS
        shown = all_prior[-_CTX_MAX_PRIOR_ATTEMPTS:]
        first_shown_idx = omitted + 1
    else:
        omitted = 0
        shown = all_prior
        first_shown_idx = 1
    if shown:
        lines.append("## Prior attempts on this task")
        if omitted:
            lines.append(
                f"_({omitted} earlier attempt{'s' if omitted != 1 else ''} "
                f"omitted; showing most recent {len(shown)})_"
            )
        for offset, run in enumerate(shown):
            idx = first_shown_idx + offset
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started_at))
            age = _relative_age(run.started_at, _now)
            ts_disp = f"{ts}, {age}" if age else ts
            profile = run.profile or "(unknown)"
            outcome = run.outcome or run.status
            lines.append(f"### Attempt {idx} — {outcome} ({profile}, {ts_disp})")
            if run.summary and run.summary.strip():
                lines.append(_cap(run.summary))
            if run.error and run.error.strip():
                lines.append(f"_error_: {_cap(run.error)}")
            if run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.append("")

    # Parents: prefer the most-recent 'completed' run's summary + metadata,
    # fall back to ``task.result`` when no run rows exist (legacy DBs,
    # or tasks completed before the runs table landed).
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    parent_ids = [r["parent_id"] for r in parent_rows]

    if parent_ids:
        wrote_header = False
        for pid in parent_ids:
            pt = get_task(conn, pid)
            if not pt or pt.status != "done":
                continue
            runs = [r for r in list_runs(conn, pid) if r.outcome == "completed"]
            runs.sort(key=lambda r: r.started_at, reverse=True)
            run = runs[0] if runs else None

            if not wrote_header:
                lines.append("## Parent task results")
                lines.append(
                    "_Handoffs from upstream tasks, captured when each parent "
                    "completed (see age below). These are point-in-time "
                    "snapshots, not live state — if a result drives your "
                    "current work and it's not recent, re-verify against the "
                    "source before acting on it as current._"
                )
                wrote_header = True

            # When did this parent's result get produced? Prefer the
            # completed run's end time; fall back to the task's completed_at.
            done_ts = None
            if run is not None and getattr(run, "ended_at", None):
                done_ts = run.ended_at
            elif pt.completed_at:
                done_ts = pt.completed_at
            age = _relative_age(done_ts, _now)
            lines.append(f"### {pid}" + (f" (completed {age})" if age else ""))

            body_lines: list[str] = []
            if run is not None and run.summary and run.summary.strip():
                body_lines.append(_cap(run.summary))
            elif pt.result:
                body_lines.append(_cap(pt.result))
            else:
                body_lines.append("(no result recorded)")

            if run is not None and run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    body_lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.extend(body_lines)
            lines.append("")

    # Cross-task role history: what else has THIS assignee completed
    # recently? Gives the worker implicit continuity — "I'm the reviewer
    # and my last three reviews focused on security" — without forcing
    # the user to wire anything into SOUL.md / MEMORY.md. Bounded to the
    # most recent 5 completed runs, excluding this task so the retry
    # section above isn't duplicated. Safe on assignee=None (skipped).
    if task.assignee:
        role_rows = conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = ? AND r.task_id != ? "
            "  AND r.outcome = 'completed' "
            "ORDER BY r.ended_at DESC LIMIT 5",
            (task.assignee, task_id),
        ).fetchall()
        if role_rows:
            lines.append(f"## Recent work by @{task.assignee}")
            for row in role_rows:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(int(row["ended_at"]))
                )
                age = _relative_age(row["ended_at"], _now)
                ts_disp = f"{ts}, {age}" if age else ts
                s = (row["summary"] or "").strip().splitlines()
                first = s[0][:200] if s else "(no summary)"
                lines.append(f"- {row['id']} — {row['title']} ({ts_disp}): {first}")
            lines.append("")

    # Comments: cap at the most-recent _CTX_MAX_COMMENTS so
    # comment-storm tasks don't blow out the worker's prompt. Older
    # comments summarised in a one-line marker like prior attempts.
    all_comments = list_comments(conn, task_id)
    if len(all_comments) > _CTX_MAX_COMMENTS:
        omitted_c = len(all_comments) - _CTX_MAX_COMMENTS
        shown_c = all_comments[-_CTX_MAX_COMMENTS:]
    else:
        omitted_c = 0
        shown_c = all_comments
    if shown_c:
        lines.append("## Comment thread")
        if omitted_c:
            lines.append(
                f"_({omitted_c} earlier comment{'s' if omitted_c != 1 else ''} "
                f"omitted; showing most recent {len(shown_c)})_"
            )
        for c in shown_c:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            age = _relative_age(c.created_at, _now)
            ts_disp = f"{ts}, {age}" if age else ts
            # Render author with explicit "comment from worker" framing so
            # operator-controlled HERMES_PROFILE values like "hermes-system"
            # or "operator" can't be misread by the next worker as a system
            # directive above the (attacker-influenceable) comment body.
            # Defense-in-depth — the LLM-controlled author-forgery surface
            # was already closed in #22435. See #22452.
            safe_author = (c.author or "").replace("`", "")
            lines.append(f"comment from worker `{safe_author}` at {ts_disp}:")
            lines.append(_cap(c.body, _CTX_MAX_COMMENT_BYTES))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Stats + SLA helpers
# ---------------------------------------------------------------------------

def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, plus the oldest ``ready`` age in
    seconds (the clearest staleness signal for a router or HUD).
    """
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        by_assignee.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
    }


def _to_epoch(val) -> Optional[int]:
    """Normalise a timestamp to unix epoch seconds.

    Accepts ints (pass-through), numeric strings, and ISO-8601 strings.
    Returns ``None`` for ``None`` / empty values.
    """
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c = _to_epoch(task.created_at)
    _s = _to_epoch(task.started_at)
    _co = _to_epoch(task.completed_at)
    age_since_created = now - _c if _c is not None else None
    age_since_started = now - _s if _s is not None else None
    time_to_complete = (
        _co - (_s or _c) if _co is not None else None
    )
    return {
        "created_age_seconds": age_since_created,
        "started_age_seconds": age_since_started,
        "time_to_complete_seconds": time_to_complete,
    }


# ---------------------------------------------------------------------------
# Notification subscriptions (used by the gateway kanban-notifier)
# ---------------------------------------------------------------------------

def _encode_notify_delivery_metadata(
    metadata: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """Serialize platform send metadata stored on notification subscriptions."""
    if not isinstance(metadata, Mapping):
        return None
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[str(key)] = value
    if not clean:
        return None
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _decode_notify_delivery_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): value
        for key, value in data.items()
        if isinstance(value, (str, int, float, bool))
    }


def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    chat_type: Optional[str] = None,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Register a gateway source that wants terminal-state notifications
    for ``task_id``. Idempotent on (task, platform, chat, thread).

    New subscriptions start "caught up": ``last_event_id`` snaps to the
    task's current ``MAX(task_events.id)`` at creation instead of the
    schema default 0. A cursor of 0 on an already-active task made the
    gateway notifier replay every historical terminal event on its next
    tick — and with many stale subs, a single boot-time burst of 100+
    messages (issue #29905). Subscribers only want events that occur
    AFTER they subscribe; the gateway/tool auto-subscribe paths run at
    task creation, where the snapshot is 0 anyway.
    """
    now = int(time.time())
    metadata_json = _encode_notify_delivery_metadata(delivery_metadata)
    with write_txn(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, chat_type, thread_id, user_id,
                 notifier_profile, delivery_metadata, created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))
            """,
            (
                task_id,
                platform,
                chat_id,
                chat_type,
                thread_id or "",
                user_id,
                notifier_profile,
                metadata_json,
                now,
                task_id,
            ),
        )
        if chat_type:
            # Self-heal rows created before chat_type was persisted.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET chat_type = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (chat_type IS NULL OR chat_type = '')
                """,
                (chat_type, task_id, platform, chat_id, thread_id or ""),
            )
        if notifier_profile:
            # Self-heal legacy rows that predate notifier ownership by
            # backfilling only when the existing value is unset.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET notifier_profile = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (notifier_profile IS NULL OR notifier_profile = '')
                """,
                (notifier_profile, task_id, platform, chat_id, thread_id or ""),
            )
        if metadata_json:
            # A duplicate subscribe from the same chat/thread should refresh
            # the routing anchor. Telegram DM-topic notifications need the
            # latest reply anchor to stay inside the visible topic lane.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET delivery_metadata = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                """,
                (metadata_json, task_id, platform, chat_id, thread_id or ""),
            )


def list_notify_subs(
    conn: sqlite3.Connection, task_id: Optional[str] = None,
) -> list[dict]:
    if task_id is not None:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kanban_notify_subs").fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        if "delivery_metadata" in item:
            item["delivery_metadata"] = _decode_notify_delivery_metadata(
                item.get("delivery_metadata")
            )
        out.append(item)
    return out


def count_notify_subs(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> int:
    """Count ``kanban_notify_subs`` rows via a read-only connection.

    Cheap probe for the gateway notifier's zero-subscription early exit:
    unlike :func:`connect`, this never creates the DB file, never runs
    schema init/migration, and never opens the database writable (no
    write locks, no checkpoints — though a read-only open of a WAL
    database may still create the ``-shm``/``-wal`` sidecars, it cannot
    write table content). Rows in a not-yet-checkpointed WAL are
    visible, so a freshly added subscription is never missed. A missing
    DB, or a legacy DB that predates the subscriptions table, counts as
    zero. Path resolution matches :func:`connect` (explicit ``db_path``,
    else ``board`` via :func:`kanban_db_path`). Raises
    :class:`sqlite3.Error` when the DB exists but cannot be read
    (locked, corrupt); callers choose their own fallback.
    """
    path = db_path if db_path is not None else kanban_db_path(board=board)
    if not path.exists():
        return 0
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM kanban_notify_subs"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        )
    return cur.rowcount > 0


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` for a given subscription.

    Only events with ``id > last_event_id`` are returned. The subscription's
    cursor is NOT advanced here; call :func:`advance_notify_cursor` after
    the gateway has successfully delivered the notifications.
    """
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs "
        "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        (task_id, platform, chat_id, thread_id or ""),
    ).fetchone()
    if row is None:
        return 0, []
    cursor = int(row["last_event_id"])
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out: list[Event] = []
    max_id = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(Event(
            id=r["id"], task_id=r["task_id"], kind=r["kind"],
            payload=payload, created_at=r["created_at"],
            run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
        ))
        max_id = max(max_id, int(r["id"]))
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen notification events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``. When events are returned,
    ``kanban_notify_subs.last_event_id`` has already been advanced to
    ``new_cursor`` inside a ``BEGIN IMMEDIATE`` transaction. That makes the
    notifier's read/claim step single-owner across multiple gateway watcher
    processes pointed at the same board DB: concurrent watchers serialize on
    SQLite's writer lock, and only the first process sees and claims a given
    event range.

    Callers should send the claimed events, then either leave the cursor at
    ``new_cursor`` on success or call :func:`rewind_notify_cursor` if delivery
    failed before any terminal unsubscribe removed the row.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        ).fetchone()
        if row is None:
            return 0, 0, []
        old_cursor = int(row["last_event_id"])
        new_cursor, events = unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            kinds=kinds,
        )
        if not events:
            return old_cursor, old_cursor, []
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or "", int(old_cursor)),
        )
        return old_cursor, new_cursor, events


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or ""),
        )


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a notification claim when delivery fails.

    The CAS guard only rewinds if no later notifier advanced the row after our
    claim. This keeps retry behavior for transient send failures without
    clobbering newer progress.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (
                int(old_cursor), task_id, platform, chat_id, thread_id or "",
                int(claimed_cursor),
            ),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Broker / orchestrator control-loop slice (INERT)
# ---------------------------------------------------------------------------
#
# Scope of this slice, deliberately small:
#
#   1. typed validation for two new event kinds;
#   2. a per-board consumer cursor + atomic claim, reusing the
#      ``kanban_notify_subs`` discipline (BEGIN IMMEDIATE + CAS);
#   3. dedup of terminal ``task_runs`` into exactly one typed completion event;
#   4. a PURE route decision function.
#
# Authority: native per-board ``task_events`` / ``task_runs`` / ``tasks`` only.
# There is no parallel queue, no lease file, no JSON cursor and no Markdown
# anywhere in this path. ``task_events.id`` is the cursor, the partial UNIQUE
# index is the dedup, and ``task_runs`` is the completion record — each of them
# already transactional with the rows it orders.
#
# Inertness: nothing here spawns a worker, invokes a provider, or resumes a
# session. :func:`decide_route` is pure — it takes rows and returns a decision,
# performing no I/O — and every decision it can produce carries
# ``spawn = False``. Acting on a decision is a separate, separately-approved
# change.

#: Emitted when a terminal ``task_runs`` row has been observed by the loop.
BROKER_EVENT_WORKER_COMPLETION = "worker_completion_observed"
#: Emitted when the loop has decided what should happen next about a task.
BROKER_EVENT_ROUTE_DECIDED = "orchestrator_route_decided"
#: Emitted when a run is mapped to an already-known worker session.
BROKER_EVENT_SESSION_MAPPED = "worker_session_mapped"

BROKER_EVENT_KINDS = frozenset(
    {
        BROKER_EVENT_WORKER_COMPLETION,
        BROKER_EVENT_ROUTE_DECIDED,
        BROKER_EVENT_SESSION_MAPPED,
    }
)

# --- worker-session provenance -------------------------------------------
#
# How a run came to be associated with a worker session. A session id on its
# own says nothing about whether it is safe to hand work back to; the source
# does. Only *declared* sources are eligible to drive a CONTINUE.

#: The dispatcher recorded the session when it spawned the worker.
SESSION_SOURCE_DISPATCHER = "dispatcher_spawn"
#: An operator explicitly declared the mapping.
SESSION_SOURCE_OPERATOR = "operator_declared"
#: Derived from observation (log scraping, workspace matching, …). Recorded for
#: visibility, never trusted to drive work.
SESSION_SOURCE_INFERRED = "inferred"

VALID_SESSION_SOURCES = frozenset(
    {SESSION_SOURCE_DISPATCHER, SESSION_SOURCE_OPERATOR, SESSION_SOURCE_INFERRED}
)

#: Sources whose mapping may drive a ``continue``. Inference never qualifies.
CONTINUE_ELIGIBLE_SESSION_SOURCES = frozenset(
    {SESSION_SOURCE_DISPATCHER, SESSION_SOURCE_OPERATOR}
)

#: Terminal ``task_runs.outcome`` values, verbatim from the schema comment.
TERMINAL_RUN_OUTCOMES = frozenset(
    {
        "completed",
        "blocked",
        "crashed",
        "timed_out",
        "spawn_failed",
        "gave_up",
        "reclaimed",
    }
)

#: Outcomes that may be retried in the worker's existing session.
RETRYABLE_RUN_OUTCOMES = frozenset(
    {"crashed", "timed_out", "spawn_failed", "reclaimed"}
)

ROUTE_CONTINUE = "continue"
ROUTE_REVIEW = "review"
ROUTE_BLOCK = "block"
ROUTE_CLOSE = "close"

VALID_ROUTES = frozenset({ROUTE_CONTINUE, ROUTE_REVIEW, ROUTE_BLOCK, ROUTE_CLOSE})

# Status vocabulary is reconciled against the native ``VALID_STATUSES``
# (triage, todo, scheduled, ready, running, blocked, review, done, archived).
# An earlier version of this slice invented ``review-required``, which is not a
# native status and could never match; it is removed.
#
# Note there is no native ``cancelled`` or ``in_progress`` status — ``running``
# is the native in-flight status. Anything outside ``VALID_STATUSES`` is
# treated as unknown and routed to REVIEW rather than interpreted, so a future
# status added elsewhere cannot silently acquire a routing meaning here.
_CLOSED_TASK_STATUSES = frozenset({"done", "archived"})
_REVIEW_TASK_STATUSES = frozenset({"review", "triage"})
_BLOCKED_TASK_STATUSES = frozenset({"blocked"})
#: Statuses this router understands. Must stay a subset of VALID_STATUSES.
_ROUTABLE_TASK_STATUSES = (
    _CLOSED_TASK_STATUSES
    | _REVIEW_TASK_STATUSES
    | _BLOCKED_TASK_STATUSES
    | frozenset({"todo", "scheduled", "ready", "running"})
)

#: Default bound on how many rows one pass may fetch or drain.
BROKER_DEFAULT_LIMIT = 200

#: Hard ceiling. A caller cannot raise its own bound past this — an unbounded
#: pass is the failure mode this whole slice is built to avoid, so the bound is
#: enforced rather than merely defaulted.
BROKER_MAX_LIMIT = 1000


def _enforce_limit(limit: int) -> int:
    """Clamp a caller-supplied bound into ``[1, BROKER_MAX_LIMIT]``.

    L1: a fractional limit is **rejected**, not truncated. ``int(2.7)`` silently
    became 2, so a caller asking for a bound the system cannot honour got a
    different bound without being told. A whole-valued float (``5.0``) is
    accepted as the integer it exactly represents; ``bool`` is not an integer
    for this purpose.
    """
    if isinstance(limit, bool):
        raise ValueError(f"limit must be an integer, got bool {limit!r}")
    if isinstance(limit, int):
        value = limit
    elif isinstance(limit, float):
        if not limit.is_integer():
            raise ValueError(f"limit must be a whole number, got {limit!r}")
        value = int(limit)
    else:
        raise ValueError(f"limit must be an integer, got {limit!r}")
    if value < 1:
        raise ValueError(f"limit must be >= 1, got {value}")
    return min(value, BROKER_MAX_LIMIT)

#: Quarantine kind for legacy duplicate completion rows. Renamed, never
#: deleted, so a repair cannot lose data.
BROKER_EVENT_WORKER_COMPLETION_DUPLICATE = "worker_completion_observed_duplicate"


#: Name of the exactly-once guard, so health checks and repairs agree on it.
COMPLETION_DEDUP_INDEX = "idx_events_completion_once"


class BrokerEventValidationError(ValueError):
    """A broker event payload does not match its declared schema."""


class BrokerUnsafeError(RuntimeError):
    """The board cannot provide exactly-once folding.

    Raised instead of silently degrading. See :func:`broker_health`.
    """


@dataclass(frozen=True)
class BrokerHealth:
    """Queryable health of the broker substrate on one board."""

    dedup_index_present: bool
    #: Duplicate completion rows visible right now (0 when the index holds).
    duplicate_completion_rows: int
    degraded_reason: Optional[str] = None

    @property
    def healthy(self) -> bool:
        return self.dedup_index_present and self.duplicate_completion_rows == 0

    @property
    def safe_to_schedule(self) -> bool:
        """Whether a consumer may be scheduled/activated against this board.

        Without the dedup index, exactly-once folding is not enforceable — a
        multiprocess race produces duplicate completion events. That is a
        no-schedule condition, not a warning.
        """
        return self.dedup_index_present

    def to_json(self) -> dict:
        return {
            "dedup_index_present": self.dedup_index_present,
            "duplicate_completion_rows": self.duplicate_completion_rows,
            "degraded_reason": self.degraded_reason,
            "healthy": self.healthy,
            "safe_to_schedule": self.safe_to_schedule,
        }


def completion_dedup_index_present(conn: sqlite3.Connection) -> bool:
    """Live check: does the exactly-once guard exist on this board?"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name = ?",
        (COMPLETION_DEDUP_INDEX,),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class ConsumerFreshness:
    """How far behind a named consumer is, and how stale that makes it."""

    consumer: str
    cursor: int
    max_event_id: int
    #: Events past the cursor right now.
    lag: int
    #: Seconds since the consumer last advanced, or None if it never has.
    seconds_since_advance: Optional[int]
    #: Age of the oldest unconsumed event, or None when caught up.
    oldest_unconsumed_age_seconds: Optional[int]

    def stale(self, max_lag_seconds: int) -> bool:
        age = self.oldest_unconsumed_age_seconds
        return age is not None and age > max_lag_seconds

    def to_json(self) -> dict:
        return {
            "consumer": self.consumer,
            "cursor": self.cursor,
            "max_event_id": self.max_event_id,
            "lag": self.lag,
            "seconds_since_advance": self.seconds_since_advance,
            "oldest_unconsumed_age_seconds": self.oldest_unconsumed_age_seconds,
        }


def consumer_freshness(
    conn: sqlite3.Connection,
    *,
    consumer: str,
    now: Optional[int] = None,
) -> Optional[ConsumerFreshness]:
    """Freshness for one named consumer, or None if it is not registered.

    Lag is measured in *events* and in *seconds of unconsumed backlog*, because
    a consumer that stopped is indistinguishable from an idle one by cursor
    position alone — the age of the oldest thing it has not read is what makes
    a stall visible.
    """
    row = conn.execute(
        "SELECT last_event_id, updated_at FROM kanban_broker_subs WHERE consumer = ?",
        (consumer,),
    ).fetchone()
    if row is None:
        return None
    stamp = int(now if now is not None else time.time())
    cursor = int(row["last_event_id"])
    top = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()
    max_id = int(top["m"] if top and top["m"] else 0)
    lag_row = conn.execute(
        "SELECT COUNT(*) AS c, MIN(created_at) AS oldest FROM task_events WHERE id > ?",
        (cursor,),
    ).fetchone()
    lag = int(lag_row["c"] or 0)
    oldest = lag_row["oldest"]
    return ConsumerFreshness(
        consumer=consumer,
        cursor=cursor,
        max_event_id=max_id,
        lag=lag,
        seconds_since_advance=(
            stamp - int(row["updated_at"]) if row["updated_at"] is not None else None
        ),
        oldest_unconsumed_age_seconds=(
            stamp - int(oldest) if oldest is not None else None
        ),
    )


@dataclass(frozen=True)
class NotificationProjection:
    """A testable, transport-independent view of one routing decision.

    Kept separate from the rendered string so a consumer can assert on typed
    fields rather than parse text, and so a future transport can format it
    differently without changing the decision.
    """

    task_id: str
    run_id: int
    route: str
    reason: str
    outcome: str
    session_id: Optional[str]
    provider: Optional[str]
    seat: Optional[str]
    spawn: bool
    text: str

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "route": self.route,
            "reason": self.reason,
            "outcome": self.outcome,
            "session_id": self.session_id,
            "provider": self.provider,
            "seat": self.seat,
            "spawn": self.spawn,
            "text": self.text,
        }


def project_notification(decision: "RouteDecision") -> NotificationProjection:
    """Project a decision into its typed notification form."""
    return NotificationProjection(
        task_id=decision.task_id,
        run_id=decision.run_id,
        route=decision.route,
        reason=decision.reason,
        outcome=decision.outcome,
        session_id=decision.session_id,
        provider=decision.provider,
        seat=decision.seat,
        spawn=decision.spawn,
        text=render_route_notification(decision),
    )


def broker_health(conn: sqlite3.Connection) -> BrokerHealth:
    """Explicit, queryable health. The live schema is the source of truth.

    A persisted marker is also written by :func:`_create_completion_dedup_index`
    when the index could not be created, so an operator can see *why* a board
    degraded even after a later connect repairs it. The live check always wins
    for the scheduling decision — a stale marker must never gate a healthy
    board, and a healthy-looking marker must never unblock a degraded one.
    """
    present = completion_dedup_index_present(conn)
    duplicates = conn.execute(
        "SELECT COALESCE(SUM(n - 1), 0) AS d FROM ("
        "  SELECT COUNT(*) AS n FROM task_events"
        "   WHERE kind = ? AND run_id IS NOT NULL"
        "   GROUP BY run_id HAVING n > 1)",
        (BROKER_EVENT_WORKER_COMPLETION,),
    ).fetchone()
    duplicate_rows = int(duplicates["d"] if duplicates and duplicates["d"] else 0)

    reason = None
    if not present:
        reason = _read_broker_health_marker(conn) or "dedup_index_absent"
    elif duplicate_rows:
        reason = "duplicate_completion_rows_present"
    return BrokerHealth(
        dedup_index_present=present,
        duplicate_completion_rows=duplicate_rows,
        degraded_reason=reason,
    )


def assert_broker_safe_to_schedule(conn: sqlite3.Connection) -> BrokerHealth:
    """Hard gate. Raises :class:`BrokerUnsafeError` when not safe.

    Intended as the check a scheduler/activation path must call before running
    a consumer against a board. Degrading silently is what this replaces.
    """
    health = broker_health(conn)
    if not health.safe_to_schedule:
        raise BrokerUnsafeError(
            "board cannot enforce exactly-once completion folding "
            f"({health.degraded_reason}); refusing to schedule a broker consumer"
        )
    return health


def _read_broker_health_marker(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT last_event_id FROM kanban_broker_subs WHERE consumer = ?",
            (_BROKER_HEALTH_MARKER_CONSUMER,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return "dedup_index_creation_failed"


def _write_broker_health_marker(conn: sqlite3.Connection) -> None:
    """Persist that this board failed to build the dedup index.

    Stored as a reserved row in ``kanban_broker_subs`` rather than a new table,
    so the degraded state survives a restart without widening the schema. It is
    diagnostic only: :func:`broker_health` always re-checks the live schema.
    """
    now = int(time.time())
    try:
        conn.execute(
            "INSERT OR IGNORE INTO kanban_broker_subs "
            "(consumer, last_event_id, created_at, updated_at) VALUES (?, 0, ?, ?)",
            (_BROKER_HEALTH_MARKER_CONSUMER, now, now),
        )
    except sqlite3.Error:
        pass


#: Reserved consumer name used only as a persistent degraded-state marker.
_BROKER_HEALTH_MARKER_CONSUMER = "__broker_health__dedup_index_failed"


# field -> (python types, required)
_BROKER_EVENT_SPECS: dict[str, dict[str, tuple[tuple[type, ...], bool]]] = {
    BROKER_EVENT_WORKER_COMPLETION: {
        "run_id": ((int,), True),
        "task_id": ((str,), True),
        "outcome": ((str,), True),
        "run_status": ((str,), True),
        "profile": ((str, type(None)), False),
        "started_at": ((int, type(None)), False),
        "ended_at": ((int, type(None)), False),
        "error": ((str, type(None)), False),
        # The session the worker actually ran in, when the dispatcher recorded
        # one. NULL until the producer-side change lands; a NULL here is what
        # makes a ``continue`` route fail closed to REVIEW.
        "worker_session_id": ((str, type(None)), False),
        # N1: provenance travels WITH the completion. A payload without it is
        # unprovenanced and fails closed at the router — enforcement must not
        # depend on a caller remembering to pass an optional kwarg.
        "worker_session_source": ((str, type(None)), False),
    },
    BROKER_EVENT_ROUTE_DECIDED: {
        "run_id": ((int,), True),
        "task_id": ((str,), True),
        "route": ((str,), True),
        "reason": ((str,), True),
        "outcome": ((str,), True),
        "spawn": ((bool,), True),
        "session_id": ((str, type(None)), False),
        "provider": ((str, type(None)), False),
        "seat": ((str, type(None)), False),
    },
    BROKER_EVENT_SESSION_MAPPED: {
        "run_id": ((int,), True),
        "task_id": ((str,), True),
        "worker_session_id": ((str,), True),
        "source": ((str,), True),
        "continue_eligible": ((bool,), True),
    },
}


def validate_broker_event_payload(kind: str, payload: Optional[dict]) -> dict:
    """Validate + normalise a broker event payload. Pure; raises on mismatch.

    ``task_events.kind`` is free-form text and ``_append_event`` accepts any
    payload, which is fine for human-facing history but not for rows a control
    loop will make decisions from. These two kinds are typed at the boundary so
    a malformed producer fails loudly here rather than being silently
    interpreted downstream.
    """
    if kind not in _BROKER_EVENT_SPECS:
        raise BrokerEventValidationError(f"not a broker event kind: {kind!r}")
    if not isinstance(payload, dict):
        raise BrokerEventValidationError(
            f"{kind}: payload must be a dict, got {type(payload).__name__}"
        )

    spec = _BROKER_EVENT_SPECS[kind]
    unknown = sorted(set(payload) - set(spec))
    if unknown:
        raise BrokerEventValidationError(f"{kind}: unknown field(s) {unknown}")

    out: dict[str, Any] = {}
    for field, (types_, required) in spec.items():
        if field not in payload:
            if required:
                raise BrokerEventValidationError(f"{kind}: missing required field {field!r}")
            continue
        value = payload[field]
        # bool is a subclass of int; keep the two from satisfying each other.
        if bool in types_ and not isinstance(value, bool):
            raise BrokerEventValidationError(
                f"{kind}: field {field!r} must be bool, got {type(value).__name__}"
            )
        if bool not in types_ and isinstance(value, bool) and int in types_:
            raise BrokerEventValidationError(
                f"{kind}: field {field!r} must be int, got bool"
            )
        if not isinstance(value, types_):
            names = "/".join(t.__name__ for t in types_)
            raise BrokerEventValidationError(
                f"{kind}: field {field!r} must be {names}, got {type(value).__name__}"
            )
        out[field] = value

    # Both kinds must name a real run. SQLite treats NULLs as distinct in a
    # UNIQUE index, so a NULL/0 run_id would sit outside the dedup guard
    # entirely and could be recorded without limit; a decision about run 0 is
    # equally meaningless.
    if out["run_id"] <= 0:
        raise BrokerEventValidationError(
            f"{kind}: run_id must be a positive run id, got {out['run_id']!r}"
        )

    if kind == BROKER_EVENT_WORKER_COMPLETION:
        if out["outcome"] not in TERMINAL_RUN_OUTCOMES:
            raise BrokerEventValidationError(
                f"{kind}: outcome {out['outcome']!r} is not terminal"
            )
    elif kind == BROKER_EVENT_SESSION_MAPPED:
        if out["source"] not in VALID_SESSION_SOURCES:
            raise BrokerEventValidationError(
                f"{kind}: unknown session source {out['source']!r}"
            )
        expected = out["source"] in CONTINUE_ELIGIBLE_SESSION_SOURCES
        if out["continue_eligible"] is not expected:
            raise BrokerEventValidationError(
                f"{kind}: continue_eligible must be {expected} for source "
                f"{out['source']!r}"
            )
    else:
        if out["route"] not in VALID_ROUTES:
            raise BrokerEventValidationError(f"{kind}: unknown route {out['route']!r}")
        if out["spawn"] is not False:
            # Structural guard: this slice cannot express a spawning decision.
            raise BrokerEventValidationError(f"{kind}: spawn must be False in this slice")
    return out


# --- per-board consumer cursor + atomic claim ------------------------------


class BrokerAuthError(RuntimeError):
    """A named consumer was used without, or with the wrong, token."""


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authenticate_consumer(
    conn: sqlite3.Connection, consumer: str, token: Optional[str]
) -> None:
    """Enforce a registered consumer's token. Constant-time comparison.

    A consumer registered *without* a token stays usable without one (local and
    legacy callers), but once a token is set the name cannot be used without
    it — so a second process cannot quietly share, or steal, another
    consumer's cursor.
    """
    row = conn.execute(
        "SELECT token_sha256 FROM kanban_broker_subs WHERE consumer = ?", (consumer,)
    ).fetchone()
    if row is None:
        return
    expected = row["token_sha256"] if "token_sha256" in row.keys() else None
    if not expected:
        return
    if not token:
        raise BrokerAuthError(f"consumer {consumer!r} requires a token")
    if not hmac.compare_digest(str(expected), _token_digest(token)):
        raise BrokerAuthError(f"invalid token for consumer {consumer!r}")


def ensure_broker_sub(
    conn: sqlite3.Connection, *, consumer: str, token: Optional[str] = None,
) -> int:
    """Create the consumer row if absent. Returns its current cursor.

    Passing ``token`` on first registration binds the name to that secret; a
    later call must present the same token. Re-registering with a different
    token raises rather than silently rebinding the name.
    """
    if not consumer or not isinstance(consumer, str):
        raise ValueError("consumer must be a non-empty string")
    _authenticate_consumer(conn, consumer, token)
    now = int(time.time())
    digest_value = _token_digest(token) if token else None
    with write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO kanban_broker_subs "
            "(consumer, last_event_id, created_at, updated_at, token_sha256) "
            "VALUES (?, 0, ?, ?, ?)",
            (consumer, now, now, digest_value),
        )
        if digest_value:
            # Bind the token on a row that predates it, but never overwrite a
            # different one — _authenticate_consumer already rejected that.
            conn.execute(
                "UPDATE kanban_broker_subs SET token_sha256 = ?, updated_at = ? "
                "WHERE consumer = ? AND token_sha256 IS NULL",
                (digest_value, now, consumer),
            )
    row = conn.execute(
        "SELECT last_event_id FROM kanban_broker_subs WHERE consumer = ?", (consumer,),
    ).fetchone()
    return int(row["last_event_id"]) if row else 0


def broker_cursor(conn: sqlite3.Connection, *, consumer: str) -> int:
    row = conn.execute(
        "SELECT last_event_id FROM kanban_broker_subs WHERE consumer = ?", (consumer,),
    ).fetchone()
    return int(row["last_event_id"]) if row else 0


def unseen_events_for_broker(
    conn: sqlite3.Connection,
    *,
    consumer: str,
    kinds: Optional[Iterable[str]] = None,
    limit: int = BROKER_DEFAULT_LIMIT,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` past this consumer's cursor.

    Board-wide (every task), ordered by ``task_events.id``, and **bounded** by
    ``limit`` so one pass cannot try to drain an arbitrarily large backlog. The
    cursor is not advanced here.
    """
    row = conn.execute(
        "SELECT last_event_id FROM kanban_broker_subs WHERE consumer = ?", (consumer,),
    ).fetchone()
    if row is None:
        return 0, []
    cursor = int(row["last_event_id"])
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC LIMIT ?"
    )
    params: list[Any] = [cursor]
    if kind_list:
        params.extend(kind_list)
    params.append(_enforce_limit(limit))
    rows = conn.execute(q, params).fetchall()
    out: list[Event] = []
    max_id = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            Event(
                id=r["id"],
                task_id=r["task_id"],
                kind=r["kind"],
                payload=payload,
                created_at=r["created_at"],
                run_id=(
                    int(r["run_id"])
                    if "run_id" in r.keys() and r["run_id"] is not None
                    else None
                ),
            )
        )
        max_id = max(max_id, int(r["id"]))
    return max_id, out


def claim_unseen_events_for_broker(
    conn: sqlite3.Connection,
    *,
    consumer: str,
    kinds: Optional[Iterable[str]] = None,
    limit: int = BROKER_DEFAULT_LIMIT,
    token: Optional[str] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim this consumer's unseen events board-wide.

    Returns ``(old_cursor, new_cursor, events)``. Exactly the discipline
    :func:`claim_unseen_events_for_sub` uses: the read and the cursor advance
    happen inside one ``BEGIN IMMEDIATE`` transaction, and the ``UPDATE`` is
    guarded by a CAS on the previous ``last_event_id``. Concurrent consumers
    with the same name serialize on SQLite's writer lock, so a given event
    range is claimed once.

    On a delivery/processing failure the caller calls
    :func:`rewind_broker_cursor`, which only rewinds if nobody else advanced
    past the claim.
    """
    _authenticate_consumer(conn, consumer, token)
    limit = _enforce_limit(limit)
    with write_txn(conn):
        row = conn.execute(
            "SELECT last_event_id FROM kanban_broker_subs WHERE consumer = ?",
            (consumer,),
        ).fetchone()
        if row is None:
            return 0, 0, []
        old_cursor = int(row["last_event_id"])
        new_cursor, events = unseen_events_for_broker(
            conn, consumer=consumer, kinds=kinds, limit=limit
        )
        if not events:
            return old_cursor, old_cursor, []
        if int(new_cursor) <= int(old_cursor):
            # Monotonic guard: a cursor may only move forward. Anything else
            # would re-deliver or rewind silently.
            return old_cursor, old_cursor, []
        cur = conn.execute(
            "UPDATE kanban_broker_subs SET last_event_id = ?, updated_at = ? "
            "WHERE consumer = ? AND last_event_id = ?",
            (int(new_cursor), int(time.time()), consumer, int(old_cursor)),
        )
        if cur.rowcount != 1:
            # The CAS lost: another claimer advanced this consumer between our
            # read and our write. Claiming without advancing would hand the
            # same events to two owners, so we yield the range entirely.
            return old_cursor, old_cursor, []
        return old_cursor, new_cursor, events


def advance_broker_cursor(
    conn: sqlite3.Connection, *, consumer: str, new_cursor: int,
) -> bool:
    """Move a consumer's cursor **forward only**. Returns True if it moved.

    The ``last_event_id <= ?`` guard makes this monotonic: an advance can never
    rewind a cursor. Deliberate rewinds go through
    :func:`rewind_broker_cursor`, which is CAS-guarded on the exact claim.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_broker_subs SET last_event_id = ?, updated_at = ? "
            "WHERE consumer = ? AND last_event_id <= ?",
            (int(new_cursor), int(time.time()), consumer, int(new_cursor)),
        )
    return cur.rowcount > 0


def rewind_broker_cursor(
    conn: sqlite3.Connection,
    *,
    consumer: str,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a claim after a failed pass. CAS-guarded; True if it rewound."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_broker_subs SET last_event_id = ?, updated_at = ? "
            "WHERE consumer = ? AND last_event_id = ?",
            (int(old_cursor), int(time.time()), consumer, int(claimed_cursor)),
        )
    return cur.rowcount > 0


# --- terminal runs -> exactly one typed completion event -------------------


def record_worker_completion_events(
    conn: sqlite3.Connection,
    *,
    limit: int = BROKER_DEFAULT_LIMIT,
    allow_degraded: bool = False,
) -> list[int]:
    """Fold terminal ``task_runs`` into typed completion events, exactly once.

    A run qualifies when it has both ``ended_at`` and a terminal ``outcome``.
    Non-terminal (still-running, or ended without an outcome) rows are ignored,
    so an in-flight attempt is never mistaken for a completion.

    Exactly-once is enforced by the database — the partial UNIQUE index on
    ``task_events(run_id, kind)`` — and the insert is ``INSERT OR IGNORE``
    inside a single ``BEGIN IMMEDIATE``. A crash mid-pass, a restart, or a
    second consumer racing the same board therefore converges on one event per
    run rather than relying on any application-side ledger.

    Returns the run ids newly recorded by this call. Bounded by ``limit``.

    **Refuses to run without the dedup index.** The ``NOT EXISTS`` subquery
    below is a read in the same transaction as the insert, but SQLite's
    snapshot does not serialise two processes doing read-then-insert on
    different connections — a multiprocess race therefore produces duplicate
    completion events when the index is absent. Rather than degrade silently,
    this raises :class:`BrokerUnsafeError`; a caller that genuinely accepts
    at-least-once folding must opt in with ``allow_degraded=True``, which makes
    the unsafe mode explicit at the call site instead of a log line nobody
    reads.
    """
    if not allow_degraded and not completion_dedup_index_present(conn):
        raise BrokerUnsafeError(
            f"{COMPLETION_DEDUP_INDEX} is absent: exactly-once completion "
            "folding cannot be enforced on this board. Pass allow_degraded=True "
            "to accept at-least-once folding explicitly."
        )
    rows = conn.execute(
        """
        SELECT r.id AS run_id, r.task_id, r.profile, r.status, r.outcome,
               r.started_at, r.ended_at, r.error, r.worker_session_id,
               r.worker_session_source
          FROM task_runs r
         WHERE r.ended_at IS NOT NULL
           AND r.outcome IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM task_events e
                  WHERE e.run_id = r.id AND e.kind = ?
               )
         ORDER BY r.id ASC
         LIMIT ?
        """,
        (BROKER_EVENT_WORKER_COMPLETION, _enforce_limit(limit)),
    ).fetchall()

    recorded: list[int] = []
    if not rows:
        return recorded

    with write_txn(conn):
        for r in rows:
            outcome = (r["outcome"] or "").strip()
            if outcome not in TERMINAL_RUN_OUTCOMES:
                # Unknown outcome: not a completion this slice understands.
                # Left alone rather than guessed at.
                continue
            payload = validate_broker_event_payload(
                BROKER_EVENT_WORKER_COMPLETION,
                {
                    "run_id": int(r["run_id"]),
                    "task_id": str(r["task_id"]),
                    "outcome": outcome,
                    "run_status": str(r["status"] or ""),
                    "profile": r["profile"],
                    "started_at": int(r["started_at"]) if r["started_at"] is not None else None,
                    "ended_at": int(r["ended_at"]) if r["ended_at"] is not None else None,
                    "error": r["error"],
                    "worker_session_id": r["worker_session_id"],
                    "worker_session_source": r["worker_session_source"],
                },
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_events "
                "(task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    payload["task_id"],
                    payload["run_id"],
                    BROKER_EVENT_WORKER_COMPLETION,
                    json.dumps(payload, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            if cur.rowcount:
                recorded.append(payload["run_id"])
    return recorded



def record_worker_completion_event(conn: sqlite3.Connection, *, run_id: int) -> bool:
    """Fold exactly one specified terminal run, without draining board backlog.

    A governed handoff must fold *its own* reclaimed antecedent before it can
    decide whether to continue the pre-existing session. Calling the bounded
    board-wide consumer for that purpose is incorrect: a large historical
    backlog can fill its limit before the current handoff run is reached.

    The same partial unique index and ``INSERT OR IGNORE`` provide exactly-once
    semantics. A concurrent normal consumer is accepted only when it recorded
    the identical typed payload for this exact run; otherwise this refuses.
    """
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise BrokerEventValidationError("run_id must be a positive integer")
    if not completion_dedup_index_present(conn):
        raise BrokerUnsafeError(
            f"{COMPLETION_DEDUP_INDEX} is absent: exactly-once completion "
            "folding cannot be enforced on this board"
        )
    row = conn.execute(
        """
        SELECT id AS run_id, task_id, profile, status, outcome, started_at,
               ended_at, error, worker_session_id, worker_session_source
          FROM task_runs WHERE id = ? AND ended_at IS NOT NULL
                            AND outcome IS NOT NULL
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return False
    payload = validate_broker_event_payload(
        BROKER_EVENT_WORKER_COMPLETION,
        {
            "run_id": int(row["run_id"]), "task_id": str(row["task_id"]),
            "outcome": (row["outcome"] or "").strip(),
            "run_status": str(row["status"] or ""), "profile": row["profile"],
            "started_at": int(row["started_at"]) if row["started_at"] is not None else None,
            "ended_at": int(row["ended_at"]) if row["ended_at"] is not None else None,
            "error": row["error"], "worker_session_id": row["worker_session_id"],
            "worker_session_source": row["worker_session_source"],
        },
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    with write_txn(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO task_events "
            "(task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (payload["task_id"], payload["run_id"], BROKER_EVENT_WORKER_COMPLETION,
             encoded, int(time.time())),
        )
        if cur.rowcount:
            return True
        existing = conn.execute(
            "SELECT payload FROM task_events WHERE run_id = ? AND kind = ?",
            (run_id, BROKER_EVENT_WORKER_COMPLETION),
        ).fetchone()
    if existing is None:
        return False
    try:
        return json.loads(existing["payload"]) == payload
    except (TypeError, ValueError):
        return False
# --- pure route decision ---------------------------------------------------


@dataclass(frozen=True)
class RouteDecision:
    """What should happen next about a task. Inert: ``spawn`` is always False."""

    route: str
    reason: str
    task_id: str
    run_id: int
    outcome: str
    session_id: Optional[str] = None
    provider: Optional[str] = None
    spawn: bool = False
    #: Declared seat this decision would reuse, when one resolved.
    seat: Optional[str] = None

    def to_payload(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "route": self.route,
            "reason": self.reason,
            "outcome": self.outcome,
            "spawn": self.spawn,
            "session_id": self.session_id,
            "provider": self.provider,
            "seat": self.seat,
        }


def _failure_limit_for(task_row: Any, failure_limit: Optional[int] = None) -> int:
    """Resolve the effective failure limit in the **native** order.

    Identical to ``recompute_ready`` / ``_record_task_failure`` so the loop and
    the circuit breaker never disagree about when a task is permanently
    blocked:

      1. per-task ``max_retries`` if set
      2. caller-supplied ``failure_limit`` (the dispatcher's
         ``kanban.failure_limit`` config value)
      3. ``DEFAULT_FAILURE_LIMIT``

    An earlier version of this slice carried its own constant of 3, which
    silently disagreed with the native default of 2.
    """
    try:
        value = task_row["max_retries"]
    except (KeyError, IndexError, TypeError):
        value = None
    if isinstance(value, int) and value > 0:
        return value
    if failure_limit is not None:
        return int(failure_limit)
    return DEFAULT_FAILURE_LIMIT


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def decide_route(
    *,
    completion: dict,
    task_row: Any,
    provider_resolver: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    failure_limit: Optional[int] = None,
    seats: Optional[SeatRegistry] = None,
) -> RouteDecision:
    """Decide continue / review / block / close for one completion. **Pure.**

    No I/O, no writes, no spawning: this takes a validated completion payload
    plus the ``tasks`` row and returns a decision.

    **The resume target is the worker session, not the originating session.**
    ``tasks.session_id`` records the chat/agent session that *created* the
    task; handing work back to it would drive the wrong session. The only
    sound target is ``task_runs.worker_session_id`` — the session the worker
    actually ran in — which arrives on the completion payload.

    ``continue`` is the only route that hands work back to an existing session,
    so it requires an unambiguous target: a worker session **and** a resolvable
    provider. Either missing or ambiguous ⇒ ``review``, never a spawn.

    ``provider_resolver`` maps a profile name to its configured provider, for
    tasks that do not set ``provider_override``. It is injected rather than
    read from config here so this layer stays pure and free of a config
    dependency; without it, only an explicit override counts as resolved.
    """
    payload = validate_broker_event_payload(BROKER_EVENT_WORKER_COMPLETION, completion)
    task_id = payload["task_id"]
    run_id = payload["run_id"]
    outcome = payload["outcome"]

    def _clean(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value.strip() else None

    # The worker session the run executed in — the resume target — and the
    # provenance of that mapping. Both come from the validated payload, which
    # `record_worker_completion_events` fills from the run row, so the
    # canonical path always carries them (N1).
    session_id = _clean(payload.get("worker_session_id"))
    session_source = _clean(payload.get("worker_session_source"))

    provider = _clean(_row_get(task_row, "provider_override"))
    if provider is None and provider_resolver is not None:
        try:
            provider = _clean(provider_resolver(payload.get("profile")))
        except Exception:
            # A resolver that raises is an ambiguous mapping, not a crash.
            provider = None

    seat = seats.eligible_for_session(session_id) if seats is not None else None
    if seat is not None and provider is None:
        provider = seat.provider

    def decision(route: str, reason: str) -> RouteDecision:
        return RouteDecision(
            route=route,
            reason=reason,
            task_id=task_id,
            run_id=run_id,
            outcome=outcome,
            session_id=session_id,
            provider=provider,
            spawn=False,
            seat=seat.seat_id if seat is not None else None,
        )

    if task_row is None:
        return decision(ROUTE_REVIEW, "task_not_found")

    status = _row_get(task_row, "status")
    status = str(status) if status is not None else ""

    if status not in _ROUTABLE_TASK_STATUSES:
        # Not a status this router understands (including any future or
        # foreign value such as ``cancelled`` / ``in_progress``, neither of
        # which is native). Never interpreted, never continued.
        return decision(ROUTE_REVIEW, f"unknown_task_status:{status or 'none'}")

    if status in _CLOSED_TASK_STATUSES:
        return decision(ROUTE_CLOSE, f"task_already_{status}")
    if status in _BLOCKED_TASK_STATUSES:
        return decision(ROUTE_BLOCK, "task_already_blocked")

    if outcome == "completed":
        if status in _REVIEW_TASK_STATUSES:
            return decision(ROUTE_REVIEW, "completed_pending_review")
        # The loop does not close a card on the worker's say-so.
        return decision(ROUTE_REVIEW, "completed_awaiting_verdict")

    if outcome in ("blocked", "gave_up"):
        return decision(ROUTE_BLOCK, f"run_{outcome}")

    if outcome in RETRYABLE_RUN_OUTCOMES:
        failures = _row_get(task_row, "consecutive_failures") or 0
        try:
            failures = int(failures)
        except (TypeError, ValueError):
            failures = 0
        limit = _failure_limit_for(task_row, failure_limit)
        if failures >= limit:
            return decision(ROUTE_BLOCK, f"failure_limit_reached:{failures}/{limit}")
        if session_id is None:
            return decision(ROUTE_REVIEW, "missing_worker_session")
        # N1 — provenance gate, read from the AUTHORITATIVE completion payload.
        #
        # An earlier version took the source from an optional kwarg, so the
        # canonical fold -> claim -> route path never enforced it: a session id
        # with no provenance at all sailed through to CONTINUE. Enforcement
        # must not depend on a caller remembering to pass something.
        #
        # Missing, inferred, and unknown all fail closed. Only the declared
        # sources in CONTINUE_ELIGIBLE_SESSION_SOURCES may drive reuse.
        if session_source is None:
            return decision(ROUTE_REVIEW, "missing_session_provenance")
        if session_source not in VALID_SESSION_SOURCES:
            return decision(ROUTE_REVIEW, f"unknown_session_provenance:{session_source}")
        if session_source not in CONTINUE_ELIGIBLE_SESSION_SOURCES:
            return decision(ROUTE_REVIEW, f"session_source_not_eligible:{session_source}")
        # Seat gate: when a registry is supplied, only a declared eligible seat
        # may be reused. No registry means no seat-based reuse is claimed.
        if seats is not None and seat is None:
            return decision(ROUTE_REVIEW, "no_declared_eligible_seat")
        if provider is None:
            return decision(ROUTE_REVIEW, "ambiguous_provider_mapping")
        return decision(ROUTE_CONTINUE, f"retryable_{outcome}:{failures}/{limit}")

    # Unreachable for validated payloads, but never guess toward continuing.
    return decision(ROUTE_REVIEW, f"unrecognised_outcome:{outcome}")


def set_run_worker_session(
    conn: sqlite3.Connection, *, run_id: int, worker_session_id: str,
) -> bool:
    """Record the session a dispatched worker ran in. Producer-side API.

    Provided so the linkage is *expressible* natively; this slice never calls
    it. The dispatcher spawn path is the intended caller, and wiring it there
    is a separate change against a live write path.
    """
    if not worker_session_id or not isinstance(worker_session_id, str):
        raise ValueError("worker_session_id must be a non-empty string")
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE task_runs SET worker_session_id = ? WHERE id = ?",
            (worker_session_id, int(run_id)),
        )
    return cur.rowcount > 0


# --- worker-session provenance (slice 3.1) --------------------------------


@dataclass(frozen=True)
class WorkerSessionMapping:
    """A run's association with an already-known worker session."""

    run_id: int
    task_id: str
    worker_session_id: str
    source: str

    @property
    def continue_eligible(self) -> bool:
        return self.source in CONTINUE_ELIGIBLE_SESSION_SOURCES


class ProvenanceDowngradeError(ValueError):
    """A declared provenance would be replaced by a weaker one."""


def record_worker_session_provenance(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    worker_session_id: str,
    source: str,
    allow_downgrade: bool = False,
) -> WorkerSessionMapping:
    """Map an existing run to an already-known worker session, with provenance.

    **Records; never spawns.** This associates a run with a session that
    already exists somewhere else — it does not create, resume, or contact one.

    ``source`` is closed (:data:`VALID_SESSION_SOURCES`). Only *declared*
    sources are CONTINUE-eligible: an ``inferred`` mapping is stored and
    reported so an operator can see it, but the router will not hand work back
    on the strength of a guess.
    """
    if source not in VALID_SESSION_SOURCES:
        raise ValueError(
            f"unknown worker session source {source!r}; "
            f"expected one of {sorted(VALID_SESSION_SOURCES)}"
        )
    if not worker_session_id or not isinstance(worker_session_id, str):
        raise ValueError("worker_session_id must be a non-empty string")

    row = conn.execute(
        "SELECT task_id, worker_session_source FROM task_runs WHERE id = ?",
        (int(run_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"no such run: {run_id}")
    task_id = str(row["task_id"])

    # Monotonic provenance: a declared mapping is never quietly weakened.
    # Defence in depth alongside `revalidate_decision_provenance` — this closes
    # the ordinary write path, that one closes the action boundary against a
    # direct SQL write which bypasses this function entirely.
    existing = row["worker_session_source"]
    if (
        not allow_downgrade
        and isinstance(existing, str)
        and existing in CONTINUE_ELIGIBLE_SESSION_SOURCES
        and source not in CONTINUE_ELIGIBLE_SESSION_SOURCES
    ):
        raise ProvenanceDowngradeError(
            f"run {run_id} already has declared provenance {existing!r}; "
            f"refusing to downgrade to {source!r}. A deliberate downgrade is an "
            "A3 action and must pass allow_downgrade=True explicitly."
        )

    mapping = WorkerSessionMapping(
        run_id=int(run_id),
        task_id=task_id,
        worker_session_id=worker_session_id,
        source=source,
    )
    payload = validate_broker_event_payload(
        BROKER_EVENT_SESSION_MAPPED,
        {
            "run_id": mapping.run_id,
            "task_id": mapping.task_id,
            "worker_session_id": mapping.worker_session_id,
            "source": mapping.source,
            "continue_eligible": mapping.continue_eligible,
        },
    )
    with write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_session_id = ?, worker_session_source = ? "
            "WHERE id = ?",
            (worker_session_id, source, int(run_id)),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                int(run_id),
                BROKER_EVENT_SESSION_MAPPED,
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
            ),
        )
    return mapping


# --- declared seat registry (slice 3.3) -----------------------------------


@dataclass(frozen=True)
class Seat:
    """A declared, already-existing worker seat."""

    seat_id: str
    provider: str
    worker_session_id: str
    eligible: bool = False


class SeatRegistry:
    """Declared eligible seats, injected — never read from live config.

    Provider resolution **fails closed**: a task continues only when its run's
    worker session maps to a declared seat that is marked eligible and carries
    a provider. Nothing here discovers seats, and an unknown session resolves
    to ``None`` rather than to a guess.
    """

    def __init__(self, seats: Iterable[Seat] = ()) -> None:
        self._by_session: dict[str, Seat] = {}
        for seat in seats:
            if not seat.worker_session_id:
                raise ValueError(f"seat {seat.seat_id!r} has no worker_session_id")
            self._by_session[seat.worker_session_id] = seat

    def __len__(self) -> int:
        return len(self._by_session)

    def for_session(self, worker_session_id: Optional[str]) -> Optional[Seat]:
        if not worker_session_id:
            return None
        return self._by_session.get(worker_session_id)

    def eligible_for_session(self, worker_session_id: Optional[str]) -> Optional[Seat]:
        seat = self.for_session(worker_session_id)
        if seat is None or not seat.eligible or not seat.provider:
            return None
        return seat


# --- A3 gate + action policy (slice 3.2) ----------------------------------

#: Positive A3 marker, mirroring the native ``REVIEW_VERDICT=APPROVED`` idiom
#: that :func:`apply_approvals` already recognises.
A3_GATE_MARKERS = ("A3_GATE=GRANTED", "A3_GATE: GRANTED")

_A3_NEGATED_RE = re.compile(
    r"\b(no|not|without|never|revoke[d]?|deny|denied|refus\w*)\b[^.\n]{0,40}A3_GATE",
    re.IGNORECASE,
)
_A3_REVOKED_RE = re.compile(r"A3_GATE\s*[=:]\s*(REVOKED|DENIED)", re.IGNORECASE)


class ActionNotPermittedError(RuntimeError):
    """A real (non-simulated) action was attempted without a positive A3 gate."""


#: Durable, append-only latch kinds for L2. Stored as ``task_events`` rows —
#: native, append-only, and NOT removable by deleting a comment.
A3_EVENT_REVOKED = "a3_gate_revoked"
A3_EVENT_REVOCATION_CLEARED = "a3_gate_revocation_cleared"


def latch_a3_revocation(
    conn: sqlite3.Connection, *, task_id: str, reason: str,
) -> int:
    """Durably latch an A3 revocation. **Guarded: never called by this slice.**

    L2 — comment-based revocation was reversible: delete the revoking comment
    and a previously granted gate came back to life. A veto that an attacker
    (or an accidental cleanup) can erase is not a veto.

    The latch is an append-only ``task_events`` row, so removing a *comment*
    cannot revive the gate. Once latched, :func:`a3_gate_granted` returns False
    regardless of any grant comment, until an operator explicitly calls
    :func:`clear_a3_revocation_latch`.

    **This function performs a DB write and is deliberately not invoked
    anywhere in this slice** — not by the router, the policy, or the consumer.
    It exists so the latch is testable and so an operator interface exists to
    review. Against a live board it is an A3 action; see the activation delta.
    """
    if not reason or not isinstance(reason, str):
        raise ValueError("reason must be a non-empty string")
    with write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (
                task_id,
                A3_EVENT_REVOKED,
                json.dumps({"reason": reason}, ensure_ascii=False),
                int(time.time()),
            ),
        )
    return int(cur.lastrowid)


def clear_a3_revocation_latch(
    conn: sqlite3.Connection, *, task_id: str, reason: str,
) -> int:
    """Release a latched revocation. **Guarded: never called by this slice.**

    Appends a ``a3_gate_revocation_cleared`` row. The gate re-opens only if a
    valid grant comment also exists — clearing the latch does not itself grant
    anything. Same live-write caveat as :func:`latch_a3_revocation`.
    """
    if not reason or not isinstance(reason, str):
        raise ValueError("reason must be a non-empty string")
    with write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (
                task_id,
                A3_EVENT_REVOCATION_CLEARED,
                json.dumps({"reason": reason}, ensure_ascii=False),
                int(time.time()),
            ),
        )
    return int(cur.lastrowid)


def a3_revocation_latched(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when a durable revocation latch is in force (read-only)."""
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, A3_EVENT_REVOKED, A3_EVENT_REVOCATION_CLEARED),
    ).fetchone()
    if row is None:
        return False
    return str(row["kind"]) == A3_EVENT_REVOKED


def a3_gate_granted(conn: sqlite3.Connection, task_id: str) -> bool:
    """Is there a live, non-negated A3 grant for this task?

    Deliberately the same shape as the native approval scan: a marker comment,
    negation detection, and a later revocation wins. Absence is a denial.

    A durable revocation latch (L2) overrides everything: while latched, no
    combination of comments can re-open the gate.
    """
    if a3_revocation_latched(conn, task_id):
        return False
    rows = conn.execute(
        "SELECT id, body FROM task_comments WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    granted_at: Optional[int] = None
    revoked_at: Optional[int] = None
    for row in rows:
        body = row["body"] or ""
        if _A3_REVOKED_RE.search(body):
            revoked_at = int(row["id"])
            continue
        if any(marker in body for marker in A3_GATE_MARKERS):
            if _A3_NEGATED_RE.search(body):
                continue
            granted_at = int(row["id"])
    if granted_at is None:
        return False
    if revoked_at is not None and revoked_at > granted_at:
        return False
    return True


class ActionTransport(Protocol):
    """Something that can observe a routing outcome."""

    name: str
    simulated: bool

    def observe(self, decision: "RouteDecision") -> None:  # pragma: no cover
        ...


@dataclass
class SimulatedTransport:
    """Records outcomes. Contacts nothing, resumes nothing, spawns nothing."""

    name: str = "simulated"
    simulated: bool = True
    observed: list["RouteDecision"] = field(default_factory=list)

    def observe(self, decision: "RouteDecision") -> None:
        self.observed.append(decision)


class ActionPolicy:
    """Who may observe an outcome, and whether real action is permitted.

    **Trust is owned by the policy, never declared by the transport (N2).**

    An earlier version consulted ``transport.simulated``. That is
    self-declaration: any object could set ``simulated = True`` and bypass both
    the ``allow_real_action`` flag and the A3 gate. A transport asserting its
    own harmlessness is exactly the claim that must not be taken at face value.

    Now the policy holds **references** to the transports it considers
    simulated, and membership is tested by object identity. A caller cannot
    forge that: to be trusted, the very object must have been handed to the
    policy's constructor. ``transport.simulated`` is never read.

    Anything not registered is treated as real, and real action requires
    **both** ``allow_real_action=True`` **and** a positive A3 gate on the task.
    """

    def __init__(
        self,
        allow_real_action: bool = False,
        simulated_transports: Iterable[ActionTransport] = (),
    ) -> None:
        self.allow_real_action = bool(allow_real_action)
        # Identity set: `is`-comparison against the exact objects registered.
        self._simulated: list[ActionTransport] = list(simulated_transports)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"ActionPolicy(allow_real_action={self.allow_real_action}, "
            f"registered_simulated={len(self._simulated)})"
        )

    def is_registered_simulated(self, transport: ActionTransport) -> bool:
        """Identity membership. Never consults the transport's own claim."""
        return any(candidate is transport for candidate in self._simulated)

    def permit(
        self,
        conn: sqlite3.Connection,
        transport: ActionTransport,
        decision: "RouteDecision",
    ) -> None:
        """Raise :class:`ActionNotPermittedError` unless this is allowed."""
        if self.is_registered_simulated(transport):
            return
        name = getattr(transport, "name", type(transport).__name__)
        if not self.allow_real_action:
            raise ActionNotPermittedError(
                f"transport {name!r} is not a registered simulated transport and "
                "this policy does not allow real action (allow_real_action=False)"
            )
        if not a3_gate_granted(conn, decision.task_id):
            raise ActionNotPermittedError(
                f"transport {name!r} requires a positive A3 gate on "
                f"task {decision.task_id}; none found (or it was revoked)"
            )


class ProvenanceChangedError(ActionNotPermittedError):
    """The run's provenance no longer supports a decision made earlier."""


def current_run_provenance(
    conn: sqlite3.Connection, run_id: int,
) -> tuple[Optional[str], Optional[str]]:
    """Read a run's *current* ``(worker_session_id, worker_session_source)``.

    Read-only. This is the live value, not the snapshot a decision was made
    from.
    """
    row = conn.execute(
        "SELECT worker_session_id, worker_session_source FROM task_runs WHERE id = ?",
        (int(run_id),),
    ).fetchone()
    if row is None:
        return None, None

    def _clean(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value.strip() else None

    return _clean(row["worker_session_id"]), _clean(row["worker_session_source"])


def revalidate_decision_provenance(
    conn: sqlite3.Connection, decision: "RouteDecision",
) -> None:
    """Re-check a CONTINUE decision against the run's provenance *right now*.

    Provenance is captured into the completion payload at fold time and is then
    immutable, so a decision folded while the mapping said ``dispatcher_spawn``
    stayed CONTINUE-eligible even if ``task_runs.worker_session_source`` was
    later downgraded to ``inferred``. Nothing acts today, but a decision must
    not carry a stale warrant into the moment it would.

    This is deliberately checked **at the action boundary** rather than inside
    :func:`decide_route`: the router stays pure and free of I/O, folding and
    exactly-once semantics are untouched, and no new control plane or state is
    introduced — it is one read of the same native row.

    Only ``continue`` is revalidated; it is the only route that would hand work
    back to a session. Any drift fails closed with
    :class:`ProvenanceChangedError`.
    """
    if decision.route != ROUTE_CONTINUE:
        return
    session_id, source = current_run_provenance(conn, decision.run_id)
    if source is None:
        raise ProvenanceChangedError(
            f"run {decision.run_id} has no provenance now; refusing to act on a "
            "decision made when it did"
        )
    if source not in CONTINUE_ELIGIBLE_SESSION_SOURCES:
        raise ProvenanceChangedError(
            f"run {decision.run_id} provenance is now {source!r}, which is not "
            "continue-eligible; refusing to act on a stale decision"
        )
    if session_id != decision.session_id:
        raise ProvenanceChangedError(
            f"run {decision.run_id} worker session changed "
            f"({decision.session_id!r} -> {session_id!r}); refusing to act"
        )


def dispatch_outcome(
    conn: sqlite3.Connection,
    decision: "RouteDecision",
    *,
    transport: ActionTransport,
    policy: Optional[ActionPolicy] = None,
) -> bool:
    """Hand one outcome to a transport, subject to policy. Returns True if observed.

    This is the only place an outcome reaches anything outside the database,
    and by default the only transport that may receive one is simulated.

    Two independent gates, both fail-closed, checked before anything is
    observed: the decision's warrant must still hold
    (:func:`revalidate_decision_provenance`), and the policy must permit the
    transport (:meth:`ActionPolicy.permit`).

    **R11 — the CONTINUE boundary is serialized.** Those two checks and the
    observation used to be three separate statements, so a provenance downgrade
    or an A3 revocation landing between them was not seen: the warrant was
    verified, then invalidated, then acted on. For ``continue`` — the only route
    that hands work back to a session — the whole boundary now runs inside a
    single :func:`write_txn` (``BEGIN IMMEDIATE``). That takes SQLite's RESERVED
    lock, so any concurrent writer of ``task_runs.worker_session_source`` or of
    an A3 latch blocks until we finish; there is no window in which the checks
    can be undermined before the act.

    No new primitive, table, or lock is introduced — this reuses the same write
    transaction the rest of the module already serializes on, and it writes
    nothing: the transaction is used purely as the mutual-exclusion boundary.

    **Constraint this places on any future real transport:** the writer lock is
    held across ``observe``. That is correct and cheap for the in-process
    transports that exist here, but a slow or network-bound transport would
    block every writer on the board for its duration. Such a transport must
    either be fast and non-blocking, or the design must change (e.g. take a
    fence/claim, commit, then act). This is recorded in the evidence report as
    a residual and belongs in the A3 packet.

    Non-``continue`` routes carry no warrant, take no action on a session, and
    are deliberately left on the unserialized path.
    """
    policy = policy or ActionPolicy()

    if decision.route != ROUTE_CONTINUE:
        policy.permit(conn, transport, decision)
        transport.observe(decision)
        return True

    with write_txn(conn):
        revalidate_decision_provenance(conn, decision)
        policy.permit(conn, transport, decision)
        transport.observe(decision)
    return True


def render_route_notification(decision: RouteDecision) -> str:
    """One concise line for a routing decision.

    Deliberately short and free of sender-controlled prose: the fields are ids,
    an enum route and a bounded reason, so the line stays safe to forward to a
    notifier without re-scanning it for secrets.
    """
    target = f" session={decision.session_id}" if decision.route == ROUTE_CONTINUE else ""
    return (
        f"{decision.route.upper()} task={decision.task_id} run={decision.run_id} "
        f"outcome={decision.outcome} reason={decision.reason}{target} spawn=false"
    )


def render_route_summary(decisions: Iterable[RouteDecision]) -> str:
    """One concise line for a whole pass. An empty pass says so."""
    counts: dict[str, int] = {}
    total = 0
    for decision in decisions:
        counts[decision.route] = counts.get(decision.route, 0) + 1
        total += 1
    if not total:
        return "hermes control loop: no new completions"
    parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return f"hermes control loop: {total} completion(s) routed — {parts} (inert, spawn=false)"


@dataclass(frozen=True)
class RouteDelivery:
    """One claimed batch of route notifications, with its claim window.

    ``old_cursor`` / ``new_cursor`` are exposed so a caller that owns delivery
    can undo the claim via :func:`rewind_broker_cursor` when its own send
    fails. Without them the claim would be at-most-once and a failed send would
    lose the notification silently.
    """

    lines: tuple[str, ...]
    old_cursor: int
    new_cursor: int

    @property
    def claimed(self) -> bool:
        return self.new_cursor > self.old_cursor


def drain_route_notifications(
    conn: sqlite3.Connection,
    *,
    consumer: str,
    limit: int = BROKER_DEFAULT_LIMIT,
    deliver: Optional[Callable[[list[str]], Any]] = None,
    token: Optional[str] = None,
) -> RouteDelivery:
    """Claim decided-route events for ``consumer`` and render notification lines.

    The consuming half: ``orchestrator_route_decided`` rows are claimed through
    the same atomic board cursor and turned into concise lines. It reads and
    advances its own cursor; it never acts on a decision.

    **Delivery semantics are explicit.**

    * With ``deliver``: **at-least-once**. The callable is invoked with the
      rendered lines inside this call; if it raises, the cursor is rewound
      (CAS-guarded) and the exception propagates, so the same batch is
      redelivered on a later pass. A duplicate notification is strictly better
      than a lost one.
    * Without ``deliver``: the caller owns delivery and therefore owns the
      retry decision. The returned :class:`RouteDelivery` carries the claim
      window so the caller can rewind. If it neither delivers nor rewinds, the
      batch is **at-most-once** — that is the caller's choice, not a silent
      default.
    """
    old_cursor, new_cursor, events = claim_unseen_events_for_broker(
        conn,
        consumer=consumer,
        kinds=[BROKER_EVENT_ROUTE_DECIDED],
        limit=limit,
        token=token,
    )
    lines: list[str] = []
    for event in events:
        payload = event.payload or {}
        try:
            payload = validate_broker_event_payload(BROKER_EVENT_ROUTE_DECIDED, payload)
        except BrokerEventValidationError as exc:
            # A malformed decision row is reported, never silently dropped and
            # never interpreted.
            lines.append(
                f"MALFORMED task={event.task_id} event={event.id} reason={exc}"
            )
            continue
        lines.append(
            render_route_notification(
                RouteDecision(
                    route=payload["route"],
                    reason=payload["reason"],
                    task_id=payload["task_id"],
                    run_id=payload["run_id"],
                    outcome=payload["outcome"],
                    session_id=payload.get("session_id"),
                    provider=payload.get("provider"),
                    spawn=False,
                )
            )
        )

    if deliver is not None and lines:
        try:
            deliver(lines)
        except BaseException:
            # BaseException, not Exception: ``asyncio.CancelledError`` (a
            # BaseException since 3.8), ``KeyboardInterrupt`` and ``SystemExit``
            # are exactly the shutdown paths where a claimed-but-undelivered
            # batch would be lost silently. Rewind first, then re-raise so the
            # cancellation/shutdown still propagates unchanged.
            rewind_broker_cursor(
                conn,
                consumer=consumer,
                claimed_cursor=new_cursor,
                old_cursor=old_cursor,
            )
            raise

    return RouteDelivery(
        lines=tuple(lines), old_cursor=old_cursor, new_cursor=new_cursor
    )


def record_route_decision_event(
    conn: sqlite3.Connection, decision: RouteDecision,
) -> bool:
    """Append one validated ``orchestrator_route_decided`` event.

    Separate from :func:`decide_route` on purpose: deciding is pure, recording
    is the only part that touches the DB.
    """
    payload = validate_broker_event_payload(
        BROKER_EVENT_ROUTE_DECIDED, decision.to_payload()
    )
    with write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                payload["task_id"],
                payload["run_id"],
                BROKER_EVENT_ROUTE_DECIDED,
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
            ),
        )
    return cur.rowcount > 0


@dataclass(frozen=True)
class BrokerPass:
    """One bounded native completion-routing pass.

    The cursor advance and all resulting route-decision records commit in one
    transaction.  Therefore a crash leaves the range unclaimed, while a
    successful pass cannot advance past a completion without its durable route
    and notification projection being available to the next layer.
    """

    consumer: str
    old_cursor: int
    new_cursor: int
    folded_run_ids: tuple[int, ...]
    decisions: tuple[RouteDecision, ...]
    notifications: tuple[NotificationProjection, ...]


def run_native_broker_pass(
    conn: sqlite3.Connection,
    *,
    consumer: str,
    token: Optional[str] = None,
    limit: int = BROKER_DEFAULT_LIMIT,
    provider_resolver: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    seats: Optional[SeatRegistry] = None,
) -> BrokerPass:
    """Fold and route one bounded completion batch using native Hermes state.

    This is deliberately a board-local, no-spawn control-loop pass.  It has no
    provider invocation, scheduler, notification transport, or terminal task
    mutation.  Consumers receive concise projections and decide separately how
    to deliver them.  The source of truth remains ``task_runs`` and
    ``task_events``; no queue, sidecar cursor, or lease is introduced.
    """
    assert_broker_safe_to_schedule(conn)
    bounded = _enforce_limit(limit)
    ensure_broker_sub(conn, consumer=consumer, token=token)
    folded = tuple(record_worker_completion_events(conn, limit=bounded))

    _authenticate_consumer(conn, consumer, token)
    with write_txn(conn):
        row = conn.execute(
            "SELECT last_event_id FROM kanban_broker_subs WHERE consumer = ?",
            (consumer,),
        ).fetchone()
        if row is None:  # defensive: ensure_broker_sub succeeded above
            raise BrokerAuthError(f"consumer {consumer!r} is not registered")
        old_cursor = int(row["last_event_id"])
        rows = conn.execute(
            "SELECT * FROM task_events WHERE id > ? AND kind = ? "
            "ORDER BY id ASC LIMIT ?",
            (old_cursor, BROKER_EVENT_WORKER_COMPLETION, bounded),
        ).fetchall()
        decisions: list[RouteDecision] = []
        projections: list[NotificationProjection] = []
        new_cursor = old_cursor
        for event_row in rows:
            try:
                payload = json.loads(event_row["payload"])
            except Exception as exc:  # malformed durable input must not skip ahead
                raise BrokerEventValidationError(
                    f"completion event {event_row['id']} has invalid JSON"
                ) from exc
            completion = validate_broker_event_payload(
                BROKER_EVENT_WORKER_COMPLETION, payload
            )
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (completion["task_id"],)
            ).fetchone()
            decision = decide_route(
                completion=completion,
                task_row=task_row,
                provider_resolver=provider_resolver,
                seats=seats,
            )
            route_payload = validate_broker_event_payload(
                BROKER_EVENT_ROUTE_DECIDED, decision.to_payload()
            )
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (route_payload["task_id"], route_payload["run_id"],
                 BROKER_EVENT_ROUTE_DECIDED,
                 json.dumps(route_payload, ensure_ascii=False), int(time.time())),
            )
            decisions.append(decision)
            projections.append(project_notification(decision))
            new_cursor = int(event_row["id"])
        if new_cursor != old_cursor:
            cur = conn.execute(
                "UPDATE kanban_broker_subs SET last_event_id = ?, updated_at = ? "
                "WHERE consumer = ? AND last_event_id = ?",
                (new_cursor, int(time.time()), consumer, old_cursor),
            )
            if cur.rowcount != 1:
                raise BrokerUnsafeError("broker cursor CAS lost; route batch rolled back")
    return BrokerPass(
        consumer=consumer,
        old_cursor=old_cursor,
        new_cursor=new_cursor,
        folded_run_ids=folded,
        decisions=tuple(decisions),
        notifications=tuple(projections),
    )


# ---------------------------------------------------------------------------
# Session-resume invocation contract (slice 4 — pre-activation, INERT)
# ---------------------------------------------------------------------------
#
# What this is: a **specification** of the one command a future, separately
# A3-gated executor would run to hand work back to an already-existing worker
# session. It is not an executor and contains no execution path.
#
# What it must never do, structurally rather than by convention:
#   * spawn a subprocess or import ``subprocess`` for its own use
#   * read a config file, environment variable, credential, or secret
#   * resume, create, attach to, or discover any real session
#   * write a board, schedule anything, or notify anything external
#   * alter :func:`dispatch_once`, :func:`decide_route`, or
#     :func:`dispatch_outcome` — none of them call into this section, and
#     nothing here calls back into them
#
# Everything below is a pure function over values the caller already holds.
# The only inputs are a validated :class:`RouteDecision` and a *declared*
# :class:`SessionBinding`; nothing is inferred, discovered, or defaulted from
# the environment. Every rejection raises :class:`InvocationPlanError`.

#: The only provider whose resume command this contract knows how to render.
PROVIDER_CLAUDE_CODE = "claude-code"

#: Providers a plan may be rendered for. Anything outside this set fails
#: closed. A "best effort" command for an unknown provider is exactly the kind
#: of guess that becomes a real invocation later, so it is refused instead.
RESUME_CAPABLE_PROVIDERS = frozenset({PROVIDER_CLAUDE_CODE})

#: Bounds on the executor timeout a plan may declare. An unbounded or absurd
#: timeout is the wedge failure mode this slice exists to avoid, so the bound
#: is enforced at plan time rather than left to the executor.
RESUME_MIN_TIMEOUT_SECONDS = 30
RESUME_DEFAULT_TIMEOUT_SECONDS = 900
RESUME_MAX_TIMEOUT_SECONDS = 3600

#: Capsule schema version. Bumped when the capsule shape changes so a future
#: executor can refuse a capsule it does not understand.
RESUME_CAPSULE_VERSION = 1

#: Hard bounds on capsule content. A capsule is a control-plane artifact, not a
#: transcript: it carries identifiers and a bounded instruction, nothing more.
RESUME_CAPSULE_MAX_INSTRUCTION_CHARS = 4000
RESUME_CAPSULE_MAX_NOTE_CHARS = 500
RESUME_CAPSULE_MAX_NOTES = 8

#: Declared JSON schema for the ``stream-json`` events a resumed session emits.
#: Carried BY the plan rather than rendered as a CLI flag: no Claude Code flag
#: that accepts an output schema was verified to exist, and inventing one here
#: would produce a command that is wrong at activation while looking complete.
#: A future executor validates observed events against this instead.
RESUME_STREAM_EVENT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "hermes.session_resume.stream_event",
    "type": "object",
    "required": ["type"],
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "system", "assistant", "user", "result", "hook_event",
                "rate_limit_event",
            ],
        },
        "subtype": {"type": "string"},
        "session_id": {"type": "string"},
        "hook_event_name": {"type": "string"},
        "is_error": {"type": "boolean"},
    },
    "allOf": [
        {
            "if": {
                "properties": {"type": {"const": "rate_limit_event"}},
                "required": ["type"],
            },
            "then": {
                "required": ["session_id", "rate_limit_info"],
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                    "rate_limit_info": {"type": "object"},
                },
            },
        },
    ],
    "additionalProperties": True,
}


class InvocationPlanError(ValueError):
    """A session-resume plan could not be built. Always fail closed."""


# ---------------------------------------------------------------------------
# Executor boundary (SOURCE-ONLY, DEFAULT-OFF)
# ---------------------------------------------------------------------------
#
# Consumes an already-claimed, persisted route decision and drives a
# **deterministic fake transport**. Nothing here spawns a process, resumes a
# session, opens a socket, reads a credential, or schedules anything.
#
# Ordering (the whole point of this boundary):
#
#     write_txn #1  warrant recheck -> persist execution claim + fence  [COMMIT]
#       (no transaction)  binding freshness -> A3/kill -> fake execute (timeout)
#     write_txn #2  fence recheck -> canonical terminal write (expected_run_id)
#
# Why a fence rather than a lock: ``dispatch_outcome`` (the R11 path) serializes
# by holding ``write_txn`` across its observation, which blocks every writer on
# the board for the duration. That is acceptable for an in-process observation
# and unacceptable for an external execution. Here the claim is committed first
# and the fence is *rechecked* afterwards, so interleaving is **detected and
# discarded** instead of prevented by blocking. The two paths therefore have
# deliberately different scope:
#
#   dispatch_outcome  — observation only, no external call, lock-serialized.
#   execute_planned_resume — external call, unlocked, fence-validated.
#
# Both fail closed; neither can perform a terminal write on a stale warrant.

EXECUTION_CLAIM_INDEX = "idx_events_execution_claim_once"

#: Typed executor events. Append-only; the claim doubles as the idempotency key.
EXEC_EVENT_CLAIMED = "session_execution_claimed"
EXEC_EVENT_COMPLETED = "session_execution_completed"
EXEC_EVENT_DISCARDED = "session_execution_discarded"
EXEC_EVENT_REFUSED = "session_execution_refused"
#: Truthful NON-terminal marker: the result was validated and the fence held,
#: but the canonical terminal transition has not been attempted yet. Recording
#: EXEC_EVENT_COMPLETED at that point would claim a lifecycle that may still be
#: refused by the in-transaction A3 guard.
EXEC_EVENT_VALIDATED = "session_execution_validated"

#: Only a binding minted by the dispatcher may drive an execution.
DISPATCHER_BINDING_OWNER = "hermes-dispatcher"

#: Terminal statuses a transport may report, and the route each maps to.
EXECUTION_STATUS_TO_ROUTE = {
    "completed": ROUTE_CLOSE,
    "blocked": ROUTE_BLOCK,
    "needs_review": ROUTE_REVIEW,
    "incomplete": ROUTE_CONTINUE,
}

#: Bound on the free-text summary a transport may return.
EXECUTION_MAX_SUMMARY_CHARS = 2000


class ExecutorError(RuntimeError):
    """Base for every executor-boundary refusal. All fail closed."""


class ExecutionNotPermitted(ExecutorError):
    """Policy refused this executor, or the A3/kill gate is not open."""


class DuplicateExecutionError(ExecutorError):
    """An execution was already claimed for this run."""


class BindingNotFreshError(ExecutorError):
    """The session mapping is retired, expired, or not dispatcher-owned."""


class ExecutorUnavailableError(ExecutorError):
    """The transport could not be reached or raised before producing a result."""


class ExecutionTimeoutError(ExecutorError):
    """The transport exceeded its bounded timeout."""


class ExecutionResultInvalid(ExecutorError):
    """The transport returned a malformed or non-conforming terminal result."""


class ExecutionFenceLost(ExecutorError):
    """The fence moved during execution; the result must be discarded."""


class ExecutorTransport(Protocol):
    """Something that can execute a plan. In this tree, only fakes exist."""

    name: str

    def execute(
        self, plan: "InvocationPlan", *, timeout_seconds: int
    ) -> dict:  # pragma: no cover - protocol
        ...


class ExecutorPolicy:
    """Who may execute. Trust is owned by the policy, never self-declared.

    Same identity-based model as :class:`ActionPolicy` (the N2 repair): the
    policy holds references to the fake transports it trusts and compares with
    ``is``. A transport that merely *claims* to be a fake is refused.

    ``allow_real_execution`` is False and there is no real transport in this
    tree; even set True, a non-registered executor additionally requires a
    positive A3 gate on the task.
    """

    def __init__(
        self,
        allow_real_execution: bool = False,
        fake_executors: Iterable[ExecutorTransport] = (),
    ) -> None:
        self.allow_real_execution = bool(allow_real_execution)
        self._fakes: list[ExecutorTransport] = list(fake_executors)

    def is_registered_fake(self, executor: ExecutorTransport) -> bool:
        return any(candidate is executor for candidate in self._fakes)

    def permit(
        self, conn: sqlite3.Connection, executor: ExecutorTransport, task_id: str,
    ) -> None:
        if self.is_registered_fake(executor):
            return
        name = getattr(executor, "name", type(executor).__name__)
        if not self.allow_real_execution:
            raise ExecutionNotPermitted(
                f"executor {name!r} is not a registered fake and this policy does "
                "not allow real execution (allow_real_execution=False)"
            )
        if not a3_gate_granted(conn, task_id):
            raise ExecutionNotPermitted(
                f"executor {name!r} requires a positive A3 gate on task {task_id}; "
                "none found (or it was revoked)"
            )


@dataclass(frozen=True)
class ExecutionFence:
    """What must still be true when the result comes back."""

    claim_event_id: int
    run_id: int
    task_id: str
    current_run_id: Optional[int]

    def to_payload(self) -> dict:
        return {
            "claim_event_id": self.claim_event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "current_run_id": self.current_run_id,
        }


@dataclass(frozen=True)
class ExecutionOutcome:
    """Result of one executor pass. ``executed`` refers to the FAKE transport."""

    fence: ExecutionFence
    status: str
    route: str
    summary: str
    terminal_write: bool
    notification: "NotificationProjection"
    executed_against_real_provider: bool = False

    def to_payload(self) -> dict:
        return {
            "fence": self.fence.to_payload(),
            "status": self.status,
            "route": self.route,
            "summary": self.summary,
            "terminal_write": self.terminal_write,
            "executed_against_real_provider": self.executed_against_real_provider,
            "notification": self.notification.to_json(),
        }


def validate_binding_freshness(
    binding: Any, *, now: int, owner: str = DISPATCHER_BINDING_OWNER,
) -> SessionBinding:
    """Eligible, fresh, non-retired, dispatcher-owned — or fail closed (G1).

    Checked **at execution time**, not only when the plan was built: a mapping
    can retire or expire between planning and acting, and that is precisely the
    window an executor must not walk into.
    """
    if not isinstance(binding, SessionBinding):
        raise BindingNotFreshError(
            f"binding must be a SessionBinding, got {type(binding).__name__}"
        )
    if binding.retired:
        raise BindingNotFreshError(
            f"session mapping for {binding.session_id!r} is retired"
        )
    if binding.owner != owner:
        raise BindingNotFreshError(
            f"session mapping owner {binding.owner!r} is not {owner!r}; only a "
            "dispatcher-owned mapping may drive an execution"
        )
    if binding.source not in CONTINUE_ELIGIBLE_SESSION_SOURCES:
        raise BindingNotFreshError(
            f"session source {binding.source!r} is not continue-eligible"
        )
    for field in ("issued_at", "expires_at"):
        value = getattr(binding, field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BindingNotFreshError(f"binding.{field} must be a positive integer")
    if binding.expires_at <= binding.issued_at:
        raise BindingNotFreshError(
            "binding.expires_at must be after binding.issued_at"
        )
    if now >= binding.expires_at:
        raise BindingNotFreshError(
            f"session mapping expired at {binding.expires_at} (now {now})"
        )
    if now < binding.issued_at:
        raise BindingNotFreshError(
            f"binding.issued_at {binding.issued_at} is in the future (now {now}); "
            "refusing to trust an implausible window"
        )
    return binding


def _validate_execution_result(result: Any, fence: ExecutionFence) -> tuple[str, str]:
    """Strict structured terminal result, or fail closed."""
    if not isinstance(result, dict):
        raise ExecutionResultInvalid(
            f"result must be a dict, got {type(result).__name__}"
        )
    allowed = {"status", "summary", "run_id"}
    unknown = sorted(set(result) - allowed)
    if unknown:
        raise ExecutionResultInvalid(f"result carries unknown field(s) {unknown}")
    for field in ("status", "summary", "run_id"):
        if field not in result:
            raise ExecutionResultInvalid(f"result missing required field {field!r}")

    status = result["status"]
    if not isinstance(status, str) or status not in EXECUTION_STATUS_TO_ROUTE:
        raise ExecutionResultInvalid(
            f"result.status must be one of {sorted(EXECUTION_STATUS_TO_ROUTE)}, "
            f"got {status!r}"
        )
    run_id = result["run_id"]
    if isinstance(run_id, bool) or not isinstance(run_id, int):
        raise ExecutionResultInvalid("result.run_id must be an integer")
    if run_id != fence.run_id:
        raise ExecutionResultInvalid(
            f"result.run_id {run_id} does not match the executed run {fence.run_id}"
        )
    try:
        summary = _require_bounded_text(
            result["summary"], "result.summary", EXECUTION_MAX_SUMMARY_CHARS
        )
    except InvocationPlanError as exc:
        # Translate: a transport's malformed summary is an *execution* fault.
        # Leaking InvocationPlanError here escaped the ExecutorError handler
        # entirely, so no refusal was recorded and the caller saw the wrong
        # exception type.
        raise ExecutionResultInvalid(str(exc)) from exc
    return status, summary


def _record_exec_event(
    conn: sqlite3.Connection, *, task_id: str, run_id: Optional[int], kind: str,
    payload: dict,
) -> int:
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, json.dumps(payload, ensure_ascii=False),
         int(time.time())),
    )
    return int(cur.lastrowid)


def _refuse(
    conn: sqlite3.Connection, *, task_id: str, run_id: Optional[int], reason: str,
) -> None:
    """Record a refusal. Best-effort: never masks the original failure."""
    try:
        with write_txn(conn):
            _record_exec_event(
                conn, task_id=task_id, run_id=run_id, kind=EXEC_EVENT_REFUSED,
                payload={"reason": reason},
            )
    except Exception:  # noqa: BLE001 - diagnostics must not shadow the refusal
        pass


def claim_execution_fence(
    conn: sqlite3.Connection, *, plan: "InvocationPlan",
) -> ExecutionFence:
    """Persist and COMMIT an execution claim. Step 1 of the boundary.

    The claim is the idempotency key: the partial UNIQUE index means a second
    claim for the same run raises :class:`DuplicateExecutionError`, so a
    redelivered decision cannot execute twice.

    Returns after commit, so the external call in step 2 holds no transaction.
    """
    decision = plan.decision
    if decision.route != ROUTE_CONTINUE:
        raise ExecutionNotPermitted(
            f"only a {ROUTE_CONTINUE!r} decision may be executed, got "
            f"{decision.route!r}"
        )
    if not execution_claim_index_present(conn):
        raise ExecutionNotPermitted(
            f"{EXECUTION_CLAIM_INDEX} is absent: executor idempotency cannot be "
            "enforced, refusing to claim"
        )

    try:
        with write_txn(conn):
            # The warrant must still hold at claim time.
            revalidate_decision_provenance(conn, decision)
            row = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id = ?", (decision.task_id,),
            ).fetchone()
            if row is None:
                raise ExecutionNotPermitted(f"no such task: {decision.task_id}")
            current_run_id = (
                int(row["current_run_id"]) if row["current_run_id"] is not None else None
            )
            claim_id = _record_exec_event(
                conn,
                task_id=decision.task_id,
                run_id=decision.run_id,
                kind=EXEC_EVENT_CLAIMED,
                payload={
                    "run_id": decision.run_id,
                    "task_id": decision.task_id,
                    "current_run_id": current_run_id,
                    "session_id": decision.session_id,
                },
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateExecutionError(
            f"run {decision.run_id} already has an execution claim; refusing to "
            "execute a redelivered decision twice"
        ) from exc

    return ExecutionFence(
        claim_event_id=claim_id,
        run_id=decision.run_id,
        task_id=decision.task_id,
        current_run_id=current_run_id,
    )


def _fence_intact(conn: sqlite3.Connection, fence: ExecutionFence) -> Optional[str]:
    """Return a reason string when the fence moved, else None."""
    claim = conn.execute(
        "SELECT id FROM task_events WHERE id = ? AND kind = ?",
        (fence.claim_event_id, EXEC_EVENT_CLAIMED),
    ).fetchone()
    if claim is None:
        return "claim_event_missing"
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (fence.task_id,)
    ).fetchone()
    if row is None:
        return "task_missing"
    current = int(row["current_run_id"]) if row["current_run_id"] is not None else None
    if current != fence.current_run_id:
        return f"current_run_id_changed:{fence.current_run_id}->{current}"
    return None


def execute_planned_resume(
    conn: sqlite3.Connection,
    *,
    plan: "InvocationPlan",
    binding: SessionBinding,
    executor: ExecutorTransport,
    policy: Optional[ExecutorPolicy] = None,
    now: Optional[int] = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ExecutionOutcome:
    """Drive a FAKE transport for one already-claimed decision. Fails closed.

    Nothing here spawns a process, resumes a session, opens a socket, reads a
    credential, or schedules work. ``executor`` must be a registered fake.

    Sequence, and why each step sits where it does:

    1. :func:`claim_execution_fence` — warrant recheck plus a committed claim.
       Committed *before* the external call so the call holds no lock, and so a
       crash mid-execution leaves a durable record that an execution was
       attempted.
    2. Outside any transaction: binding freshness, then **one** authoritative
       A3/kill check immediately before the call, then the bounded call.
    3. A second transaction: recheck the fence, and only then write the terminal
       outcome through the canonical API with ``expected_run_id`` — the native
       CAS — so a stale executor cannot land a result.
    """
    policy = policy or ExecutorPolicy()
    stamp = int(now if now is not None else time.time())
    fence = claim_execution_fence(conn, plan=plan)

    try:
        # --- step 2: outside any transaction ---------------------------
        validate_binding_freshness(binding, now=stamp)
        if binding.session_id != plan.decision.session_id:
            raise BindingNotFreshError(
                f"binding session {binding.session_id!r} does not match the "
                f"decision session {plan.decision.session_id!r}"
            )
        # One authoritative kill/A3 check, immediately before the call.
        policy.permit(conn, executor, fence.task_id)
        if a3_revocation_latched(conn, fence.task_id):
            raise ExecutionNotPermitted(
                f"A3 revocation is latched for task {fence.task_id}; refusing to "
                "execute"
            )

        timeout_seconds = plan.command.timeout_seconds
        started = monotonic()
        try:
            raw = executor.execute(plan, timeout_seconds=timeout_seconds)
        except (TimeoutError, ExecutionTimeoutError) as exc:
            raise ExecutionTimeoutError(
                f"executor exceeded {timeout_seconds}s"
            ) from exc
        except ExecutorError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure is closed
            raise ExecutorUnavailableError(f"executor failed: {exc}") from exc
        elapsed = monotonic() - started
        if elapsed > timeout_seconds:
            raise ExecutionTimeoutError(
                f"executor took {elapsed:.3f}s, exceeding {timeout_seconds}s"
            )

        status, summary = _validate_execution_result(raw, fence)
        route = EXECUTION_STATUS_TO_ROUTE[status]

        # --- step 3: fence recheck, then canonical terminal routing ----
        with write_txn(conn):
            moved = _fence_intact(conn, fence)
            if moved is None:
                _record_exec_event(
                    conn, task_id=fence.task_id, run_id=fence.run_id,
                    kind=EXEC_EVENT_COMPLETED,
                    payload={"status": status, "route": route, "summary": summary},
                )
        if moved is not None:
            # Recorded in its OWN committed transaction, and only after the
            # one above has closed. Writing the discard inside that block and
            # then raising rolled it back — destroying the only evidence that a
            # result had been discarded.
            with write_txn(conn):
                _record_exec_event(
                    conn, task_id=fence.task_id, run_id=fence.run_id,
                    kind=EXEC_EVENT_DISCARDED, payload={"reason": moved},
                )
            raise ExecutionFenceLost(
                f"fence moved during execution ({moved}); result discarded"
            )

        terminal_write = False
        if route == ROUTE_CLOSE:
            terminal_write = complete_task(
                conn, fence.task_id, summary=summary,
                expected_run_id=fence.current_run_id,
            )
        elif route == ROUTE_BLOCK:
            terminal_write = block_task(
                conn, fence.task_id, reason=summary, kind="needs_input",
                expected_run_id=fence.current_run_id,
            )
        # REVIEW and CONTINUE deliberately write no task status: a verdict and a
        # re-drive are decisions for the loop and a human, not for the executor.

        decision = plan.decision
        notification = project_notification(
            RouteDecision(
                route=route,
                reason=f"execution_{status}",
                task_id=decision.task_id,
                run_id=decision.run_id,
                outcome=decision.outcome,
                session_id=decision.session_id,
                provider=decision.provider,
                spawn=False,
                seat=decision.seat,
            )
        )
        return ExecutionOutcome(
            fence=fence,
            status=status,
            route=route,
            summary=summary,
            terminal_write=bool(terminal_write),
            notification=notification,
            executed_against_real_provider=False,
        )
    except ExecutorError as exc:
        _refuse(
            conn, task_id=fence.task_id, run_id=fence.run_id,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise


def resume_stream_event_schema() -> dict:
    """Return a fresh deep copy of the declared stream-event schema.

    A copy, not the module constant: a caller that mutated the shared dict
    would silently change the contract for every later plan.

    Round-tripped through JSON rather than ``copy.deepcopy`` so this section
    adds no import to the module: the schema is pure JSON by construction, so
    the round trip is an exact deep copy and also asserts that property.
    """
    return json.loads(json.dumps(RESUME_STREAM_EVENT_SCHEMA))


@dataclass(frozen=True)
class SessionBinding:
    """A **declared** provider/session mapping. Never inferred here.

    This is the caller's assertion that ``session_id`` is a real, reusable
    worker session for ``provider``, obtained from a declared source. It is
    validated against the decision it is paired with; it is not looked up,
    discovered, or defaulted.
    """

    provider: str
    session_id: str
    source: str
    seat_id: Optional[str] = None
    #: Validity window and ownership of the mapping itself (G1).
    #:
    #: Deliberately NOT derived from ``task_runs.last_heartbeat_at``: that is
    #: the *worker's* liveness, not the *binding's* validity. Conflating them
    #: would let a heartbeating worker keep a retired mapping alive.
    #:
    #: Defaulted so every existing construction site keeps working. The
    #: defaults are not a valid binding — they fail
    #: :func:`validate_binding_freshness` — so the executor path cannot be
    #: entered by accident with an undeclared window.
    issued_at: int = 0
    expires_at: int = 0
    owner: str = ""
    retired: bool = False


@dataclass(frozen=True)
class ResumeCapsule:
    """A bounded, structured description of the work to resume.

    Deliberately small and typed: identifiers plus one bounded instruction and
    a few bounded notes. No transcript, no payload passthrough, no nested
    free-form structure that could smuggle content past the bounds.
    """

    capsule_version: int
    task_id: str
    run_id: int
    outcome: str
    reason: str
    instruction: str
    notes: tuple[str, ...] = ()

    def to_payload(self) -> dict:
        return {
            "capsule_version": self.capsule_version,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "instruction": self.instruction,
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        """Canonical JSON. Sorted keys so the rendering is deterministic."""
        return json.dumps(
            self.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


@dataclass(frozen=True)
class ResumeCommandSpec:
    """An explicit command specification. Rendering it does not run it.

    ``argv`` is a tuple, not a list, so a holder cannot append to a spec that
    has already been reviewed.
    """

    provider: str
    session_id: str
    argv: tuple[str, ...]
    output_schema_json: str
    timeout_seconds: int
    #: The sole canonical JSONL input record for ``--input-format stream-json``.
    #: It is data only; this module never opens a process or writes stdin.
    input_jsonl: str = ""
    #: Structural marker: nothing in this module ever sets this True.
    executed: bool = False

    def to_payload(self) -> dict:
        return {
            "provider": self.provider,
            "session_id": self.session_id,
            "argv": list(self.argv),
            "output_schema_json": self.output_schema_json,
            "timeout_seconds": self.timeout_seconds,
            "input_jsonl": self.input_jsonl,
            "executed": self.executed,
        }


@dataclass(frozen=True)
class InvocationPlan:
    """An inert plan. Suitable ONLY for a future A3-gated executor.

    ``executed`` is hard-wired False and ``requires_a3_gate`` hard-wired True:
    this module never flips either, so a plan cannot claim to have run, and
    cannot claim to be exempt from the gate that
    :class:`ActionPolicy` already enforces for real action.
    """

    decision: "RouteDecision"
    binding: SessionBinding
    capsule: ResumeCapsule
    command: ResumeCommandSpec
    executed: bool = False
    requires_a3_gate: bool = True

    def to_payload(self) -> dict:
        return {
            "route": self.decision.route,
            "task_id": self.decision.task_id,
            "run_id": self.decision.run_id,
            "provider": self.binding.provider,
            "session_id": self.binding.session_id,
            "session_source": self.binding.source,
            "seat": self.binding.seat_id,
            "capsule": self.capsule.to_payload(),
            "command": self.command.to_payload(),
            "executed": self.executed,
            "requires_a3_gate": self.requires_a3_gate,
        }


#: Characters that must never survive into a rendered capsule or command.
#: Broader than the original ``\r\n\x00`` check: every C0 control, DEL, and the
#: Unicode line-breaking characters, which split lines in some renderers and log
#: pipelines even though ``str.splitlines`` is the only thing most code tests
#: against.
#:
#: ``U+0085`` (NEL) is included explicitly: it is a C1 control, so it sits above
#: the ``< 0x20`` C0 range and would otherwise pass, yet Python's own
#: ``str.splitlines`` treats it as a line break — exactly the class of
#: smuggling this check exists to stop.
_FORBIDDEN_TEXT_CHARS = "  \x7f"


def _has_control_chars(value: str) -> bool:
    return any(ch in _FORBIDDEN_TEXT_CHARS or ord(ch) < 0x20 for ch in value)


def _require_identifier(value: Any, field: str) -> str:
    """A non-empty, single-line, stripped string, or fail closed."""
    if not isinstance(value, str):
        raise InvocationPlanError(
            f"{field} must be a string, got {type(value).__name__}"
        )
    cleaned = value.strip()
    if not cleaned:
        raise InvocationPlanError(f"{field} must be a non-empty string")
    if _has_control_chars(cleaned):
        raise InvocationPlanError(f"{field} must not contain control characters")
    return cleaned


def _require_bounded_text(value: Any, field: str, max_chars: int) -> str:
    """Bounded free-ish text: string, non-blank, control-free, within bound.

    Used for the instruction and for each note. Same rules as
    :func:`_require_identifier` plus an explicit length bound, so the two
    entry points into a capsule — the builder and the plan-boundary
    revalidation — cannot disagree about what is acceptable.
    """
    cleaned = _require_identifier(value, field)
    if len(cleaned) > max_chars:
        raise InvocationPlanError(
            f"{field} exceeds {max_chars} chars ({len(cleaned)})"
        )
    return cleaned


def _validate_timeout(timeout_seconds: Any) -> int:
    """Bounded integer seconds. bool is not an int for this purpose."""
    if isinstance(timeout_seconds, bool):
        raise InvocationPlanError(
            f"timeout_seconds must be an integer, got bool {timeout_seconds!r}"
        )
    if not isinstance(timeout_seconds, int):
        raise InvocationPlanError(
            f"timeout_seconds must be an integer, got {type(timeout_seconds).__name__}"
        )
    if not (RESUME_MIN_TIMEOUT_SECONDS <= timeout_seconds <= RESUME_MAX_TIMEOUT_SECONDS):
        raise InvocationPlanError(
            f"timeout_seconds must be within "
            f"[{RESUME_MIN_TIMEOUT_SECONDS}, {RESUME_MAX_TIMEOUT_SECONDS}], "
            f"got {timeout_seconds}"
        )
    return timeout_seconds


def build_resume_capsule(
    *,
    decision: "RouteDecision",
    instruction: str,
    notes: Iterable[str] = (),
) -> ResumeCapsule:
    """Build a bounded capsule from a CONTINUE decision. **Pure.**

    Every field is validated and bounded here so a malformed capsule can never
    reach :func:`plan_session_resume`.
    """
    if not isinstance(decision, RouteDecision):
        raise InvocationPlanError(
            f"decision must be a RouteDecision, got {type(decision).__name__}"
        )
    if decision.route != ROUTE_CONTINUE:
        raise InvocationPlanError(
            f"capsule requires route {ROUTE_CONTINUE!r}, got {decision.route!r}"
        )

    task_id = _require_identifier(decision.task_id, "decision.task_id")
    if not isinstance(decision.run_id, int) or isinstance(decision.run_id, bool):
        raise InvocationPlanError("decision.run_id must be an integer run id")
    if decision.run_id <= 0:
        raise InvocationPlanError(
            f"decision.run_id must be positive, got {decision.run_id!r}"
        )

    # Same rule the plan boundary applies, so the builder cannot mint a capsule
    # its own revalidation would reject — control characters in the instruction
    # previously passed here and were only caught later.
    cleaned_instruction = _require_bounded_text(
        instruction, "instruction", RESUME_CAPSULE_MAX_INSTRUCTION_CHARS
    )

    if isinstance(notes, (str, bytes)):
        raise InvocationPlanError("notes must be an iterable of strings, not a string")
    cleaned_notes: list[str] = []
    for index, note in enumerate(notes):
        if not isinstance(note, str):
            raise InvocationPlanError(
                f"note must be a string, got {type(note).__name__}"
            )
        if not note.strip():
            # A blank note carries nothing; dropping it is not a silent repair
            # of malformed input, it is omission of an empty item.
            continue
        cleaned_notes.append(
            _require_bounded_text(
                note, f"notes[{index}]", RESUME_CAPSULE_MAX_NOTE_CHARS
            )
        )
    if len(cleaned_notes) > RESUME_CAPSULE_MAX_NOTES:
        raise InvocationPlanError(
            f"at most {RESUME_CAPSULE_MAX_NOTES} notes, got {len(cleaned_notes)}"
        )

    return ResumeCapsule(
        capsule_version=RESUME_CAPSULE_VERSION,
        task_id=task_id,
        run_id=int(decision.run_id),
        outcome=_require_identifier(decision.outcome, "decision.outcome"),
        reason=_require_identifier(decision.reason, "decision.reason"),
        instruction=cleaned_instruction,
        notes=tuple(cleaned_notes),
    )


def _validate_capsule(capsule: Any) -> ResumeCapsule:
    """Re-validate a capsule at the plan boundary. Fail closed on drift.

    ``ResumeCapsule`` is a frozen dataclass, but frozen is not validated:
    ``ResumeCapsule(...)`` can be constructed directly with any field values,
    bypassing :func:`build_resume_capsule` entirely. This boundary therefore
    re-checks **every** field to the same rules the builder applies, rather
    than spot-checking a few.

    The earlier version checked only ``capsule_version``, ``task_id``,
    ``instruction`` (identifier rule), ``run_id`` and the note *count*. A
    directly-constructed capsule could carry a non-string or over-length
    ``outcome``/``reason``, a ``notes`` value that was not a tuple of strings,
    or notes that were blank, control-character-bearing, or over-length. All of
    those now fail closed here.
    """
    if not isinstance(capsule, ResumeCapsule):
        raise InvocationPlanError(
            f"capsule must be a ResumeCapsule, got {type(capsule).__name__}"
        )
    if isinstance(capsule.capsule_version, bool) or not isinstance(
        capsule.capsule_version, int
    ):
        raise InvocationPlanError(
            "capsule.capsule_version must be an integer, got "
            f"{type(capsule.capsule_version).__name__}"
        )
    if capsule.capsule_version != RESUME_CAPSULE_VERSION:
        raise InvocationPlanError(
            f"unsupported capsule_version {capsule.capsule_version!r}; "
            f"this contract renders version {RESUME_CAPSULE_VERSION}"
        )

    _require_identifier(capsule.task_id, "capsule.task_id")
    _require_identifier(capsule.outcome, "capsule.outcome")
    _require_identifier(capsule.reason, "capsule.reason")
    _require_bounded_text(
        capsule.instruction, "capsule.instruction", RESUME_CAPSULE_MAX_INSTRUCTION_CHARS
    )

    if not isinstance(capsule.run_id, int) or isinstance(capsule.run_id, bool):
        raise InvocationPlanError("capsule.run_id must be an integer run id")
    if capsule.run_id <= 0:
        raise InvocationPlanError(
            f"capsule.run_id must be positive, got {capsule.run_id!r}"
        )

    # The container itself, before its contents: a list is mutable and a bare
    # string is an iterable of characters, so neither may stand in for the
    # declared tuple.
    if not isinstance(capsule.notes, tuple):
        raise InvocationPlanError(
            f"capsule.notes must be a tuple, got {type(capsule.notes).__name__}"
        )
    if len(capsule.notes) > RESUME_CAPSULE_MAX_NOTES:
        raise InvocationPlanError("capsule carries too many notes")
    for index, note in enumerate(capsule.notes):
        _require_bounded_text(
            note, f"capsule.notes[{index}]", RESUME_CAPSULE_MAX_NOTE_CHARS
        )
    return capsule


def _validate_binding(binding: Any, decision: "RouteDecision") -> SessionBinding:
    """Validate a declared mapping against the decision it is paired with.

    Three independent things must agree before a resume can even be described:
    the provider must be one this contract can render, the session source must
    be continue-eligible (``inferred`` never is), and the mapping must match
    the decision's own session/provider. Any disagreement fails closed rather
    than picking a winner.
    """
    if not isinstance(binding, SessionBinding):
        raise InvocationPlanError(
            f"binding must be a SessionBinding, got {type(binding).__name__}"
        )

    provider = _require_identifier(binding.provider, "binding.provider")
    if provider not in RESUME_CAPABLE_PROVIDERS:
        raise InvocationPlanError(
            f"provider {provider!r} is not resume-capable in this contract "
            f"(known: {sorted(RESUME_CAPABLE_PROVIDERS)})"
        )

    session_id = _require_identifier(binding.session_id, "binding.session_id")

    source = _require_identifier(binding.source, "binding.source")
    if source not in VALID_SESSION_SOURCES:
        raise InvocationPlanError(f"unknown session source {source!r}")
    if source not in CONTINUE_ELIGIBLE_SESSION_SOURCES:
        raise InvocationPlanError(
            f"session source {source!r} is not continue-eligible; refusing to "
            "describe a resume for an unprovenanced session"
        )

    # The decision carries its own view of provider/session. If it has one and
    # it disagrees with the declared mapping, that is exactly the ambiguity
    # that must not be resolved silently.
    if decision.session_id is not None and decision.session_id != session_id:
        raise InvocationPlanError(
            f"binding session {session_id!r} does not match decision session "
            f"{decision.session_id!r}"
        )
    if decision.provider is not None and decision.provider != provider:
        raise InvocationPlanError(
            f"binding provider {provider!r} does not match decision provider "
            f"{decision.provider!r}"
        )

    seat_id = binding.seat_id
    if seat_id is not None:
        seat_id = _require_identifier(seat_id, "binding.seat_id")
        if decision.seat is not None and decision.seat != seat_id:
            raise InvocationPlanError(
                f"binding seat {seat_id!r} does not match decision seat "
                f"{decision.seat!r}"
            )

    # Carry the G1 window through. Rebuilding without it let `plan.binding`
    # report owner='' / retired=False for a mapping that was in fact retired —
    # a fabrication. It failed *safe* (an empty owner never satisfies
    # validate_binding_freshness), but a plan must not misdescribe its own
    # binding. Freshness is still checked at execution time, not here.
    return SessionBinding(
        provider=provider, session_id=session_id, source=source, seat_id=seat_id,
        issued_at=binding.issued_at, expires_at=binding.expires_at,
        owner=binding.owner, retired=binding.retired,
    )


def render_resume_command(
    *,
    provider: str,
    session_id: str,
    timeout_seconds: int = RESUME_DEFAULT_TIMEOUT_SECONDS,
) -> ResumeCommandSpec:
    """Deterministically render the resume command spec. **Pure.**

    Same inputs always produce the same ``argv``, in the same order. Nothing is
    read from the environment, so the rendering cannot drift with the host.

    The flags are fixed by contract, not composed from options:

      ``--resume <session-id>``   reuse the existing session, never create one
      ``--print``                 non-interactive, single result
      ``--input-format stream-json``   structured capsule in, not loose prose
      ``--output-format stream-json``  machine-parseable event stream
      ``--include-hook-events``   hook events are part of the observable record
      ``--permission-mode plan``  provider-enforced no-mutation/no-shell mode
      ``--max-turns 1``           one bounded response, never an agent loop
      ``--disallowedTools …``     deny the built-in execution/read/network tools
      ``--safe-mode``             disable hooks, skills, plugins and custom MCP
      ``--strict-mcp-config``     ignore every MCP source unless explicitly given

    The output schema is carried on the spec (``output_schema_json``) rather
    than as a CLI flag — see :data:`RESUME_STREAM_EVENT_SCHEMA`.
    """
    if provider not in RESUME_CAPABLE_PROVIDERS:
        raise InvocationPlanError(
            f"provider {provider!r} is not resume-capable in this contract"
        )
    session = _require_identifier(session_id, "session_id")
    timeout = _validate_timeout(timeout_seconds)

    argv: tuple[str, ...] = (
        "claude",
        "--resume",
        session,
        "--print",
        # Claude CLI requires verbose stream records when --print is paired
        # with stream-json output. Without this the real transport exits
        # before emitting the bound init/result pair the parser requires.
        "--verbose",
        # Structured BOTH ways. Without --input-format the capsule would have to
        # be handed over as loose prose on stdin, which is exactly the
        # unstructured channel this contract exists to avoid.
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--include-hook-events",
        # The only real transport currently admitted by the activation packet
        # is the harmless control-plane echo.  Retain this restriction in the
        # canonical command rather than trusting capsule prose.  A future
        # task class with tools must use a separate reviewed adapter contract.
        "--permission-mode",
        "plan",
        "--disallowedTools",
        "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task",
        "--safe-mode",
        "--strict-mcp-config",
        "--max-turns",
        "1",
    )
    return ResumeCommandSpec(
        provider=provider,
        session_id=session,
        argv=argv,
        output_schema_json=json.dumps(
            RESUME_STREAM_EVENT_SCHEMA,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        timeout_seconds=timeout,
        executed=False,
    )


def render_claude_stream_input(capsule: ResumeCapsule) -> str:
    """Render exactly one canonical Claude Code ``stream-json`` user record.

    Claude Code's documented stream input is JSON Lines containing a user
    message envelope.  The capsule is validated before serialisation, framed
    as data rather than instructions supplied by a caller, and emitted as one
    newline-terminated record.  This is deliberately a pure renderer: it does
    not open stdin, create a process, or contact a provider.
    """
    checked = _validate_capsule(capsule)
    content = (
        "Hermes resume capsule (schema v1; bounded task data):\n"
        + checked.to_json()
    )
    envelope = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": content}],
        },
    }
    return json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"


def plan_session_resume(
    *,
    decision: "RouteDecision",
    binding: SessionBinding,
    capsule: ResumeCapsule,
    timeout_seconds: int = RESUME_DEFAULT_TIMEOUT_SECONDS,
) -> InvocationPlan:
    """Build an INERT session-resume plan. **Pure; executes nothing.**

    Accepts only a validated CONTINUE :class:`RouteDecision` plus a declared
    :class:`SessionBinding`, and returns a plan a future **separately
    A3-gated** executor could act on. This function does not act on it, and
    nothing else in this module consumes an :class:`InvocationPlan`.

    Fails closed on: a non-CONTINUE decision, a decision that claims to spawn,
    an unknown or non-resume-capable provider, a missing/blank session id or
    task id, provenance that is not continue-eligible, a binding that
    contradicts the decision, an out-of-range timeout, or a malformed capsule.
    """
    if not isinstance(decision, RouteDecision):
        raise InvocationPlanError(
            f"decision must be a RouteDecision, got {type(decision).__name__}"
        )
    if decision.route != ROUTE_CONTINUE:
        raise InvocationPlanError(
            f"only a {ROUTE_CONTINUE!r} decision can be resumed, got "
            f"{decision.route!r}"
        )
    if decision.spawn is not False:
        # Structural guard mirroring validate_broker_event_payload: this slice
        # cannot express a spawning decision, so it cannot plan one either.
        raise InvocationPlanError("decision.spawn must be False in this slice")

    _require_identifier(decision.task_id, "decision.task_id")
    checked_binding = _validate_binding(binding, decision)
    checked_capsule = _validate_capsule(capsule)

    if checked_capsule.task_id != decision.task_id.strip():
        raise InvocationPlanError(
            f"capsule task {checked_capsule.task_id!r} does not match decision "
            f"task {decision.task_id!r}"
        )
    if checked_capsule.run_id != decision.run_id:
        raise InvocationPlanError(
            f"capsule run {checked_capsule.run_id!r} does not match decision "
            f"run {decision.run_id!r}"
        )

    command = render_resume_command(
        provider=checked_binding.provider,
        session_id=checked_binding.session_id,
        timeout_seconds=timeout_seconds,
    )
    return InvocationPlan(
        decision=decision,
        binding=checked_binding,
        capsule=checked_capsule,
        command=command,
        executed=False,
        requires_a3_gate=True,
    )


# ---------------------------------------------------------------------------
# Provider-adapter slice — persisted mapping, request preparation, result
# interpretation. Source-only: nothing here invokes a provider.
#
# The executor slice above still accepts a caller-supplied SessionBinding. That
# is the weakness this slice closes: a mapping is now a persisted, durable,
# dispatcher-owned row, and `prepare_resume_request` will read it from nowhere
# else. An in-memory binding can no longer authorise a resume.
#
# Ordering note: the binding and A3 checks run BEFORE the fence claim. Claiming
# first would burn the run's one idempotency slot on a request that was never
# admissible, and — because the claim index is UNIQUE per run — no corrected
# retry could ever claim again. Validation failures therefore cost nothing; only
# an admissible request consumes the claim, and the ResumeRequest object is only
# constructed after that claim has COMMITTED.
# ---------------------------------------------------------------------------

class BindingNotFoundError(BindingNotFreshError):
    """No persisted mapping for this run. Unknown is not a licence to guess."""


class BindingConflictError(BindingNotFreshError):
    """A different live mapping already exists for this run."""


def record_session_binding(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    task_id: str,
    provider: str,
    session_id: str,
    source: str,
    issued_at: int,
    expires_at: int,
    owner: str = DISPATCHER_BINDING_OWNER,
    seat_id: Optional[str] = None,
    now: Optional[int] = None,
) -> None:
    """Persist a dispatcher-owned mapping. Idempotent; never silently rebinds.

    Re-recording an identical mapping is a no-op, so a redelivered registration
    is harmless. Recording a *different* session for a run that already has a
    live one raises :class:`BindingConflictError` — the ambiguity is exactly
    what must not be resolved by last-writer-wins.
    """
    stamp = int(time.time()) if now is None else int(now)
    provider = _require_identifier(provider, "provider")
    if provider not in RESUME_CAPABLE_PROVIDERS:
        raise InvocationPlanError(
            f"provider {provider!r} is not resume-capable in this contract"
        )
    session_id = _require_identifier(session_id, "session_id")
    task_id = _require_identifier(task_id, "task_id")
    source = _require_identifier(source, "source")
    if source not in VALID_SESSION_SOURCES:
        raise InvocationPlanError(f"unknown session source {source!r}")
    if source not in CONTINUE_ELIGIBLE_SESSION_SOURCES:
        # `inferred` provenance can never be stored, so it can never be loaded.
        raise InvocationPlanError(
            f"session source {source!r} is not continue-eligible; refusing to "
            "persist an unprovenanced mapping"
        )
    owner = _require_identifier(owner, "owner")
    for field, value in (("issued_at", issued_at), ("expires_at", expires_at)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvocationPlanError(f"{field} must be a positive integer")
    if expires_at <= issued_at:
        raise InvocationPlanError("expires_at must be after issued_at")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise InvocationPlanError("run_id must be a positive integer")
    if seat_id is not None:
        seat_id = _require_identifier(seat_id, "seat_id")

    with write_txn(conn):
        row = conn.execute(
            "SELECT provider, session_id, source, seat_id, owner, issued_at, "
            "expires_at, retired_at, task_id FROM kanban_session_bindings "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is not None:
            same = (
                row["provider"] == provider
                and row["session_id"] == session_id
                and row["source"] == source
                and row["seat_id"] == seat_id
                and row["owner"] == owner
                and int(row["issued_at"]) == issued_at
                and int(row["expires_at"]) == expires_at
                and row["task_id"] == task_id
            )
            if same:
                return  # idempotent re-registration
            if row["retired_at"] is None:
                raise BindingConflictError(
                    f"run {run_id} already has a live mapping to session "
                    f"{row['session_id']!r}; refusing to rebind to "
                    f"{session_id!r}"
                )
            conn.execute(
                "DELETE FROM kanban_session_bindings WHERE run_id = ?", (run_id,)
            )
        conn.execute(
            "INSERT INTO kanban_session_bindings "
            "(run_id, task_id, provider, session_id, source, seat_id, owner, "
            " issued_at, expires_at, retired_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (run_id, task_id, provider, session_id, source, seat_id, owner,
             issued_at, expires_at, stamp),
        )


def retire_session_binding(
    conn: sqlite3.Connection, *, run_id: int, now: Optional[int] = None,
) -> bool:
    """Retire a mapping durably. Returns False if there was nothing live."""
    stamp = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_session_bindings SET retired_at = ? "
            "WHERE run_id = ? AND retired_at IS NULL",
            (stamp, run_id),
        )
        return cur.rowcount == 1


def load_session_binding(
    conn: sqlite3.Connection, *, run_id: int, task_id: Optional[str] = None,
) -> SessionBinding:
    """Load the persisted mapping for a run, or fail closed.

    Read-only. Returns the mapping as recorded — including retirement — so the
    caller's freshness check sees the real state rather than a reconstruction.
    """
    row = conn.execute(
        "SELECT task_id, provider, session_id, source, seat_id, owner, "
        "issued_at, expires_at, retired_at FROM kanban_session_bindings "
        "WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise BindingNotFoundError(
            f"no persisted session mapping for run {run_id}; refusing to infer one"
        )
    if task_id is not None and row["task_id"] != task_id:
        raise BindingConflictError(
            f"mapping for run {run_id} belongs to task {row['task_id']!r}, "
            f"not {task_id!r}"
        )
    return SessionBinding(
        provider=row["provider"],
        session_id=row["session_id"],
        source=row["source"],
        seat_id=row["seat_id"],
        issued_at=int(row["issued_at"]),
        expires_at=int(row["expires_at"]),
        owner=row["owner"],
        retired=row["retired_at"] is not None,
    )


@dataclass(frozen=True)
class ResumeRequest:
    """An inert, fully-prepared resume request backed by a committed claim.

    ``executed`` is hard-wired False: preparing a request is not performing one,
    and nothing in this module flips it.
    """

    fence: ExecutionFence
    plan: InvocationPlan
    binding: SessionBinding
    prepared_at: int
    executed: bool = False

    @property
    def argv(self) -> tuple[str, ...]:
        return self.plan.command.argv

    def to_payload(self) -> dict:
        return {
            "run_id": self.fence.run_id,
            "task_id": self.fence.task_id,
            "claim_event_id": self.fence.claim_event_id,
            "prepared_at": self.prepared_at,
            "executed": self.executed,
            "plan": self.plan.to_payload(),
        }


def prepare_resume_request(
    conn: sqlite3.Connection,
    *,
    decision: "RouteDecision",
    instruction: str,
    now: int,
    notes: Iterable[str] = (),
    timeout_seconds: int = RESUME_DEFAULT_TIMEOUT_SECONDS,
) -> ResumeRequest:
    """Prepare an explicit-session resume request. **Invokes nothing.**

    Reads the mapping from :func:`load_session_binding` only. Fails closed on an
    unknown, stale, retired, or mismatched mapping, on an A3 revocation, and on
    a duplicate claim.
    """
    if not isinstance(decision, RouteDecision):
        raise InvocationPlanError(
            f"decision must be a RouteDecision, got {type(decision).__name__}"
        )
    if decision.route != ROUTE_CONTINUE:
        raise ExecutionNotPermitted(
            f"only a {ROUTE_CONTINUE!r} decision may be prepared, got "
            f"{decision.route!r}"
        )
    task_id = _require_identifier(decision.task_id, "decision.task_id")

    try:
        # 1. Persisted mapping only — never the caller's word for it.
        binding = load_session_binding(
            conn, run_id=decision.run_id, task_id=task_id,
        )
        # 2. Fresh, non-retired, dispatcher-owned, at *preparation* time.
        validate_binding_freshness(binding, now=now)
        # 3. The kill switch outranks a valid mapping.
        if a3_revocation_latched(conn, task_id):
            raise ExecutionNotPermitted(
                f"A3 revocation is latched for {task_id}; refusing to prepare"
            )
        # 4. Pure construction: capsule -> plan -> rendered argv.
        capsule = build_resume_capsule(
            decision=decision, instruction=instruction, notes=notes,
        )
        plan = plan_session_resume(
            decision=decision, binding=binding, capsule=capsule,
            timeout_seconds=timeout_seconds,
        )
    except (ExecutorError, InvocationPlanError) as exc:
        _refuse(conn, task_id=task_id, run_id=decision.run_id, reason=str(exc))
        raise

    # 5. Durable atomic claim. Commits before the request object exists.
    try:
        fence = claim_execution_fence(conn, plan=plan)
    except ExecutorError as exc:
        _refuse(conn, task_id=task_id, run_id=decision.run_id, reason=str(exc))
        raise

    return ResumeRequest(
        fence=fence, plan=plan, binding=binding, prepared_at=int(now),
        executed=False,
    )


@dataclass(frozen=True)
class TerminalInterpretation:
    """The typed reading of an adapter's terminal result."""

    route: str
    status: str
    summary: str
    terminal_write: bool
    notification: NotificationProjection


class UnsealedResultError(ExecutorError):
    """A result arrived without proof that a trusted adapter produced it."""


@dataclass(frozen=True)
class AdapterReceipt:
    """Sealed evidence that a *registered* adapter produced this result.

    The seal is the ``adapter`` reference itself, not a flag on the payload: a
    receipt is only honoured when the interpreting policy still holds that exact
    object (``is``). This is the M8 lesson applied to the result path — a
    payload claiming ``{"simulated": true}`` proves nothing, and neither does a
    receipt whose adapter merely resembles a fake.

    Cannot be constructed meaningfully by a caller: :func:`seal_adapter_result`
    is the only mint, and it refuses any adapter the policy does not vouch for.
    """

    adapter: Any
    request: ResumeRequest
    result: Any
    sealed_at: int


def seal_adapter_result(
    conn: sqlite3.Connection,
    *,
    adapter: Any,
    request: ResumeRequest,
    result: Any,
    policy: ExecutorPolicy,
    now: Optional[int] = None,
) -> AdapterReceipt:
    """Mint a receipt. Only a policy-registered adapter may seal a result.

    Reuses :meth:`ExecutorPolicy.permit`, so a real adapter still needs both
    ``allow_real_execution`` and a positive A3 gate — and there is no real
    adapter in this tree. Real invocation stays disabled; the extension point
    is preserved rather than removed.
    """
    if not isinstance(request, ResumeRequest):
        raise ExecutionResultInvalid(
            f"request must be a ResumeRequest, got {type(request).__name__}"
        )
    policy.permit(conn, adapter, request.fence.task_id)
    return AdapterReceipt(
        adapter=adapter, request=request, result=result,
        sealed_at=int(time.time()) if now is None else int(now),
    )


def interpret_terminal_result(
    conn: sqlite3.Connection,
    *,
    receipt: AdapterReceipt,
    policy: ExecutorPolicy,
    now: Optional[int] = None,
) -> TerminalInterpretation:
    """Route a sealed terminal result through canonical Hermes APIs only.

    Requires an :class:`AdapterReceipt`, never a bare mapping. A plain dict, an
    unsealed request, or a receipt naming an adapter this policy does not hold
    is refused **before** any status write, so there is no callable path from
    fabricated data to a mutated board.

    Status writes go through :func:`complete_task` / :func:`block_task` with
    ``expected_run_id`` — never raw SQL.
    """
    if not isinstance(receipt, AdapterReceipt):
        raise UnsealedResultError(
            f"terminal interpretation requires an AdapterReceipt, got "
            f"{type(receipt).__name__}; a bare result cannot mutate the board"
        )
    request = receipt.request
    if not isinstance(request, ResumeRequest):
        raise UnsealedResultError(
            f"receipt.request must be a ResumeRequest, got {type(request).__name__}"
        )
    fence = request.fence
    # Re-verify at interpretation time. A receipt is not a bearer token: the
    # policy doing the writing must itself vouch for the adapter, so a receipt
    # cannot be replayed under a policy that never trusted it.
    if not policy.is_registered_fake(receipt.adapter):
        try:
            policy.permit(conn, receipt.adapter, fence.task_id)
        except ExecutorError as exc:
            _refuse(conn, task_id=fence.task_id, run_id=fence.run_id, reason=str(exc))
            raise
    try:
        status, summary = _validate_execution_result(receipt.result, fence)
    except ExecutorError as exc:
        _refuse(conn, task_id=fence.task_id, run_id=fence.run_id, reason=str(exc))
        raise
    route = EXECUTION_STATUS_TO_ROUTE[status]

    # Terminal-time kill recheck — UNIVERSAL, and the only one on this path.
    #
    # A3 can latch *after* the request was prepared and after the receipt was
    # sealed. The registered-fake branch above deliberately skips
    # `policy.permit`, so a fake-sealed receipt reached the canonical terminal
    # write with no revocation check at all: prepare-time and real-adapter
    # checks are both blind to a latch that lands in between. This is checked
    # regardless of adapter type, and inside the same transaction as the fence
    # observation, so the window before the terminal write is as small as the
    # canonical APIs allow (`complete_task` opens its own transaction and
    # cannot be nested here).
    #
    # Ordering: the latch is evaluated BEFORE `_fence_intact` and before the
    # completion event, so a revoked task records no completion, performs no
    # terminal write, and produces no notification. The fence itself is left
    # exactly as it was — the only mutation is the append-only refusal below.
    with write_txn(conn):
        a3_latched = a3_revocation_latched(conn, fence.task_id)
        moved = None
        if not a3_latched:
            moved = _fence_intact(conn, fence)
            if moved is None:
                # NON-terminal on purpose. The terminal transition below can
                # still be refused by the in-transaction A3 guard; writing
                # EXEC_EVENT_COMPLETED here would leave "completed, then
                # refused" in the log — a lifecycle that never happened.
                _record_exec_event(
                    conn, task_id=fence.task_id, run_id=fence.run_id,
                    kind=EXEC_EVENT_VALIDATED,
                    payload={"status": status, "route": route, "summary": summary},
                )
    if a3_latched:
        reason = (
            f"A3 revocation is latched for {fence.task_id}; refusing terminal "
            "interpretation"
        )
        _refuse(conn, task_id=fence.task_id, run_id=fence.run_id, reason=reason)
        raise ExecutionNotPermitted(reason)
    if moved is not None:
        with write_txn(conn):
            _record_exec_event(
                conn, task_id=fence.task_id, run_id=fence.run_id,
                kind=EXEC_EVENT_DISCARDED, payload={"reason": moved},
            )
        raise ExecutionFenceLost(
            f"fence moved during execution ({moved}); result discarded"
        )

    # The check above cannot be the last word: it commits, and the canonical
    # writers open their OWN transaction (write_txn is not re-entrant), so A3
    # could latch in that gap. `a3_guard=True` re-evaluates the latch *inside*
    # the same transaction as the expected_run_id CAS — the only placement that
    # is genuinely terminal-time. On a latch the writer raises and its
    # transaction rolls back, so no partial mutation survives.
    terminal_write = False
    try:
        if route == ROUTE_CLOSE:
            terminal_write = complete_task(
                conn, fence.task_id, summary=summary,
                expected_run_id=fence.current_run_id, a3_guard=True,
            )
        elif route == ROUTE_BLOCK:
            terminal_write = block_task(
                conn, fence.task_id, reason=summary, kind="needs_input",
                expected_run_id=fence.current_run_id, a3_guard=True,
            )
    except ExecutionNotPermitted as exc:
        _refuse(conn, task_id=fence.task_id, run_id=fence.run_id, reason=str(exc))
        raise
    # REVIEW and CONTINUE deliberately write no task status.

    # Terminal event recorded ONLY now — after the canonical guarded transition
    # actually succeeded (or was legitimately a no-op for REVIEW/CONTINUE). The
    # log therefore never claims a completion that the A3 guard refused.
    with write_txn(conn):
        _record_exec_event(
            conn, task_id=fence.task_id, run_id=fence.run_id,
            kind=EXEC_EVENT_COMPLETED,
            payload={"status": status, "route": route, "summary": summary,
                     "terminal_write": terminal_write},
        )

    decision = request.plan.decision
    notification = project_notification(
        RouteDecision(
            route=route,
            reason=f"adapter_{status}",
            task_id=decision.task_id,
            run_id=decision.run_id,
            outcome=decision.outcome,
            session_id=decision.session_id,
            provider=decision.provider,
            spawn=False,
            seat=decision.seat,
        )
    )
    return TerminalInterpretation(
        route=route, status=status, summary=summary,
        terminal_write=terminal_write, notification=notification,
    )


# ---------------------------------------------------------------------------
# Provider adapter boundary — provider-neutral contract, Claude Code first.
#
# DISABLED BY DEFAULT AND UNARMED. `ClaudeCodeAdapter.execute` refuses
# deterministically on every path in this tree; there is no transport behind it.
# Nothing in this section imports or references subprocess, shells, sockets,
# HTTP, environment/credential reads, hooks, schedulers, cron, services, or a
# provider CLI. (The *module* imports subprocess for unrelated legacy dispatch
# code that long predates this slice — the adapter path itself is scanned for
# those tokens by test, which is the honest scope of the guarantee.)
# ---------------------------------------------------------------------------

class AdapterExecutionDisabled(ExecutorError):
    """The adapter refused to execute. Always, in this tree."""


class ProviderOutputInvalid(ExecutorError):
    """Provider output was structurally malformed. Fails closed."""


class ProviderAdapter(Protocol):
    """Narrow provider-neutral contract.

    Deliberately smaller than :class:`ExecutorTransport`: an adapter may only
    *describe* an invocation and *interpret* an outcome. It is handed an
    already-validated plan (which carries the persisted session mapping and a
    typed CONTINUE decision) and can obtain nothing else on its own — no
    discovery, no environment, no session enumeration.
    """

    name: str
    provider: str

    def build_command(
        self, plan: "InvocationPlan"
    ) -> "ResumeCommandSpec":  # pragma: no cover - protocol
        ...

    def execute(
        self, plan: "InvocationPlan", *, timeout_seconds: int
    ) -> dict:  # pragma: no cover - protocol
        ...


#: Structurally-valid but semantically inconclusive provider outcomes. These are
#: normalised to ``needs_review``, which routes to ROUTE_REVIEW and writes **no**
#: task status. Chosen over ``blocked`` deliberately: a block is itself a
#: terminal board write, and an ambiguous or unavailable provider outcome must
#: not mutate the board in either direction.
AMBIGUOUS_PROVIDER_SUBTYPES = frozenset({
    "error_max_turns",
    "error_during_execution",
    "error",
    "cancelled",
    "timeout",
    "unavailable",
})

#: Subtypes that map to a definite terminal reading.
CLAUDE_SUBTYPE_TO_STATUS = {
    "success": "completed",
    "blocked": "blocked",
    "needs_review": "needs_review",
    "incomplete": "incomplete",
}


def parse_claude_stream_output(
    events: Any, *, expected_run_id: int, expected_session_id: str,
) -> dict:
    """Parse Claude Code ``stream-json`` events into a typed terminal result.

    **Pure.** Takes already-captured structured events — never a stream, a file
    handle, a process, or text to be scraped. Fails closed on anything
    structurally malformed; normalises anything merely *inconclusive* to
    ``needs_review`` so it routes to review without a terminal write.

    Returns the canonical ``{"status", "summary", "run_id"}`` mapping the sealed
    interpretation path already validates, so no new result vocabulary is
    introduced downstream.
    """
    if isinstance(events, (str, bytes, Mapping)) or not isinstance(events, Iterable):
        raise ProviderOutputInvalid(
            f"provider output must be a sequence of event mappings, got "
            f"{type(events).__name__}"
        )
    items = list(events)
    if not items:
        raise ProviderOutputInvalid("provider output carried no events")

    if not isinstance(expected_run_id, int) or isinstance(expected_run_id, bool):
        raise ProviderOutputInvalid("expected_run_id must be a positive integer")
    if expected_run_id <= 0:
        raise ProviderOutputInvalid("expected_run_id must be positive")
    expected_session = _require_identifier(
        expected_session_id, "expected_session_id"
    )

    init_events = []
    results = []
    for index, event in enumerate(items):
        if not isinstance(event, Mapping):
            raise ProviderOutputInvalid(
                f"event {index} is {type(event).__name__}, not a mapping"
            )
        kind = event.get("type")
        if not isinstance(kind, str) or not kind.strip():
            raise ProviderOutputInvalid(f"event {index} has no usable 'type'")
        kind = kind.strip()
        if kind not in {"system", "assistant", "result", "rate_limit_event"}:
            raise ProviderOutputInvalid(
                f"event {index} has unsupported Claude stream type {kind!r}"
            )
        if kind == "rate_limit_event":
            # Claude Code emits this normal lifecycle event between assistant
            # output and the terminal result.  It is informational, never a
            # completion signal, but it still has to be bound to the persisted
            # session so a foreign stream cannot be mixed into this receipt.
            rate_session = _require_identifier(
                event.get("session_id"), "rate_limit_event.session_id"
            )
            if rate_session != expected_session:
                raise ProviderOutputInvalid(
                    "rate_limit_event session_id does not match persisted binding"
                )
            if not isinstance(event.get("rate_limit_info"), Mapping):
                raise ProviderOutputInvalid(
                    "rate_limit_event must carry a mapping rate_limit_info"
                )
        if kind == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                init_events.append(event)
            elif subtype in {"hook_started", "hook_response"}:
                # Claude's normal --include-hook-events stream may carry
                # SessionStart/Stop lifecycle records before or after init.
                # They are non-terminal and never drive routing, but must be
                # bound to the persisted session so a foreign hook stream
                # cannot be mixed into this receipt.
                hook_session = _require_identifier(
                    event.get("session_id"), "hook system.session_id"
                )
                if hook_session != expected_session:
                    raise ProviderOutputInvalid(
                        "hook system session_id does not match persisted binding"
                    )
            else:
                raise ProviderOutputInvalid(
                    "system event must be init or a supported Claude hook lifecycle event; "
                    f"got {subtype!r}"
                )
        if kind == "result":
            results.append(event)

    if len(init_events) != 1:
        raise ProviderOutputInvalid(
            f"provider output carried {len(init_events)} init events; expected one"
        )
    init_session = _require_identifier(init_events[0].get("session_id"), "init.session_id")
    if init_session != expected_session:
        raise ProviderOutputInvalid("init session_id does not match persisted binding")

    if not results:
        raise ProviderOutputInvalid("provider output carried no terminal result event")
    if len(results) > 1:
        # Two terminal readings is not something to pick a winner from.
        raise ProviderOutputInvalid(
            f"provider output carried {len(results)} terminal result events"
        )

    result = results[0]
    result_session = _require_identifier(result.get("session_id"), "result.session_id")
    if result_session != expected_session:
        raise ProviderOutputInvalid("result session_id does not match persisted binding")
    subtype = result.get("subtype")
    if not isinstance(subtype, str) or not subtype.strip():
        raise ProviderOutputInvalid("terminal result event has no usable 'subtype'")
    subtype = subtype.strip()

    is_error = result.get("is_error", False)
    if not isinstance(is_error, bool):
        raise ProviderOutputInvalid("terminal result 'is_error' must be a boolean")

    text = result.get("result", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ProviderOutputInvalid(
            f"terminal result 'result' must be a string, got {type(text).__name__}"
        )

    if is_error or subtype in AMBIGUOUS_PROVIDER_SUBTYPES:
        status = "needs_review"
        summary = f"[{subtype}] {text}".strip() if text else f"[{subtype}]"
    elif subtype in CLAUDE_SUBTYPE_TO_STATUS:
        status = CLAUDE_SUBTYPE_TO_STATUS[subtype]
        summary = text
    else:
        # Unknown-but-well-formed: inconclusive, never assumed successful.
        status = "needs_review"
        summary = f"[unknown subtype {subtype!r}] {text}".strip()

    if not summary.strip():
        summary = f"[{subtype}] provider returned no summary text"
    summary = _require_bounded_text(
        summary, "provider.summary", EXECUTION_MAX_SUMMARY_CHARS,
    )
    return {"status": status, "summary": summary, "run_id": expected_run_id}


class ClaudeCodeAdapter:
    """First concrete adapter. **Cannot invoke anything in this tree.**

    ``enabled`` defaults False. Even constructed with ``enabled=True`` the
    adapter still refuses: there is no transport linked behind it. Both refusals
    are deterministic and typed, so a caller can never fall through to a shell,
    a spawn, or a network call — the extension point exists, the capability
    does not.
    """

    name = "claude-code-adapter"
    provider = PROVIDER_CLAUDE_CODE

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self.build_calls = 0
        self.execute_calls = 0

    def build_command(self, plan: "InvocationPlan") -> "ResumeCommandSpec":
        """Render the explicit resume command. **Pure; runs nothing.**

        The session id is taken from the plan's persisted binding only. There is
        no discovery, no `--continue`, no inference, and no fallback spawn: an
        unusable mapping raises rather than degrading to a new session.
        """
        self.build_calls += 1
        if not isinstance(plan, InvocationPlan):
            raise InvocationPlanError(
                f"plan must be an InvocationPlan, got {type(plan).__name__}"
            )

        # InvocationPlan is a frozen dataclass, not an unforgeable capability.
        # A caller can construct one directly, so this adapter boundary must
        # rebuild its canonical form before it renders even an inert command.
        # Otherwise a BLOCK decision paired with an inferred/retired mapping
        # could manufacture an apparently-authoritative --resume argv for a
        # session that never came from the dispatcher path.
        canonical = plan_session_resume(
            decision=plan.decision,
            binding=plan.binding,
            capsule=plan.capsule,
            timeout_seconds=plan.command.timeout_seconds,
        )
        if canonical != plan:
            raise InvocationPlanError(
                "InvocationPlan is not canonical; refusing a forged or "
                "internally inconsistent resume plan"
            )
        if canonical.binding.retired:
            raise InvocationPlanError("cannot render a command for a retired mapping")
        if canonical.binding.owner != DISPATCHER_BINDING_OWNER:
            raise InvocationPlanError(
                "only a dispatcher-owned mapping may render a resume command"
            )
        if canonical.binding.issued_at <= 0 or canonical.binding.expires_at <= canonical.binding.issued_at:
            raise InvocationPlanError("binding has no plausible eligible time window")
        if canonical.binding.provider != self.provider:
            raise InvocationPlanError(
                f"adapter handles {self.provider!r}, not "
                f"{canonical.binding.provider!r}"
            )
        command = render_resume_command(
            provider=canonical.binding.provider,
            session_id=canonical.binding.session_id,
            timeout_seconds=canonical.command.timeout_seconds,
        )
        return replace(command, input_jsonl=render_claude_stream_input(canonical.capsule))

    def execute(self, plan: "InvocationPlan", *, timeout_seconds: int) -> dict:
        """Always refuses. There is no execution path in this slice."""
        self.execute_calls += 1
        if not self.enabled:
            raise AdapterExecutionDisabled(
                f"{self.name} is disabled (enabled=False); refusing to execute"
            )
        raise AdapterExecutionDisabled(
            f"{self.name} has no transport linked in this tree; refusing to "
            "execute even though enabled=True"
        )


# ---------------------------------------------------------------------------
# Retention + garbage collection
# ---------------------------------------------------------------------------

def gc_events(
    conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600,
) -> int:
    """Delete task_events rows older than ``older_than_seconds`` for tasks
    in a terminal state (``done`` or ``archived``). Returns the number of
    rows deleted. Running / ready / blocked tasks keep their full event
    history.

    **A3 revocation latches are never pruned.** The latch kinds
    (:data:`A3_EVENT_REVOKED` / :data:`A3_EVENT_REVOCATION_CLEARED`) are the
    durable veto behind :func:`a3_gate_granted`. Without this exclusion a latch
    on a task that later reached ``done``/``archived`` would be deleted once it
    aged past the cutoff, and any surviving ``A3_GATE=GRANTED`` comment would
    silently re-open the gate — reintroducing the reversible-revocation defect
    the latch exists to close. Excluding two kinds deletes strictly fewer rows,
    so ordinary history pruning is unchanged.
    """
    cutoff = int(time.time()) - int(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived')) "
            "AND kind NOT IN (?, ?)",
            (cutoff, A3_EVENT_REVOKED, A3_EVENT_REVOCATION_CLEARED),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(
    *, older_than_seconds: int = 30 * 24 * 3600,
    board: Optional[str] = None,
) -> int:
    """Delete worker log files older than ``older_than_seconds``. Returns
    the number of files removed. Kept separate from ``gc_events`` because
    log files live on disk, not in SQLite. Scoped to ``board`` (defaults
    to the active board) — per-board isolation means deleting logs from
    board A cannot touch board B's logs."""
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Worker log accessor
# ---------------------------------------------------------------------------

def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Return the path to a worker's log file. The file may not exist
    (task never spawned, or log already GC'd).

    When ``board`` is None, resolves via the active board (env var →
    current-board file → default). The dispatcher always passes the
    board explicitly to avoid any resolution ambiguity when multiple
    boards exist."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None,
    board: Optional[str] = None,
) -> Optional[str]:
    """Read the worker log for ``task_id``. Returns None if the file
    doesn't exist. If ``tail_bytes`` is set, only the last N bytes are
    returned (useful for the dashboard drawer which shouldn't page megabytes)."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip a partial line if we tailed mid-line. But if the
                # window has no newline at all (one giant log line),
                # readline() would eat everything — in that case don't
                # skip and return the raw tail.
                probe = f.tell()
                partial = f.readline()
                if not partial.endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Assignee enumeration (known profiles + per-profile board stats)
# ---------------------------------------------------------------------------

def list_profiles_on_disk() -> list[str]:
    """Return the set of assignee/profile names discovered on disk.

    Includes:
    - named profiles under ``<default-root>/profiles/<name>/config.yaml``
    - the implicit ``default`` profile when the default Hermes root exists

    Reads profile paths directly so this module has no import dependency on
    ``hermes_cli.profiles`` (which pulls in a large chunk of the CLI startup
    path).
    """
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")

    if profiles_dir.is_dir():
        try:
            for entry in sorted(profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if (entry / "config.yaml").is_file():
                    names.add(entry.name)
        except OSError:
            pass

    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """Return every assignee name known to the board or on disk.

    Each entry is ``{"name": str, "on_disk": bool, "counts": {status: n}}``.
    A name is included when it's a configured profile on disk OR when
    any non-archived task has it as the assignee. Used by:

    - ``hermes kanban assignees`` for the terminal.
    - The dashboard assignee dropdown (so a fresh profile appears in
      the picker even before it's been given any task).
    - Router-profile heuristics ("who's overloaded?") without scanning
      the whole board.
    """
    on_disk = set(list_profiles_on_disk())

    # Count tasks per (assignee, status), excluding archived.
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    names = sorted(on_disk | set(counts.keys()))
    return [
        {
            "name": name,
            "on_disk": name in on_disk,
            "counts": counts.get(name, {}),
        }
        for name in names
    ]


# ---------------------------------------------------------------------------
# Runs (attempt history on a task)
# ---------------------------------------------------------------------------

def list_runs(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    include_active: bool = True,
    state_type: Optional[str] = None,
    state_name: Optional[str] = None,
) -> list[Run]:
    """Return all runs for ``task_id`` in start order.

    ``include_active=True`` (default) includes the currently-running
    attempt if any. Set False to return only closed runs (useful for
    "how many prior attempts have there been?" checks).

    When ``state_type`` and ``state_name`` are set, restrict to rows
    where that column equals ``state_name`` (``state_type`` is
    ``status`` or ``outcome``). Both must be passed together.
    """
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None:
        if state_type not in ("status", "outcome"):
            raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute(
        "SELECT * FROM task_runs WHERE id = ?", (int(run_id),),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return the latest non-null ``task_runs.summary`` for ``task_id``.

    The worker writes its handoff to ``task_runs.summary``
    via ``complete_task(summary=...)``; ``tasks.result`` is left empty
    unless the caller passes ``result=`` explicitly. Dashboards and CLI
    "show" views need this value to surface what a worker actually did
    — without it, ``tasks.result`` is NULL and the task looks like a
    no-op even when the run completed.

    Picks the most recent run by ``ended_at`` (falling back to ``id``
    for ties or unfinished rows). Returns None if no run has a summary.
    """
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(
    conn: sqlite3.Connection, task_ids: Iterable[str]
) -> dict[str, str]:
    """Batch-fetch latest non-null summaries for a list of task ids.

    Used by the dashboard board endpoint to attach ``latest_summary`` to
    every card in a single SQL query, avoiding the N+1 pattern of
    calling :func:`latest_summary` per task. Returns a dict mapping
    ``task_id`` → summary string, omitting tasks with no summary.

    Approach: a window function picks the newest non-null-summary row
    per ``task_id``; works against SQLite ≥ 3.25 (default on every
    supported platform).
    """
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}
