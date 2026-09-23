#!/usr/bin/env bash
# kanban-stale-workspace-gc.sh — no-agent Hermes cron helper (Pulse 2026-09-12).
# Reap headless Chromium bound to kanban workspaces when the task is missing
# or terminal (done/archived), remove workspaces with no task row / aged terminal,
# and reap aged playwright-mcp npx trees with no bound kanban chromium.
#
# Env:
#   DRY_RUN=1
#   MIN_AGE_MIN=30              chromium root min age
#   GC_RM_WORKSPACE=1
#   GC_RM_CHROMIUM_PROFILE=1
#   TERMINAL_WS_MIN=360         done/archived workspace age before rm
#   PLAYWRIGHT_MCP_MIN_AGE_MIN=360    default 6h — orphan mcp min age (Pulse 2026-09-13; was 4d/missed node bin)
#   HERMES_HOME
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DRY_RUN="${DRY_RUN:-0}"
MIN_AGE_MIN="${MIN_AGE_MIN:-30}"
GC_RM_WORKSPACE="${GC_RM_WORKSPACE:-1}"
GC_RM_CHROMIUM_PROFILE="${GC_RM_CHROMIUM_PROFILE:-1}"
TERMINAL_WS_MIN="${TERMINAL_WS_MIN:-360}"
PLAYWRIGHT_MCP_MIN_AGE_MIN="${PLAYWRIGHT_MCP_MIN_AGE_MIN:-360}"
LOG="${KANBAN_STALE_WS_GC_LOG:-$HERMES_HOME/cron/state/kanban-stale-workspace-gc.log}"
mkdir -p "$(dirname "$LOG")"

python3 - "$HERMES_HOME" "$DRY_RUN" "$MIN_AGE_MIN" "$GC_RM_WORKSPACE" "$GC_RM_CHROMIUM_PROFILE" "$LOG" "$TERMINAL_WS_MIN" "$PLAYWRIGHT_MCP_MIN_AGE_MIN" <<'PY'
from __future__ import annotations
import os, re, signal, sqlite3, sys, time, shutil
from pathlib import Path

hermes_home = Path(sys.argv[1])
dry_run = sys.argv[2] == "1"
min_age_min = int(sys.argv[3])
rm_workspace = sys.argv[4] == "1"
rm_chrome_profile = sys.argv[5] == "1"
log_path = Path(sys.argv[6])
terminal_ws_min = int(sys.argv[7])
pw_min_age = int(sys.argv[8])

WS_RE = re.compile(
    r"--user-data-dir=((?:/[^ ]*?)/kanban/boards/([^/]+)/workspaces/(t_[0-9a-f]+)/(?:chromium-profile|chrome-profile)[^ ]*)"
)


def cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def age_minutes(pid: int) -> float:
    try:
        return (time.time() - Path(f"/proc/{pid}").stat().st_ctime) / 60.0
    except OSError:
        return 0.0


def children_of(pid: int) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        cpid = int(entry.name)
        try:
            for line in Path(f"/proc/{cpid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    if int(line.split()[1]) == pid:
                        out.append(cpid)
                    break
        except (OSError, ValueError):
            pass
    return out


def descendants(pid: int) -> list[int]:
    found, stack = [], [pid]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        kids = children_of(cur)
        found.extend(kids)
        stack.extend(kids)
    return found


def task_status(board: str, task_id: str) -> str | None:
    db = hermes_home / "kanban" / "boards" / board / "kanban.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(msg)
    with log_path.open("a") as f:
        f.write(line + "\n")


def kill_tree(root_pid: int) -> None:
    tree = [root_pid] + descendants(root_pid)
    for pid in tree:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(2)
    for pid in tree:
        if Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


actions = 0

# --- Chromium roots bound to kanban workspaces ---
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    cmd = cmdline(pid)
    if "chromium" not in cmd and "/chrome " not in cmd and not cmd.endswith("chrome"):
        # still match chrome binary paths
        if "chromium-browser/chrome" not in cmd and "/chrome --" not in cmd:
            continue
    if "--type=" in cmd:
        continue
    m = WS_RE.search(cmd)
    if not m:
        continue
    udd, board, task_id = m.group(1), m.group(2), m.group(3)
    if age_minutes(pid) < min_age_min:
        continue
    status = task_status(board, task_id)
    if status is None or status in {"done", "archived"}:
        reason = "missing" if status is None else status
        actions += 1
        log(f"REAP_CHROMIUM pid={pid} board={board} task={task_id} status={reason} udd={udd} dry_run={dry_run}")
        if dry_run:
            continue
        kill_tree(pid)
        if rm_chrome_profile and status in {"done", "archived"}:
            prof = Path(udd)
            if prof.is_dir() and "chromium-profile" in prof.name:
                shutil.rmtree(prof, ignore_errors=True)
                log(f"RM_CHROMIUM_PROFILE {prof}")

# --- Workspace dirs: missing row, or aged done/archived ---
boards_root = hermes_home / "kanban" / "boards"
if boards_root.is_dir() and rm_workspace:
    for board_dir in boards_root.iterdir():
        ws_root = board_dir / "workspaces"
        if not ws_root.is_dir():
            continue
        board = board_dir.name
        for ws in ws_root.iterdir():
            if not ws.is_dir() or not ws.name.startswith("t_"):
                continue
            status = task_status(board, ws.name)
            ages = []
            try:
                for p in ws.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.name.endswith(("-shm", "-wal", "-journal")):
                        continue
                    try:
                        ages.append((time.time() - p.stat().st_mtime) / 60.0)
                    except OSError:
                        pass
            except OSError:
                ages = []
            age_min = max(ages) if ages else (time.time() - ws.stat().st_mtime) / 60.0
            if status is None:
                reason = "missing"
            elif status in {"done", "archived"} and age_min >= terminal_ws_min:
                reason = status
            else:
                continue
            actions += 1
            log(f"RM_WORKSPACE board={board} task={ws.name} status={reason} age_min={age_min:.0f} path={ws} dry_run={dry_run}")
            if not dry_run:
                shutil.rmtree(ws, ignore_errors=True)

# --- Orphan playwright-mcp trees (no kanban-bound chromium in process table) ---
# Reap aged playwright-mcp roots when no live kanban chromium exists.
# Matches: npm exec roots, sh -c wrappers, and node .../playwright-mcp binaries.
# Pulse 2026-09-13: prior matcher required "npm exec" and missed node/_npx bin form.
kanban_chrome_alive = False
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    cmd = cmdline(int(entry.name))
    if WS_RE.search(cmd):
        kanban_chrome_alive = True
        break

def is_playwright_mcp_cmd(cmd: str) -> bool:
    c = cmd.lower()
    if "playwright-mcp" in c or "@playwright/mcp" in c:
        return True
    return False

def is_playwright_mcp_root(pid: int, cmd: str) -> bool:
    """Prefer npm-exec / sh -c roots; also accept orphan node bin when parent is not playwright."""
    c = cmd.strip()
    cl = c.lower()
    if "npm exec" in cl and ("playwright-mcp" in cl or "@playwright/mcp" in cl):
        return True
    if cl.startswith("npm ") and ("playwright-mcp" in cl or "@playwright/mcp" in cl):
        return True
    if ("sh -c" in cl or c.startswith("sh ")) and ("playwright-mcp" in cl or "@playwright/mcp" in cl):
        return True
    # node .../bin/playwright-mcp — only if parent cmdline is not already a playwright root
    if "node " in cl and ("/playwright-mcp" in cl or "playwright-mcp" in cl or "@playwright/mcp" in cl):
        try:
            ppid = None
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                    break
            if ppid is None or ppid <= 1:
                return True
            pcmd = cmdline(ppid)
            if is_playwright_mcp_cmd(pcmd):
                return False  # child of wrapper/root we will reap
            return True
        except OSError:
            return True
    return False

if not kanban_chrome_alive:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmd = cmdline(pid)
        if not is_playwright_mcp_cmd(cmd):
            continue
        if not is_playwright_mcp_root(pid, cmd):
            continue
        age = age_minutes(pid)
        if age < pw_min_age:
            continue
        actions += 1
        log(f"REAP_PLAYWRIGHT_MCP pid={pid} age_min={age:.0f} cmd={cmd[:120]} dry_run={dry_run}")
        if not dry_run:
            kill_tree(pid)

sys.exit(0)
PY
