#!/usr/bin/env python3
"""Drain Alertmanager OOB spool → Discord stdout + critical Hermes kanban cards.

No-agent cron delivers stdout to Discord (#critical-alerts). Critical firing
alerts ALSO become deduped kanban cards on board sycode-trading (assignee
trading-devops) so Frank sees them in the Hermes pipe — Discord/Telegram are
not the primary ops surface.

Dedup: alertname + Alertmanager fingerprint (fallback: stable hash of labels).
Only severity=critical OR route=critical-alerts create/refresh cards; warnings
stay Discord-only via the existing stdout path.

Kanban writes are fail-open: a board/CLI failure must never suppress Discord
delivery or leave spool files stuck.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("ALERT_SPOOL_ROOT", "/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool"))
INCOMING = ROOT / "incoming"
ARCHIVE = ROOT / "archive"
DRAIN_LABEL = os.environ.get("ALERT_SPOOL_LABEL", "SYCODE ALERTMANAGER OOB RELAY")
DRAIN_TASK_TAG = os.environ.get("ALERT_SPOOL_TASK_TAG", "t_0e94fe00")

# --- Rate-limit-aware backoff config (kanban t_e656efdc) -----------------------
# Local-only drain; backoff envelope kept for consistency with sibling mechanisms.
RLB_BASE = float(os.environ.get("RLB_BASE_SECONDS", "2.0"))
RLB_MAX = float(os.environ.get("RLB_MAX_SECONDS", "60.0"))
RLB_ATTEMPTS = int(os.environ.get("RLB_MAX_ATTEMPTS", "3"))

# --- Critical → kanban (Frank 2026-09-04 GO: hermes pipe, not chat black holes) -
KANBAN_ENABLED = os.environ.get("ALERT_KANBAN_ENABLED", "1").strip() not in {"0", "false", "no"}
HERMES = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
KANBAN_BOARD = os.environ.get("ALERT_KANBAN_BOARD", "sycode-trading")
KANBAN_ASSIGNEE = os.environ.get("ALERT_KANBAN_ASSIGNEE", "trading-devops")
KANBAN_CREATED_BY = os.environ.get("ALERT_KANBAN_CREATED_BY", "sycode-alertmanager-spool")
KANBAN_PRIORITY = int(os.environ.get("ALERT_KANBAN_PRIORITY", "80"))
KANBAN_COMMENT_COOLDOWN = int(os.environ.get("ALERT_KANBAN_COMMENT_COOLDOWN_SECONDS", "3600"))
OPEN_STATUSES = ("ready", "todo", "running", "blocked", "review")
RESOLVE_STATUSES = ("ready", "todo")  # only auto-complete idle cards on resolve
_TASKID_RE = re.compile(r"\b(t_[0-9a-f]{8,})\b")


def labels_text(labels: dict[str, Any]) -> str:
    parts = []
    for key in ("alertname", "severity", "instance", "job", "container", "route"):
        if labels.get(key):
            parts.append(f"{key}={labels[key]}")
    return " ".join(parts) if parts else "labels=none"


def summarize_payload(payload: dict[str, Any]) -> list[str]:
    status = payload.get("status", "unknown")
    alerts = payload.get("alerts") or []
    lines = [f"Alertmanager OOB relay received {len(alerts)} alert(s), status={status}"]
    for alert in alerts[:12]:
        labels = alert.get("labels") or {}
        ann = alert.get("annotations") or {}
        starts = alert.get("startsAt") or alert.get("activeAt") or "unknown-start"
        summary = ann.get("summary") or ann.get("description") or "no summary"
        lines.append(f"- {labels_text(labels)} startsAt={starts}: {summary}")
    if len(alerts) > 12:
        lines.append(f"... {len(alerts) - 12} more alerts omitted")
    return lines


def _priority_key(path: Path) -> tuple[int, str]:
    # t_f32a3261: cronrelay-* files must never be starved by alertmanager-* flood.
    return (0 if path.name.startswith("cronrelay-") else 1, path.name)


def is_critical_alert(alert: dict[str, Any]) -> bool:
    labels = alert.get("labels") or {}
    return labels.get("severity") == "critical" or labels.get("route") == "critical-alerts"


def alert_fingerprint(alert: dict[str, Any]) -> str:
    fp = alert.get("fingerprint")
    if isinstance(fp, str) and fp.strip():
        return fp.strip()
    labels = alert.get("labels") or {}
    raw = json.dumps(labels, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def dedupe_key(alert: dict[str, Any]) -> str:
    labels = alert.get("labels") or {}
    name = str(labels.get("alertname") or "unknown").strip() or "unknown"
    return f"sycode-am:{name}:{alert_fingerprint(alert)}"


def _kanban_home() -> Path:
    try:
        hermes_agent = "/home/frank/.hermes/hermes-agent"
        if hermes_agent not in sys.path:
            sys.path.insert(0, hermes_agent)
        from hermes_cli.kanban_db import kanban_home as _kh  # type: ignore

        return Path(_kh())
    except Exception:
        override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
        if override:
            return Path(override).expanduser()
        env_home = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes")).expanduser()
        if env_home.parent.name == "profiles":
            return env_home.parent.parent
        return env_home


def _board_db() -> Path:
    return _kanban_home() / "kanban" / "boards" / KANBAN_BOARD / "kanban.db"


def _run_hermes(args: list[str], timeout: int = 45) -> subprocess.CompletedProcess | None:
    env = os.environ.copy()
    env.setdefault("HERMES_HOME", os.environ.get("HERMES_HOME", "/home/frank/.hermes"))
    try:
        return subprocess.run(
            [HERMES, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except Exception:
        return None


def _extract_task_id(stdout: str) -> str | None:
    try:
        data = json.loads((stdout or "").strip())
        if isinstance(data, dict):
            tid = data.get("id") or data.get("task_id")
            if tid:
                return str(tid)
    except Exception:
        pass
    m = _TASKID_RE.search(stdout or "")
    return m.group(1) if m else None


def existing_open_card(key: str) -> tuple[str, str] | None:
    """Return (task_id, status) for an open card with this idempotency key."""
    db = _board_db()
    if not db.exists():
        return None
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5)
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        row = con.execute(
            f"SELECT id, status FROM tasks WHERE idempotency_key=? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            (key, *OPEN_STATUSES),
        ).fetchone()
        con.close()
        if row:
            return str(row[0]), str(row[1])
    except Exception:
        return None
    return None


def _card_status(task_id: str) -> str | None:
    db = _board_db()
    if not db.exists():
        return None
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5)
        row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        return str(row[0]) if row else None
    except Exception:
        return None


def _comment_stamp_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._:-]+", "_", key)[:180]
    return ROOT / f".kanban-comment-{safe}"


def _comment_allowed(key: str) -> bool:
    stamp = _comment_stamp_path(key)
    now = int(time.time())
    try:
        last = int(stamp.read_text().strip())
    except Exception:
        last = 0
    if now - last < KANBAN_COMMENT_COOLDOWN:
        return False
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now))
    except Exception:
        pass
    return True


def _alert_title(alert: dict[str, Any]) -> str:
    labels = alert.get("labels") or {}
    name = labels.get("alertname") or "unknown"
    summary = (alert.get("annotations") or {}).get("summary") or ""
    summary = " ".join(str(summary).split())
    if summary:
        return f"[critical] {name}: {summary}"[:160]
    return f"[critical] Alertmanager: {name}"[:160]


def _alert_body(alert: dict[str, Any], key: str) -> str:
    labels = alert.get("labels") or {}
    ann = alert.get("annotations") or {}
    lines = [
        f"Auto-card from `{KANBAN_CREATED_BY}` (Alertmanager OOB spool drain).",
        "Primary ops surface = Hermes kanban (sycode-trading / trading-devops).",
        "Discord #critical-alerts remains secondary fan-out from the same drain.",
        "",
        f"Dedupe key: `{key}`",
        f"Fingerprint: `{alert_fingerprint(alert)}`",
        f"Alert status: `{alert.get('status') or 'unknown'}`",
        f"StartsAt: `{alert.get('startsAt') or alert.get('activeAt') or 'unknown'}`",
        "",
        "Labels:",
        "```",
        json.dumps(labels, sort_keys=True, indent=2),
        "```",
        "",
        f"Summary: {ann.get('summary') or '(none)'}",
        f"Description: {ann.get('description') or '(none)'}",
        "",
        "Acceptance: condition cleared in Prometheus/Alertmanager; card commented",
        "RESOLVED / completed; no live trading, credential, or A3 mutation from this path.",
    ]
    return "\n".join(lines)


def create_or_refresh_card(alert: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    key = dedupe_key(alert)
    status = str(alert.get("status") or "").lower()
    open_card = existing_open_card(key)

    if status == "resolved":
        if not open_card:
            return {"action": "resolved_noop", "key": key}
        tid, st = open_card
        if dry_run:
            return {"action": "dry_run_resolve", "key": key, "task_id": tid, "status": st}
        body = (
            f"[{KANBAN_CREATED_BY} RESOLVED @ {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
            f"Alertmanager status=resolved for `{key}`."
        )
        proc = _run_hermes(["kanban", "--board", KANBAN_BOARD, "comment", tid, body])
        if proc is None or proc.returncode != 0:
            return {"action": "resolve_comment_failed", "key": key, "task_id": tid}
        if st in RESOLVE_STATUSES:
            proc2 = _run_hermes(
                [
                    "kanban",
                    "--board",
                    KANBAN_BOARD,
                    "complete",
                    tid,
                    "--summary",
                    f"{KANBAN_CREATED_BY}: alert resolved ({key})",
                ]
            )
            if proc2 is None or proc2.returncode != 0:
                return {"action": "resolve_complete_failed", "key": key, "task_id": tid}
            return {"action": "resolved_completed", "key": key, "task_id": tid}
        return {"action": "resolved_commented", "key": key, "task_id": tid, "status": st}

    # firing / unknown → ensure an open card exists
    if open_card:
        tid, st = open_card
        if dry_run:
            return {"action": "dry_run_dedupe", "key": key, "task_id": tid, "status": st}
        if _comment_allowed(key):
            ann = alert.get("annotations") or {}
            body = (
                f"[{KANBAN_CREATED_BY} still FIRING @ "
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}]\n"
                f"{ann.get('summary') or ann.get('description') or labels_text(alert.get('labels') or {})}"
            )
            proc = _run_hermes(["kanban", "--board", KANBAN_BOARD, "comment", tid, body])
            if proc is None or proc.returncode != 0:
                return {"action": "dedupe_comment_failed", "key": key, "task_id": tid}
            return {"action": "deduped_commented", "key": key, "task_id": tid}
        return {"action": "deduped", "key": key, "task_id": tid}

    if dry_run:
        return {"action": "dry_run_create", "key": key, "title": _alert_title(alert)}

    # Native hermes --idempotency-key returns existing NON-ARCHIVED task (incl. done).
    # We already checked open cards; if a done card blocks create, mint a reopen key.
    keys_to_try = [key, f"{key}:reopen:{time.strftime('%Y%m%d', time.gmtime())}"]
    for try_key in keys_to_try:
        args = [
            "kanban",
            "--board",
            KANBAN_BOARD,
            "create",
            _alert_title(alert),
            "--assignee",
            KANBAN_ASSIGNEE,
            "--priority",
            str(KANBAN_PRIORITY),
            "--created-by",
            KANBAN_CREATED_BY,
            "--idempotency-key",
            try_key,
            "--body",
            _alert_body(alert, try_key),
            "--json",
        ]
        proc = _run_hermes(args)
        if proc is None or proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "") if proc else "subprocess timeout/exception"
            return {"action": "create_failed", "key": try_key, "err": str(err)[:300]}
        tid = _extract_task_id(proc.stdout or "")
        if not tid:
            return {"action": "create_failed", "key": try_key, "err": "no_task_id"}
        st = _card_status(tid)
        if st in OPEN_STATUSES or st is None:
            return {"action": "created", "key": try_key, "task_id": tid, "status": st}
        # got a closed non-archived card back — try reopen key
        continue
    return {"action": "create_blocked_by_closed", "key": key}


def route_payload_to_kanban(payload: dict[str, Any], *, dry_run: bool = False) -> list[dict[str, Any]]:
    if not KANBAN_ENABLED and not dry_run:
        return [{"action": "kanban_disabled"}]
    results: list[dict[str, Any]] = []
    for alert in payload.get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        if not is_critical_alert(alert):
            continue
        try:
            results.append(create_or_refresh_card(alert, dry_run=dry_run))
        except Exception as exc:
            results.append(
                {
                    "action": "kanban_exception",
                    "err": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
    return results


def _priority_key_files() -> list[Path]:
    if not INCOMING.exists():
        return []
    return sorted((p for p in INCOMING.glob("*.json") if p.is_file()), key=_priority_key)[:20]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    dry_run = "--dry-run" in argv
    # Optional: route a single JSON file without consuming spool (Isolation dry-run).
    if "--file" in argv:
        idx = argv.index("--file")
        path = Path(argv[idx + 1])
        payload = json.loads(path.read_text())
        print("\n".join(summarize_payload(payload if isinstance(payload, dict) else {})))
        results = route_payload_to_kanban(payload if isinstance(payload, dict) else {}, dry_run=True)
        print("KANBAN_ROUTE " + json.dumps(results, sort_keys=True))
        return 0

    if not INCOMING.exists():
        return 0
    files = _priority_key_files()
    if not files:
        return 0
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    stuck: list[str] = []
    kanban_notes: list[str] = []
    for path in files:
        # CONSUME BEFORE EMITTING (2026-07-29): never re-send what we cannot retire.
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:
            payload = None
            summary = (
                f"Alertmanager OOB spool drain: unreadable {path.name}: "
                f"{type(exc).__name__}: {str(exc)[:120]}"
            )
        else:
            summary = "\n".join(summarize_payload(payload if isinstance(payload, dict) else {}))
        prefix = "" if payload is not None else "error-"
        if dry_run:
            blocks.append(summary)
            if isinstance(payload, dict):
                for r in route_payload_to_kanban(payload, dry_run=True):
                    kanban_notes.append(json.dumps(r, sort_keys=True))
            continue
        try:
            shutil.move(str(path), str(ARCHIVE / f"{prefix}{int(time.time())}-{path.name}"))
        except Exception as exc:
            stuck.append(f"{path.name}: {type(exc).__name__}")
            continue
        blocks.append(summary)
        if isinstance(payload, dict):
            for r in route_payload_to_kanban(payload, dry_run=False):
                action = r.get("action")
                if action in {
                    "created",
                    "deduped",
                    "deduped_commented",
                    "resolved_completed",
                    "resolved_commented",
                }:
                    kanban_notes.append(
                        f"kanban {action} key={r.get('key')} task={r.get('task_id')}"
                    )
                elif action and action.endswith("_failed"):
                    kanban_notes.append(f"kanban FAIL {json.dumps(r, sort_keys=True)[:240]}")

    if stuck and not dry_run:
        stamp = ROOT / ".drain-blocked-last-report"
        now = int(time.time())
        try:
            last = int(stamp.read_text().strip())
        except Exception:
            last = 0
        if now - last >= int(os.environ.get("DRAIN_BLOCKED_REALERT_SECONDS", "3600")):
            try:
                stamp.write_text(str(now))
            except Exception:
                pass
            print(
                "🚨 SYCODE ALERTMANAGER SPOOL DRAIN BLOCKED — "
                f"{len(stuck)} file(s) cannot be archived, so spooled alerts are NOT being delivered.\n"
                f"First: {stuck[0]}\n"
                f"Backlog in {INCOMING}: {len(list(INCOMING.glob('*.json')))} file(s).\n"
                "Usual cause: /spool/incoming recreated root-owned by the relay container; the host drain "
                "runs as frank and needs write permission on the DIRECTORY to unlink. Fix: "
                "docker exec sycode-alertmanager-oob-relay chmod 0777 /spool/incoming"
            )
        return 0 if blocks else 1

    if not blocks:
        return 0
    print(f"🔴 {DRAIN_LABEL} — spooled critical alert delivery ({DRAIN_TASK_TAG}):")
    print("\n\n".join(blocks))
    if kanban_notes:
        print("KANBAN: " + " | ".join(kanban_notes[:12]))
    return 0


def _selftest() -> int:
    failures: list[str] = []
    firing = {
        "status": "firing",
        "fingerprint": "deadbeefcafebabe",
        "labels": {
            "alertname": "SelftestCritical",
            "severity": "critical",
            "instance": "test",
        },
        "annotations": {"summary": "selftest critical alert"},
    }
    warning = {
        "status": "firing",
        "fingerprint": "warnwarnwarnwarn",
        "labels": {"alertname": "SelftestWarning", "severity": "warning"},
        "annotations": {"summary": "should not card"},
    }
    if not is_critical_alert(firing):
        failures.append("critical severity must match")
    if is_critical_alert(warning):
        failures.append("warning must not match")
    route_crit = {
        "status": "firing",
        "fingerprint": "route1111111111",
        "labels": {"alertname": "CredExhausted", "route": "critical-alerts"},
    }
    if not is_critical_alert(route_crit):
        failures.append("route=critical-alerts must match")
    k = dedupe_key(firing)
    if k != "sycode-am:SelftestCritical:deadbeefcafebabe":
        failures.append(f"bad dedupe key {k}")
    k2 = dedupe_key(
        {
            "labels": {"alertname": "NoFp", "severity": "critical", "x": "1"},
        }
    )
    if not k2.startswith("sycode-am:NoFp:") or len(k2.split(":")[-1]) < 8:
        failures.append(f"fallback fingerprint key bad: {k2}")

    results = route_payload_to_kanban({"alerts": [firing, warning, route_crit]}, dry_run=True)
    actions = [r.get("action") for r in results]
    if "dry_run_create" not in actions:
        failures.append(f"expected dry_run_create, got {results}")
    if len(results) != 2:
        failures.append(f"warning must be skipped; got {len(results)} results: {results}")

    # Test kill switch: KANBAN_ENABLED=False with dry_run=False must return kanban_disabled
    global KANBAN_ENABLED
    saved_enabled = KANBAN_ENABLED
    try:
        KANBAN_ENABLED = False
        kill_results = route_payload_to_kanban({"alerts": [firing]}, dry_run=False)
        if len(kill_results) != 1 or kill_results[0].get("action") != "kanban_disabled":
            failures.append(f"kill switch test failed: expected [{{action: kanban_disabled}}], got {kill_results}")
    finally:
        KANBAN_ENABLED = saved_enabled

    if failures:
        print("SELFTEST_FAIL")
        for fl in failures:
            print(" -", fl)
        return 1
    print(
        "SELFTEST_PASS critical_filter dedupe_key fallback_fp kill_switch "
        f"dry_run_create={actions.count('dry_run_create')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
