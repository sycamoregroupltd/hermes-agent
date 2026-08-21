#!/usr/bin/env python3
"""One-way Notion 'Sycode tasks' → Hermes kanban poller.

Creates triage cards (never ready) on an explicit --board with
--idempotency-key notion:<pageid>. Writes Hermes ID / Hermes board back
onto the Notion row when a Notion token is available.

No-agent watchdog: silent stdout when there is nothing to do.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes")).expanduser()
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
STATE_PATH = Path(
    os.environ.get(
        "NOTION_KANBAN_STATE",
        str(HERMES_HOME / "state" / "notion-kanban-sync.json"),
    )
)
LOCK_PATH = Path(
    os.environ.get(
        "NOTION_KANBAN_LOCK",
        str(HERMES_HOME / "state" / "notion-kanban-sync.lock"),
    )
)
SECRET_FILES = (
    HERMES_HOME / "secrets" / "notion.env",
    HERMES_HOME / ".env",
    HERMES_HOME / "profiles" / "jarvis" / ".env",
)

DATA_SOURCE_ID = "c851a0ea-7650-4d6b-a034-8cb520ddb6b5"
DEFAULT_BOARD = "sycode-trading"
ALLOWED_BOARDS = frozenset(
    {
        "sycode-trading",
        "jarvis-os",
        "sycode-ai",
        "upero",
        "yorkstone-supplies",
    }
)
SYNC_OWNERS = frozenset({"sycode", "both", ""})
SKIP_OWNERS = frozenset({"seam"})
NOTION_VERSION = "2025-09-03"
CREATED_BY = "notion-sync"
MAX_CREATES = 20
PRIORITY_MAP = {"p0": 0, "p1": 1, "p2": 2}

TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{6,}\b")


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def notion_token() -> str:
    for path in SECRET_FILES:
        _load_env_file(path)
    for key in ("NOTION_API_KEY", "NOTION_API_TOKEN", "NOTION_KEY"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return ""


def compact_page_id(value: str) -> str:
    raw = (value or "").strip()
    if "notion.so/" in raw or "notion.com/" in raw:
        raw = raw.rstrip("/").rsplit("/", 1)[-1]
        raw = raw.split("?")[0].split("-")[-1] if "-" in raw and len(raw) > 32 else raw
    hex_only = re.sub(r"[^0-9a-fA-F]", "", raw)
    if len(hex_only) >= 32:
        return hex_only[-32:].lower()
    return hex_only.lower()


def idempotency_key(page_id: str) -> str:
    return f"notion:{compact_page_id(page_id)}"


def priority_int(label: str | None) -> int:
    return PRIORITY_MAP.get((label or "").strip().lower(), 2)


def _rich_text(prop: object) -> str:
    if isinstance(prop, str):
        return prop.strip()
    if not isinstance(prop, dict):
        return ""
    chunks = prop.get("title") or prop.get("rich_text") or []
    parts: list[str] = []
    if isinstance(chunks, list):
        for chunk in chunks:
            if isinstance(chunk, dict):
                parts.append(chunk.get("plain_text") or (chunk.get("text") or {}).get("content") or "")
    return "".join(parts).strip()


def _select(prop: object) -> str:
    if isinstance(prop, str):
        return prop.strip()
    if not isinstance(prop, dict):
        return ""
    sel = prop.get("select")
    if isinstance(sel, dict):
        return str(sel.get("name") or "").strip()
    return ""


def parse_page(page: dict) -> dict | None:
    pid = compact_page_id(str(page.get("id") or ""))
    if len(pid) != 32:
        return None
    props = page.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    title = _rich_text(props.get("Name") or props.get("title") or page.get("Name") or "")
    if not title:
        title = str(page.get("Name") or "").strip()
    if not title:
        return None
    url = str(page.get("url") or f"https://www.notion.so/{pid}")
    return {
        "page_id": pid,
        "url": url,
        "title": title,
        "note": _rich_text(props.get("Note") or page.get("Note") or ""),
        "owner": (_select(props.get("Owner") or page.get("Owner") or "")).lower(),
        "priority": (_select(props.get("Priority") or page.get("Priority") or "")).lower(),
        "status": (_select(props.get("Status") or page.get("Status") or "")).lower(),
        "hermes_id": _rich_text(props.get("Hermes ID") or page.get("Hermes ID") or ""),
        "hermes_board": _select(props.get("Hermes board") or page.get("Hermes board") or ""),
    }


def should_sync(row: dict) -> bool:
    if row.get("hermes_id"):
        return False
    if (row.get("status") or "open") != "open":
        return False
    owner = row.get("owner") or ""
    if owner in SKIP_OWNERS:
        return False
    return owner in SYNC_OWNERS


def resolve_board(row: dict) -> str:
    override = (row.get("hermes_board") or "").strip()
    board = override or DEFAULT_BOARD
    if board not in ALLOWED_BOARDS:
        raise ValueError(f"board '{board}' is not in {sorted(ALLOWED_BOARDS)}")
    return board


def card_body(row: dict) -> str:
    lines = []
    note = (row.get("note") or "").strip()
    if note:
        lines.append(note)
        lines.append("")
    lines.append(f"Source: {row['url']}")
    lines.append(f"Owner: {row.get('owner') or 'unset'}")
    lines.append(f"Priority: {row.get('priority') or 'unset'}")
    lines.append(f"Notion: {row['page_id']}")
    return "\n".join(lines)


def notion_request(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    url = f"https://api.notion.com{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Notion {method} {path} -> {exc.code}: {detail}") from exc
    return json.loads(body) if body else {}


def fetch_open_rows(token: str) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        payload: dict = {
            "page_size": 100,
            "filter": {"property": "Status", "select": {"equals": "open"}},
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = notion_request(token, "POST", f"/v1/data_sources/{DATA_SOURCE_ID}/query", payload)
        for page in data.get("results") or []:
            if isinstance(page, dict):
                parsed = parse_page(page)
                if parsed:
                    rows.append(parsed)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return rows


def write_back(token: str, page_id: str, task_id: str, board: str) -> None:
    notion_request(
        token,
        "PATCH",
        f"/v1/pages/{page_id}",
        {
            "properties": {
                "Hermes ID": {
                    "rich_text": [{"type": "text", "text": {"content": task_id[:200]}}]
                },
                "Hermes board": {"select": {"name": board}},
            }
        },
    )


def load_state() -> dict:
    if not STATE_PATH.is_file():
        return {"pages": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pages": {}}
    if not isinstance(data, dict):
        return {"pages": {}}
    data.setdefault("pages", {})
    return data


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def parse_create_id(stdout: str) -> str:
    text = (stdout or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("id", "task_id", "taskId"):
                val = str(data.get(key) or "")
                if TASK_ID_RE.fullmatch(val):
                    return val
            match = TASK_ID_RE.search(text)
            return match.group(0) if match else ""
    except json.JSONDecodeError:
        pass
    match = TASK_ID_RE.search(text)
    return match.group(0) if match else ""


def kanban_create(row: dict, board: str, dry_run: bool) -> tuple[str, bool]:
    """Return (task_id, created_new). created_new is False on idempotent hit."""
    key = idempotency_key(row["page_id"])
    cmd = [
        HERMES_BIN,
        "kanban",
        "--board",
        board,
        "create",
        row["title"],
        "--body",
        card_body(row),
        "--priority",
        str(priority_int(row.get("priority"))),
        "--triage",
        "--created-by",
        CREATED_BY,
        "--idempotency-key",
        key,
        "--json",
    ]
    if dry_run:
        return f"dry-run:{key}", True
    before = existing_task_id(board, key)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(
            f"hermes kanban create failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
        )
    task_id = parse_create_id(proc.stdout) or existing_task_id(board, key)
    if not task_id:
        raise RuntimeError(f"create succeeded but no task id in output: {proc.stdout[:300]}")
    return task_id, (before != task_id and not before)


def existing_task_id(board: str, key: str) -> str:
    db = HERMES_HOME / "kanban" / "boards" / board / "kanban.db"
    if not db.is_file():
        return ""
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' LIMIT 1",
                (key,),
            ).fetchone()
            return str(row[0]) if row else ""
        finally:
            con.close()
    except Exception:
        return ""


def acquire_lock() -> int | None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def run_sync(dry_run: bool, token: str) -> int:
    rows = [row for row in fetch_open_rows(token) if should_sync(row)]
    if not rows:
        return 0
    state = load_state()
    created: list[str] = []
    reused: list[str] = []
    errors: list[str] = []
    for row in rows[:MAX_CREATES]:
        page_id = row["page_id"]
        try:
            board = resolve_board(row)
            cached = (state.get("pages") or {}).get(page_id) or {}
            if cached.get("task_id") and existing_task_id(board, idempotency_key(page_id)):
                task_id = cached["task_id"]
                if token and not dry_run and not row.get("hermes_id"):
                    write_back(token, page_id, task_id, board)
                reused.append(f"{task_id} {row['title']}")
                continue
            task_id, is_new = kanban_create(row, board, dry_run)
            if not dry_run:
                state.setdefault("pages", {})[page_id] = {
                    "task_id": task_id,
                    "board": board,
                    "title": row["title"],
                }
                if token:
                    write_back(token, page_id, task_id, board)
                    time.sleep(0.35)
            (created if is_new else reused).append(f"{task_id} {row['title']}")
        except Exception as exc:
            errors.append(f"{page_id} {row.get('title')}: {exc}")
    if not dry_run:
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)
    lines = []
    if created:
        lines.append("created " + "; ".join(created))
    if reused:
        lines.append("linked " + "; ".join(reused))
    if errors:
        lines.append("ERROR " + " | ".join(errors))
    if lines:
        print("\n".join(lines))
    return 1 if errors else 0


def _missing_token_alert() -> int:
    """Remind at most once per day so a no-agent cron does not spam."""
    state = load_state()
    last = str(state.get("last_token_alert_at") or "")
    now = time.time()
    try:
        last_ts = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        last_ts = 0.0
    if now - last_ts < 86400:
        return 0
    state["last_token_alert_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_state(state)
    print(
        "ERROR: Notion token missing. Add NOTION_API_KEY to "
        f"{HERMES_HOME / 'secrets' / 'notion.env'} "
        "(integration at https://www.notion.so/my-integrations, share Sycode tasks with it)."
    )
    return 1


def selfcheck() -> int:
    failures = []
    if compact_page_id("https://app.notion.com/3c09b3947299811fa13fc0e68fbe9d93") != "3c09b3947299811fa13fc0e68fbe9d93":
        failures.append("compact_page_id url")
    if compact_page_id("3c09b394-7299-811f-a13f-c0e68fbe9d93") != "3c09b3947299811fa13fc0e68fbe9d93":
        failures.append("compact_page_id uuid")
    if idempotency_key("3c09b3947299811fa13fc0e68fbe9d93") != "notion:3c09b3947299811fa13fc0e68fbe9d93":
        failures.append("idempotency_key")
    if priority_int("p0") != 0 or priority_int("p2") != 2 or priority_int("nope") != 2:
        failures.append("priority_int")
    ok = {
        "page_id": "a" * 32,
        "url": "https://www.notion.so/" + "a" * 32,
        "title": "t",
        "note": "",
        "owner": "sycode",
        "priority": "p0",
        "status": "open",
        "hermes_id": "",
        "hermes_board": "",
    }
    if not should_sync(ok):
        failures.append("should_sync sycode")
    skip = dict(ok, owner="seam")
    if should_sync(skip):
        failures.append("should_sync seam")
    done = dict(ok, status="done")
    if should_sync(done):
        failures.append("should_sync done")
    linked = dict(ok, hermes_id="t_abc")
    if should_sync(linked):
        failures.append("should_sync linked")
    if resolve_board(ok) != DEFAULT_BOARD:
        failures.append("default board")
    try:
        resolve_board(dict(ok, hermes_board="not-a-board"))
        failures.append("board allowlist")
    except ValueError:
        pass
    if "--board" not in " ".join(
        [
            "hermes",
            "kanban",
            "--board",
            DEFAULT_BOARD,
            "create",
            "x",
            "--idempotency-key",
            "notion:x",
        ]
    ):
        failures.append("board+key pairing")
    if failures:
        print("SELFCHECK FAIL: " + ", ".join(failures))
        return 1
    print("SELFCHECK OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selfcheck", action="store_true")
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    lock_fd = acquire_lock()
    if lock_fd is None:
        return 0
    try:
        token = notion_token()
        if not token:
            return _missing_token_alert()
        return run_sync(dry_run=args.dry_run, token=token)
    finally:
        try:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
