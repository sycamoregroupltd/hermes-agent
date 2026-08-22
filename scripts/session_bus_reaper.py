#!/usr/bin/env python3
# CANONICAL SOURCE — /home/frank/.hermes/scripts/session_bus_reaper.py
# Profile-local cron exec shims (e.g. profiles/jarvis/scripts/session_bus_reaper.py)
# MUST be kept byte-identical to this file (CANONICAL-COPY RULE, t_41acb465). Edit here, then copy.
"""Session-Bus liveness reaper.

Scans the SESSION-BUS.md live-sessions table and flips `active` rows to
`STALE` when their `last_heartbeat` is older than a threshold (default 15 min).
It is the canonical liveness mechanism for the Session Bus (see
Orchestration/sessions/SESSION-BUS.md v1.2).

Design contract (Session Bus blueprint §5 action #1, jarvis-os card t_8045967b;
hardened by jarvis-os card t_9b909712 for frontmatter stamping):
  - Concurrent-safe: takes the SAME `.SESSION-BUS.lock` that `session-heartbeat.py`
    uses (fcntl.flock LOCK_EX). Heartbeats and reaps never interleave.
  - Liveness-only: flips `active` -> `STALE`. Never archives inbox/heartbeat
    files, never mutates `closed`/`complete`/`STALE` rows, never edits status
    text of rows still inside the threshold. Writes are atomic and idempotent: a
    second run on already-STALE/missing rows is a no-op.
  - Frontmatter coupling (t_9b909712): for every table row this run flips to
    STALE, the corresponding session file `<bus_dir>/<session_id>.md` has its
    frontmatter `status: active` rewritten to `status: STALE` so the file and
    the table can never disagree. Append-only log and every other line in the
    file are untouched — ONLY the frontmatter `status:` line changes. Files with
    no `status: active` frontmatter line, or with no frontmatter at all, are
    left byte-identical (no-op).
  - CEO / master-orchestrator exclusion (Frank special-case): session id `ceo`
    and any session file whose frontmatter declares `role: master-orchestrator`
    are NEVER stamped. Their table rows are still flipped (that is normal reaper
    liveness), but the file frontmatter is left alone and the seats are listed
    separately in the output so a human can see they were excluded.
  - Non-silent on change: prints a one-line list of the newly-STALE seats so the
    no-agent cron is observable exactly when it flips something, and stays quiet
    when nothing changed.
  - Conflict-only alerting: posts a `BLOCKED`/stale alert to the orchestrator-sync
    card t_058ad294 ONLY on a genuine conflict (two or more distinct active sessions
    with a heartbeat gap > CONFLICT_GAP_SECONDS that are NOT yet STALE). Routine
    heartbeat-only scans are silent on the kanban bus; the reaper's stdout is the
    only liveness signal and is empty when nothing changed.
  - Zero external mutation beyond the one vault table and the coupled session
    frontmatter under the lock. No kanban card/state change except the narrow
    conflict alert above.
  - Reconcile mode (--reconcile, t_58795f9f): additive/opt-in divergence sweep.
    Stamps session-file frontmatter `status: active` -> `status: STALE` for ANY
    file whose table row is NOT active (STALE/closed/archived/absent), closing
    the pre-existing divergence the liveness path never re-checks (t_9b909712
    only stamps rows flipped in the same run). Same lock, same CEO/master-orchestrator
    exclusion, same frontmatter-only stamping, per-file backup + verify-after.
    The default no-flag liveness behavior is completely unchanged.

Hermeticity: all file operations take explicit paths so --self-test / --selftest
never touches the live SESSION-BUS.md. Rollback: cron disable + script restore
(this file is git-free but the jarvis profile shim re-execs it; removing the cron
restores manual behavior). A live stamping run writes NO backup of the session
files (append-only status revert is driven by the printed newly-STALE list); the
table itself is backed up with BACKUP_SUFFIX before its atomic write. No shared-state
deletion ever performed.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_BIN = "/home/frank/.local/bin/hermes"
HERMES_HOME = "/home/frank/.hermes"
SYNC_BOARD = "orchestrator-sync"
SYNC_TASK = "t_058ad294"  # ORCH-LIVE: peer orchestrator coordination bus (blocked, no dispatcher)

DEFAULT_BUS_DIR = Path(os.environ.get("SESSION_BUS_DIR", "/home/frank/obsidian-fleet-vault/Orchestration/sessions"))
STALE_SECONDS = int(os.environ.get("SESSION_REAPER_STALE_SECONDS", "900"))  # 15 min default
BACKUP_SUFFIX = ".bak-session-reaper"
RECONCILE_BACKUP_SUFFIX = ".bak-session-reconcile"

# Non-session aux files that live in the sessions dir and are NOT tracked as
# sessions. Never stamped even if they carry `status: active` frontmatter.
RESERVED_NON_SESSION = {
    "SESSION-BUS.md",
    "SESSION-BUS-PROTOCOL-POINTER.md",
    "BUZZ-ISOLATED-READMIT.md",
}

# Row shape (8 cells), per SESSION-BUS.md v1.2 live-sessions table:
# | session-id | provider | inbox | direct handle | current focus | last_heartbeat | last_read | status |
ROW_RE = re.compile(
    r"^\|\s*`(?P<sid>[^`]+)`\s*\|"
    r"(?P<rest>.*?)\|\s*"
    r"(?P<hb>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)\s*\|"
    r"(?P<tail>.*?)\|\s*"
    r"(?P<status>active|stale|STALE|closed|archived|complete)\s*\|$"
)

# Frontmatter of a session file: a leading YAML block delimited by `---` lines.
FRONTMATTER_OPEN = re.compile(r"^---\s*$")
STATUS_LINE_RE = re.compile(r"^(status\s*:\s*)active\s*$")
ROLE_LINE_RE = re.compile(r"^role\s*:\s*master-orchestrator\s*$")


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def load_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def flip_active_rows(lines: list[str], now: datetime) -> tuple[list[str], list[str]]:
    """Flip active rows whose last_heartbeat is older than STALE_SECONDS to STALE.

    Returns (new_lines, flipped_session_ids). Idempotent: already-STALE/closed
    rows are left untouched; a within-threshold active row is left untouched.
    """
    out: list[str] = []
    flipped: list[str] = []
    for line in lines:
        match = ROW_RE.match(line)
        if not match:
            out.append(line)
            continue
        sid = match.group("sid")
        hb = parse_ts(match.group("hb"))
        status = match.group("status")
        if hb and status == "active" and (now - hb).total_seconds() > STALE_SECONDS:
            line = re.sub(r"\|\s*active\s*\|$", "| STALE |", line)
            flipped.append(sid)
        out.append(line)
    return out, flipped


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body) where frontmatter_block is the leading
    YAML block INCLUDING both `---` delimiters, or ('', text) if none exists."""
    lines = text.split("\n")
    if not lines or not FRONTMATTER_OPEN.match(lines[0]):
        return "", text
    for i in range(1, len(lines)):
        if FRONTMATTER_OPEN.match(lines[i]):
            return "\n".join(lines[: i + 1]), "\n".join(lines[i + 1 :])
    return "", text


def has_master_orchestrator_role(text: str) -> bool:
    """True if the session file frontmatter declares role: master-orchestrator."""
    fm, _ = split_frontmatter(text)
    return any(ROLE_LINE_RE.match(ln) for ln in fm.split("\n"))


def stamp_frontmatter_status(text: str) -> tuple[str, bool]:
    """Rewrite ONLY the frontmatter `status: active` line to `status: STALE`.

    Returns (new_text, changed). Append-only body/log and every other line are
    preserved byte-for-byte. If there is no frontmatter or no `status: active`
    line, returns (text, False) so callers leave the file untouched.
    """
    fm, body = split_frontmatter(text)
    if not fm:
        return text, False
    fm_lines = fm.split("\n")
    changed = False
    for i, ln in enumerate(fm_lines):
        m = STATUS_LINE_RE.match(ln)
        if m:
            fm_lines[i] = m.group(1) + "STALE"
            changed = True
            break  # only the first status line; YAML has a single status key
    if not changed:
        return text, False
    return "\n".join(fm_lines) + "\n" + body, True


def atomic_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def post_sync_alert(body: str, dry_run: bool, _poster=None) -> bool:
    """Post a BLOCKED/conflict alert comment to the orchestrator-sync card.

    `_poster` is an injectable callable for tests; defaults to the real hermes CLI.
    Returns True if a comment was posted (or would be in dry-run). Never raises.
    """
    cmd = [HERMES_BIN, "kanban", "--board", SYNC_BOARD, "comment", SYNC_TASK, body]
    if dry_run:
        print(f"[dry-run] would post orchestrator-sync alert: {body}")
        return True
    if _poster is not None:
        return _poster(body, dry_run)
    env = dict(os.environ)
    env["HERMES_HOME"] = HERMES_HOME
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    except Exception as exc:  # noqa: BLE001 - alert posting is best-effort
        print(f"⚠️ SESSION-BUS REAPER: failed to post sync alert ({exc}); stdout still emitted")
        return False
    if p.returncode != 0:
        print(f"⚠️ SESSION-BUS REAPER: sync alert post failed rc={p.returncode}: {p.stderr.strip()[:200]}")
        return False
    return True


def stamp_session_file(bus_dir: Path, sid: str, dry_run: bool) -> tuple[str, Path | None]:
    """Stamp a flipped session's file frontmatter to STALE.

    Returns (action, path):
      - ('excluded', path)  — ceo / master-orchestrator; file NOT touched.
      - ('stamped', path)   — frontmatter status rewritten (or would be in dry-run).
      - ('noop', None)      — no frontmatter / no `status: active` line / file missing.
    """
    f = bus_dir / f"{sid}.md"
    if sid == "ceo":
        return "excluded", f
    if not f.exists():
        return "noop", None
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return "noop", None
    if has_master_orchestrator_role(text):
        return "excluded", f
    new_text, changed = stamp_frontmatter_status(text)
    if not changed:
        return "noop", None
    if not dry_run:
        atomic_write(f, new_text)
    return "stamped", f


def reap(bus_path: Path, lock_handle, now: datetime, dry_run: bool, _poster=None) -> int:
    if not bus_path.exists():
        msg = f"🔴 SESSION-BUS REAPER: missing {bus_path}"
        print(msg)
        return 1
    bus_dir = bus_path.parent
    lines = load_lines(bus_path)
    new_lines, flipped = flip_active_rows(lines, now)

    parts: list[str] = []
    if flipped:
        if dry_run:
            print(f"[dry-run] would flip {len(flipped)} active row(s) to STALE: {', '.join(flipped[:8])}")
        else:
            backup = bus_path.with_suffix(bus_path.suffix + BACKUP_SUFFIX)
            shutil.copy2(bus_path, backup)
            atomic_write(bus_path, "\n".join(new_lines) + "\n")
            parts.append(
                f"marked STALE rows: {', '.join(flipped[:8])}"
                + (f" (+{len(flipped) - 8} more)" if len(flipped) > 8 else "")
            )

    # t_9b909712: stamp the coupled session file frontmatter for every flipped row.
    stamped: list[str] = []
    excluded: list[tuple[str, Path | None]] = []
    noops = 0
    for sid in flipped:
        action, path = stamp_session_file(bus_dir, sid, dry_run)
        if action == "stamped":
            stamped.append(sid)
            if dry_run:
                print(f"[dry-run] would stamp frontmatter: {path}")
        elif action == "excluded":
            excluded.append((sid, path))
        else:
            noops += 1

    if stamped:
        listing = ", ".join(stamped) + (f" (+{len(stamped) - 8} more)" if len(stamped) > 8 else "")
        if dry_run:
            print(f"[dry-run] NEWLY-STALE seats (frontmatter stamped): {listing}")
        else:
            parts.append(f"NEWLY-STALE seats (frontmatter stamped): {listing}")
    if excluded:
        excl_list = ", ".join(f"{sid}({p.name if p else '?'})" for sid, p in excluded)
        parts.append(f"excluded from stamping (CEO/master-orchestrator): {excl_list}")

    # Genuine conflict detection: only fire an orchestrator-sync alert when two or
    # more sessions appear genuinely live AND share a coordination slot (per
    # detect_conflicts). A single live session, or any number of stale zombies, is
    # routine — no alert. Routine heartbeat-only scans emit nothing.
    conflicts = detect_conflicts(new_lines, now)
    if len(conflicts) >= 2:
        body = (
            f"BLOCKED session-bus liveness conflict: {len(conflicts)} sessions appear "
            f"live (heartbeat within {STALE_SECONDS}s) yet share a direct-handle/inbox "
            f"slot ({', '.join(conflicts[:6])}"
            f"{' +more' if len(conflicts) > 6 else ''}). "
            f"Coordination collision risk — verify which session truly owns the slot."
        )
        if post_sync_alert(body, dry_run, _poster=_poster):
            parts.append("posted orchestrator-sync conflict alert")

    if parts:
        print("⚠️ SESSION-BUS REAPER: " + "; ".join(parts))
    # Silent (no stdout) when nothing changed and no conflict -> no delivery noise.
    return 0


def detect_conflicts(lines: list[str], now: datetime) -> list[str]:
    """Return session ids involved in a genuine liveness conflict.

    A *genuine* conflict (per blueprint §5: "alert ONLY on genuine conflict, never
    on routine heartbeat") is two or more sessions that BOTH still appear truly
    live — i.e. their `last_heartbeat` is within the liveness window (<= STALE_SECONDS)
    — yet share the same `direct handle` or `inbox`. That means two live sessions
    are squatting one coordination slot: a real collision risk worth one alert.

    Stale-but-active zombie rows (heartbeat older than the window) are NOT a
    conflict — they are exactly what the reaper flips to STALE routinely and must
    stay silent on the bus. A single live session is also not a conflict.
    """
    live_slots: dict[str, list[str]] = {}
    for line in lines:
        match = ROW_RE.match(line)
        if not match:
            continue
        if match.group("status") != "active":
            continue
        hb = parse_ts(match.group("hb"))
        if not hb or (now - hb).total_seconds() > STALE_SECONDS:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        sid = match.group("sid")
        inbox = cells[2]
        handle = cells[3]
        for key in (handle, inbox):
            if key and key not in ("-", ""):
                live_slots.setdefault(key, []).append(sid)
    conflicts: list[str] = []
    for sids in live_slots.values():
        if len(sids) >= 2:
            conflicts.extend(sids)
    seen: set[str] = set()
    out: list[str] = []
    for sid in conflicts:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def active_session_ids(lines: list[str]) -> set[str]:
    """Return the set of sids whose CURRENT SESSION-BUS.md table row is `active`."""
    active: set[str] = set()
    for line in lines:
        m = ROW_RE.match(line)
        if m and m.group("status") == "active":
            active.add(m.group("sid"))
    return active


def classify_session_file(path: Path, active: set[str]) -> tuple[str, str]:
    """Classify one top-level session file for reconcile (--reconcile mode).

    Returns (kind, reason) where kind is one of:
      - 'stamp':  table row NOT active (STALE/closed/archived/absent) AND the
                  file carries `status: active` frontmatter -> candidate.
      - 'excluded': ceo.md or `role: master-orchestrator` (Frank special-case);
                  NEVER stamped, listed separately.
      - 'noop':   no frontmatter or no `status: active` line (byte-identical).
      - 'non_session': reserved aux file or inbox-* message store.
      - 'active_row': table row IS active -> kept, never stamped.
    """
    if path.name in RESERVED_NON_SESSION:
        return "non_session", "reserved non-session file"
    sid = path.stem
    if sid.startswith("inbox-"):
        return "non_session", "inbox file (message store, not a session)"
    if sid == "ceo":
        return "excluded", "CEO seat (Frank special-case)"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "noop", "unreadable"
    if has_master_orchestrator_role(text):
        return "excluded", "role: master-orchestrator (Frank special-case)"
    _, changed = stamp_frontmatter_status(text)
    if not changed:
        return "noop", "no `status: active` frontmatter line"
    if sid in active:
        return "active_row", "table row active -> kept"
    return "stamp", "table row NOT active / absent -> stamp to STALE"


def scan_session_files(bus_dir: Path, active: set[str]) -> dict[str, list[Path]]:
    """Return {kind: [Path]} for all top-level session files, sorted."""
    out: dict[str, list[Path]] = {}
    for p in sorted(bus_dir.glob("*.md")):
        if not p.is_file():
            continue
        kind, _ = classify_session_file(p, active)
        out.setdefault(kind, []).append(p)
    return out


def report_reconcile(scan: dict[str, list[Path]]) -> None:
    """Print the reconcile candidate/excluded/kept breakdown for review."""
    stamped = scan.get("stamp", [])
    excluded = scan.get("excluded", [])
    kept = scan.get("active_row", [])
    print(f"[reconcile] NEWLY-STALE candidates (table row NOT active): {len(stamped)}")
    for p in stamped:
        print(f"  STAMP  {p}")
    print(f"[reconcile] excluded from stamping (CEO/master-orchestrator): {len(excluded)}")
    for p in excluded:
        print(f"  EXCL   {p}")
    print(f"[reconcile] genuinely-active table rows (kept): {len(kept)}")
    for p in kept:
        print(f"  KEEP   {p}")
    print(f"[reconcile] noop / non-session: {len(scan.get('noop', []))} / {len(scan.get('non_session', []))}")


def reconcile_remaining(bus_dir: Path) -> tuple[int, list[Path]]:
    """Re-scan: return (count, paths) of files still carrying `status: active`
    frontmatter whose table row is NOT active (the divergence this mode closes)."""
    bus = bus_dir / "SESSION-BUS.md"
    if not bus.exists():
        return 0, []
    active = active_session_ids(load_lines(bus))
    remaining: list[Path] = []
    for p in sorted(bus_dir.glob("*.md")):
        if not p.is_file():
            continue
        kind, _ = classify_session_file(p, active)
        if kind == "stamp":
            remaining.append(p)
    return len(remaining), remaining


def reconcile(bus_dir: Path, lock_handle, dry_run: bool) -> int:
    """Periodic divergence reconcile (--reconcile mode).

    Stamps session-file frontmatter `status: active` -> `status: STALE` for ANY
    file whose SESSION-BUS.md table row is NOT `active` (STALE/closed/archived/
    absent). This closes pre-existing divergence the liveness reaper never
    re-checks — t_9b909712 only stamps rows flipped in the same run, and the
    one-time sweep t_d4808fd1 cleaned history but could not keep it clean.

    Additive and opt-in: the default no-flag liveness reap() is completely
    unchanged. Takes the SAME .SESSION-BUS.lock. NEVER stamps ceo.md or any
    `role: master-orchestrator` file (listed separately). ONLY the frontmatter
    `status:` line changes; append-only body preserved. Per-file backup before
    each stamp; verify-after confirms 0 remaining.
    """
    bus = bus_dir / "SESSION-BUS.md"
    if not bus.exists():
        print(f"🔴 missing {bus}")
        return 1
    lines = load_lines(bus)
    active = active_session_ids(lines)
    s = scan_session_files(bus_dir, active)
    stamped: list[Path] = s.get("stamp", [])
    excluded: list[Path] = s.get("excluded", [])

    if dry_run:
        print("[reconcile:dry-run] read-only; no mutation.")
        report_reconcile(s)
        return 0

    if not stamped:
        print("nothing to stamp — divergence already clean")
        return 0

    done: list[Path] = []
    for p in stamped:
        per_file = Path(str(p) + RECONCILE_BACKUP_SUFFIX)
        shutil.copy2(p, per_file)
        text = p.read_text(encoding="utf-8")
        new_text, changed = stamp_frontmatter_status(text)
        if not changed:
            print(f"[reconcile] NOOP (no active line): {p}")
            continue
        atomic_write(p, new_text)
        done.append(p)
    print(f"[reconcile] stamped {len(done)} file(s); per-file backups *{RECONCILE_BACKUP_SUFFIX}")
    if excluded:
        excl_list = ", ".join(str(p.name) for p in excluded)
        print(f"[reconcile] excluded from stamping (CEO/master-orchestrator): {excl_list}")

    # verify-after: re-scan, expect 0 remaining on non-active-row files.
    n, remaining = reconcile_remaining(bus_dir)
    print(f"[reconcile:verify] remaining `status: active` on non-active-row files: {n}")
    for p in remaining:
        print(f"  REMAIN  {p}")
    return 0 if n == 0 else 2


def self_test() -> int:
    """Fixture-driven test: flip, idempotency, lock-honor, conflict/single paths,
    and the t_9b909712 frontmatter-stamping / CEO / master-orchestrator coupling.

    Fully hermetic — operates only on a temp fixture directory; never the live file.
    """
    header = "| session-id | provider | inbox | direct handle | current focus | last_heartbeat | last_read | status |"
    sep = "|---|---|---|---|---|---|---|---|"
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Short-format (no-seconds) heartbeat: must be caught by ROW_RE and flipped
    # like a normal row (t_f6ae8b53). Strips the seconds component.
    old_short = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%MZ")
    base_rows = [
        f"| `old-active` | codex | `inbox-old-active.md` | - | - | {old} | - | active |",
        f"| `shortformat-active` | codex | `inbox-shortformat-active.md` | - | - | {old_short} | - | active |",
        f"| `fresh-active` | codex | `inbox-fresh-active.md` | - | - | {new} | - | active |",
        f"| `already-stale` | codex | `inbox-already-stale.md` | - | - | {old} | - | STALE |",
        f"| `closed-one` | codex | `inbox-closed-one.md` | - | - | {old} | - | closed |",
        f"| `ceo` | codex | `inbox-ceo.md` | - | - | {old} | - | active |",
        f"| `master-orch-seat` | codex | `inbox-master-orch-seat.md` | - | - | {old} | - | active |",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        bus_dir = Path(tmp)
        bus = bus_dir / "SESSION-BUS.md"
        lock = bus_dir / ".SESSION-BUS.lock"
        now = datetime.now(timezone.utc)

        # Session files mirroring the rows that must be stamped / excluded.
        def session_file(sid: str, extra_fm: str = "", status: str = "active") -> None:
            (bus_dir / f"{sid}.md").write_text(
                f"---\nsession_id: {sid}\nstatus: {status}\n{extra_fm}---\n# Session {sid}\n\n## Log (append-only)\n- {now:%Y-%m-%dT%H:%M:%SZ} — entry\n",
                encoding="utf-8",
            )

        session_file("old-active")
        session_file("shortformat-active")
        session_file("fresh-active")
        session_file("already-stale", status="STALE")
        session_file("closed-one", status="closed")
        session_file("ceo")  # sid == ceo -> must NOT be stamped
        session_file("master-orch-seat", extra_fm="role: master-orchestrator\n")

        bus.write_text("\n".join([header, sep] + base_rows) + "\n", encoding="utf-8")

        # 1) flip + stamp path under lock
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False)
        txt = bus.read_text(encoding="utf-8")
        if "| STALE |" not in txt or "old-active" not in txt:
            raise AssertionError("active row was not flipped to STALE")
        old_row = [l for l in txt.splitlines() if "`fresh-active`" in l][0]
        if "| active |" not in old_row:
            raise AssertionError("fresh active row was wrongly flipped")

        # frontmatter stamping: old-active stamped, fresh-active NOT, others preserved
        old_fm = (bus_dir / "old-active.md").read_text(encoding="utf-8")
        if "status: STALE" not in old_fm:
            raise AssertionError("old-active frontmatter was not stamped to STALE")
        # short-format (no-seconds) heartbeat row: ROW_RE must catch it, flip it,
        # and stamp its session file — the regression locked by t_f6ae8b53.
        short_row = [l for l in txt.splitlines() if "`shortformat-active`" in l]
        if not short_row or "| STALE |" not in short_row[0]:
            raise AssertionError("short-format heartbeat row was not flipped to STALE")
        short_fm = (bus_dir / "shortformat-active.md").read_text(encoding="utf-8")
        if "status: STALE" not in short_fm:
            raise AssertionError("shortformat-active frontmatter was not stamped to STALE")
        fresh_fm = (bus_dir / "fresh-active.md").read_text(encoding="utf-8")
        if "status: active" not in fresh_fm:
            raise AssertionError("fresh-active frontmatter was wrongly stamped")
        # append-only log untouched in the stamped file
        if "## Log (append-only)" not in old_fm or old_fm.count("## Log") != 1:
            raise AssertionError("append-only log was altered during stamping")
        # CEO seat excluded
        ceo_fm = (bus_dir / "ceo.md").read_text(encoding="utf-8")
        if "status: active" not in ceo_fm or "status: STALE" in ceo_fm:
            raise AssertionError("CEO seat frontmatter was not left untouched")
        # master-orchestrator excluded
        mo_fm = (bus_dir / "master-orch-seat.md").read_text(encoding="utf-8")
        if "status: active" not in mo_fm or "status: STALE" in mo_fm:
            raise AssertionError("master-orchestrator frontmatter was not left untouched")

        # 2) idempotency: second run flips nothing
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False)
        if bus.read_text(encoding="utf-8") != txt:
            raise AssertionError("second reap was not idempotent")

        # 3) lock-honor: the shared flock is taken; re-confirm the lock file is the
        #    coordination point. A second holder would block until released.
        with lock.open("a", encoding="utf-8") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            if not lock.exists():
                raise AssertionError("lock file missing")

        # 4) conflict-alert path: two genuinely-LIVE active rows sharing a handle
        #    -> exactly one alert. Run non-dry but inject a poster so no real
        #    kanban post happens in-test (rows are recent -> not flipped anyway).
        recent = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conflict_rows = [
            f"| `a-live` | codex | `inbox-a.md` | shared-handle | - | {recent} | - | active |",
            f"| `b-live` | codex | `inbox-b.md` | shared-handle | - | {recent} | - | active |",
        ]
        bus.write_text("\n".join([header, sep] + conflict_rows) + "\n", encoding="utf-8")
        posted: list[str] = []
        poster = lambda body, dry: (posted.append(body) or True)
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False, _poster=poster)
        if len(posted) != 1:
            raise AssertionError(f"expected exactly 1 conflict alert, got {len(posted)}")

        # 5) single live row (unique handle) -> NO alert (routine reap)
        single = [f"| `only-one` | codex | `inbox-only.md` | unique-handle | - | {recent} | - | active |"]
        bus.write_text("\n".join([header, sep] + single) + "\n", encoding="utf-8")
        posted.clear()
        with lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            reap(bus, lh, now, dry_run=False, _poster=poster)
        if posted:
            raise AssertionError("single live session must NOT raise an alert")

    # 6) reconcile mode (t_58795f9f): stamp files whose table row is NOT
    #    active; keep active-row files; exclude ceo + master-orchestrator.
    #    (Own TemporaryDirectory — this block runs after the main fixture's
    #    temp context has already closed.)
    with tempfile.TemporaryDirectory() as rtmp:
        r_dir = Path(rtmp)
        r_bus = r_dir / "SESSION-BUS.md"
        r_lock = r_dir / ".SESSION-BUS.lock"
        r_rows = [
            f"| `live-seat` | codex | - | - | - | {new} | - | active |",
            f"| `stale-seat` | codex | - | - | - | {old} | - | STALE |",
            f"| `closed-seat` | codex | - | - | - | {old} | - | closed |",
            f"| `ceo` | codex | - | - | - | {old} | - | active |",
            f"| `orch-seat` | codex | - | - | - | {old} | - | active |",
        ]
        r_bus.write_text("\n".join([header, sep] + r_rows) + "\n", encoding="utf-8")

        def r_session(sid: str, status: str = "active", role: str | None = None) -> None:
            role_line = f"role: {role}\n" if role else ""
            (r_dir / f"{sid}.md").write_text(
                f"---\nsession_id: {sid}\nstatus: {status}\n{role_line}---\n# {sid}\n\n## Log (append-only)\n- x\n",
                encoding="utf-8",
            )

        r_session("live-seat", "active")  # row active -> kept
        r_session("stale-seat", "active")  # table STALE -> stamp
        r_session("closed-seat", "active")  # table closed -> stamp
        r_session("no-row-seat", "active")  # absent from table -> stamp
        r_session("ceo", "active")  # excluded
        r_session("orch-seat", "active", role="master-orchestrator")  # excluded

        # dry-run: read-only, must NOT mutate.
        with r_lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            rc = reconcile(r_dir, lh, dry_run=True)
            fcntl.flock(lh, fcntl.LOCK_UN)
        if rc != 0:
            raise AssertionError("reconcile dry-run returned non-zero")
        if "status: STALE" in (r_dir / "stale-seat.md").read_text(encoding="utf-8"):
            raise AssertionError("reconcile dry-run mutated a file")

        # live reconcile under lock.
        with r_lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            rc = reconcile(r_dir, lh, dry_run=False)
            fcntl.flock(lh, fcntl.LOCK_UN)
        if rc != 0:
            raise AssertionError(f"reconcile returned non-zero ({rc})")
        for sid in ("stale-seat", "closed-seat", "no-row-seat"):
            if "status: STALE" not in (r_dir / f"{sid}.md").read_text(encoding="utf-8"):
                raise AssertionError(f"{sid} frontmatter not stamped to STALE")
        if "status: active" not in (r_dir / "live-seat.md").read_text(encoding="utf-8"):
            raise AssertionError("live-seat wrongly stamped")
        if "status: active" not in (r_dir / "ceo.md").read_text(encoding="utf-8"):
            raise AssertionError("CEO frontmatter wrongly stamped")
        if "status: active" not in (r_dir / "orch-seat.md").read_text(encoding="utf-8"):
            raise AssertionError("master-orchestrator frontmatter wrongly stamped")
        if "## Log (append-only)" not in (r_dir / "stale-seat.md").read_text(encoding="utf-8"):
            raise AssertionError("append-only body altered during reconcile")

        # idempotency: second reconcile stamps nothing and verifies clean.
        with r_lock.open("a", encoding="utf-8") as lh:
            fcntl.flock(lh, fcntl.LOCK_EX)
            rc = reconcile(r_dir, lh, dry_run=False)
            fcntl.flock(lh, fcntl.LOCK_UN)
        if rc != 0:
            raise AssertionError("second reconcile not clean (verify-after failed)")

    print("self-test ok")
    return 0


def main() -> int:
    global DEFAULT_BUS_DIR
    ap = argparse.ArgumentParser(description="Session-Bus liveness reaper + divergence reconcile")
    ap.add_argument("--bus-dir", default=str(DEFAULT_BUS_DIR), help="Session Bus directory")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; mutate nothing")
    ap.add_argument("--reconcile", action="store_true", help="periodic divergence reconcile: stamp session files whose table row is NOT active (additive to liveness reap)")
    ap.add_argument("--self-test", action="store_true", help="run fixture self-test and exit")
    ap.add_argument("--selftest", action="store_true", help="alias for --self-test")
    args = ap.parse_args()

    if args.self_test or args.selftest:
        return self_test()

    bus_dir = Path(args.bus_dir)
    bus = bus_dir / "SESSION-BUS.md"
    lock = bus_dir / ".SESSION-BUS.lock"
    now = datetime.now(timezone.utc)

    bus_dir.mkdir(parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        if args.reconcile:
            return reconcile(bus_dir, lock_handle, dry_run=args.dry_run)
        return reap(bus, lock_handle, now, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
