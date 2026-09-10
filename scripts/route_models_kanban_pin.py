#!/usr/bin/env python3
"""route_models_kanban_pin.py — capacity-aware auto-pin for ready kanban cards.

Kanban t_4051b1cd (2026-09-10): wires the existing usage-aware router
(route_models.py) into kanban dispatch. Nothing consumed its routing
decision before this; every card ran on whatever provider/model its
profile's config.yaml defaulted to, blind to live remaining capacity.

Contract (from the owning task, do not loosen without a fresh review):
  - For each dispatch:true / state:active board in boards-manifest.json,
    read READY cards with NO explicit model_override (an explicit pin is
    a human/PM decision and is never touched).
  - Tier is always "mid" (the DEFAULT IC tier per model-routing-standard —
    this shim does not attempt task-content classification; that stays a
    PM/card-creation-time decision per the routing standard).
  - A seat is EXCLUDED from consideration if its tightest usage window is
    >=90% used (10% headroom floor). This is a stricter floor than
    route_models.HEADROOM_FLOOR (5%) by design — the shim would rather
    leave a card unpinned (falls back to profile default) than pin it to
    a seat about to run out mid-task.
  - Nous is never a pin target (mirrors route_models.choose()).
  - If no seat is routable for the tier, the card is left alone — this
    script NEVER pauses/resumes the fleet and NEVER blocks/reassigns a
    card. Dispatcher failure_limit is the existing backstop.
  - Reads (not writes) go straight to each board's sqlite (read-only URI,
    WAL-safe) to stay cheap on a 5-minute cadence across N boards; the
    actual pin write goes through `hermes kanban set-model`, the one
    supported mutation path, so it gets the CLI's own validation/locking.
  - Stale-cache-on-429 handling is already implemented inside
    route_models.provider_capacity(); this script does not duplicate it.

Silent stdout when nothing changed (no-agent cron watchdog contract).
Exit 0 on a clean pass (including "did nothing"); nonzero only on a
hard failure to read the manifest/registry itself.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))
SCRIPTS_DIR = HERMES_ROOT / "scripts"
BOARDS_DIR = HERMES_ROOT / "kanban" / "boards"
CACHE_DIR = HERMES_ROOT / "cache"
LOCK_PATH = CACHE_DIR / "route_models_kanban_pin.lock"

sys.path.insert(0, str(SCRIPTS_DIR))
import route_models  # noqa: E402  (path insert must precede this import)
import fleet_boards  # noqa: E402

DEFAULT_TIER = "mid"
# Stricter than route_models.HEADROOM_FLOOR (5%) on purpose: this shim would
# rather skip a pin than commit a card to a seat that is about to die mid-run.
PIN_HEADROOM_FLOOR = 10.0
# route_models.provider_capacity() falls back to a stale on-disk cache when a
# provider's usage endpoint 429s, and stamps the reading with stale_age_hours
# — but nothing consulted that field before this shim (rejection item 3): an
# epoch-0 cache entry (age ~497,000h) was still routed on as if fresh. This
# shim is the first WRITE consumer of that reading, so refuse to route on
# anything older than this TTL rather than trusting an arbitrarily stale read.
MAX_STALE_CACHE_AGE_HOURS = 6.0
# Statuses build_state() can hand back that must never be pin targets.
EXCLUDED_STATUSES = {"failing", "depleted", "throttled"}

# Env vars that MUST NOT leak into the `hermes kanban set-model` subprocess.
# HERMES_KANBAN_DB / HERMES_KANBAN_BOARD outrank the `--board` CLI flag inside
# `_board_path` (kanban_db.py), so any ambient board pin from this cron's own
# process env would silently redirect (or fail) a set-model call meant for a
# DIFFERENT board — the exact cross-board write hazard proven live against
# t_b9cd957b on sycode-trading while this process carried jarvis-os's DB path.
_ENV_VARS_TO_STRIP = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
)


def choose_for_pin(state: dict, tier: str) -> dict | None:
    """Same shape as route_models.choose() but with the shim's own (10%,
    not 5%) headroom floor, and re-derives routability instead of trusting
    route_models' own status field — a seat sitting at exactly 90% used is
    still "available" under route_models' 5% floor but must not be picked
    here.

    Boundary is inclusive on the excluded side: spec says exclude at >=90%
    used (<=10% headroom). A seat with EXACTLY 10.0% headroom (90.0% used)
    must be excluded, not admitted — verified rejection item 4."""
    cands = []
    for s in state["seats"]:
        if tier not in s["tiers"]:
            continue
        if s["provider"] == "nous":
            continue  # never a pin target, matches route_models.choose()
        if s["status"] in EXCLUDED_STATUSES:
            continue
        stale_age = s.get("stale_age_hours")
        if stale_age is not None and stale_age > MAX_STALE_CACHE_AGE_HOURS:
            continue  # capacity reading too old to route a write on
        head = s.get("headroom")
        if head is not None and head <= PIN_HEADROOM_FLOOR:
            continue
        if s["reserved_for"] and s["reserved_for"] != tier:
            continue
        cands.append(s)
    if not cands:
        return None
    cands.sort(
        key=lambda s: (
            route_models.tier_rank(s, tier),
            -(s["headroom"] if s["headroom"] is not None else 50),
        )
    )
    return cands[0]


def ready_unpinned_cards(board_slug: str) -> list[str]:
    """Read-only: ready cards on this board with no explicit model_override."""
    db_path = BOARDS_DIR / board_slug / "kanban.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='ready' "
            "AND (model_override IS NULL OR model_override='')"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def apply_pin(board_slug: str, task_id: str, model: str, provider: str) -> tuple[bool, str]:
    cmd = [
        "hermes",
        "kanban",
        "--board",
        board_slug,
        "set-model",
        task_id,
        model,
        "--provider",
        provider,
    ]
    env = dict(os.environ)
    for var in _ENV_VARS_TO_STRIP:
        env.pop(var, None)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, env=env,
            cwd=str(Path.home()),
        )
    except Exception as e:  # pragma: no cover - defensive
        return False, f"{type(e).__name__}: {e}"[:200]
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:200]
    return True, (r.stdout or "").strip()[:120]


def dispatch_boards() -> list[str]:
    try:
        return list(fleet_boards.boards_for("dispatch"))
    except Exception as e:  # pragma: no cover - defensive
        print(f"ERROR: cannot read boards manifest: {e}", file=sys.stderr)
        raise


@contextlib.contextmanager
def _pin_lock():
    """Cooperative lockfile so overlapping ticks (37 candidates x up to 60s
    sequential `set-model` calls can run well past the 5-minute cadence) never
    write the same boards concurrently. Non-blocking: a tick that finds the
    lock already held backs off immediately rather than queuing behind a
    slow prior run.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                yield False
                return
            raise
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except ImportError:  # pragma: no cover - fcntl is POSIX-only
        os.close(fd)
        yield True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--boards", help="comma-separated override; default = manifest dispatch:true boards")
    ap.add_argument("--tier", default=DEFAULT_TIER)
    a = ap.parse_args()

    with _pin_lock() as acquired:
        if not acquired:
            print(
                "SKIPPED: a prior route_models_kanban_pin run is still holding "
                f"the lock ({LOCK_PATH}) — overlapping tick, no mutations this pass "
                f"({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})"
            )
            return 0
        return _run(a)


def _run(a: argparse.Namespace) -> int:
    boards = a.boards.split(",") if a.boards else dispatch_boards()

    state = route_models.build_state(do_probe=False)
    pick = choose_for_pin(state, a.tier)

    lines = []
    total_candidates = 0

    if pick is None:
        # ALL seats exhausted for this tier (or no routable seat at all):
        # log and do nothing. Never touch fleet pause/resume state.
        print(
            f"ALL SEATS EXHAUSTED for tier '{a.tier}' — no board mutations this tick "
            f"({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})"
        )
        return 0

    for board in boards:
        try:
            candidates = ready_unpinned_cards(board)
        except Exception as e:
            lines.append(f"ERROR reading board {board}: {type(e).__name__}: {e}")
            continue
        total_candidates += len(candidates)
        for task_id in candidates:
            if a.dry_run:
                lines.append(
                    f"DRY-RUN: {board}/{task_id} -> {pick['model']} --provider {pick['provider']}"
                )
                continue
            ok, msg = apply_pin(board, task_id, pick["model"], pick["provider"])
            if ok:
                lines.append(f"PINNED {board}/{task_id} -> {pick['model']} --provider {pick['provider']}")
            else:
                lines.append(f"FAILED {board}/{task_id}: {msg}")

    if not lines:
        return 0  # silent — no unpinned ready cards found, contract-compliant

    header = (
        f"route_models_kanban_pin: tier={a.tier} pick={pick['model']}"
        f"@{pick['provider']} candidates={total_candidates}"
    )
    print(header)
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
