#!/usr/bin/env python3
"""Route Sycode r_multiple_labels freshness stalls to Hermes Kanban.

No-agent cron contract:
- Healthy/recovered: silent stdout, exit 0.
- Stale once per outage episode: create/update an idempotent sycode-trading kanban alert
  card and print one line.
- Operational failure: print ERROR and exit non-zero so native cron records monitor failure.

This is intentionally narrower than sycode_critical_stream_freshness.py: it protects the
r_multiple_labels accrual lane even if broader critical-stream/Discord delivery is broken.

Idempotency design (t_486cf5bf / t_cecb8268):
- A *freshness family* is identified by (table name, high-water mark). The canonical
  incident owner for this family is t_cecb8268.
- An episode key embeds the family hash (NOT timestamps or measured values) so repeated
  breaches of the same frozen producer resolve to the open owner instead of spawning a
  new PM triage lane every re-page cycle.
- Genuinely new tables or recovered producers form distinct families and are NOT hidden.

Family-stable idempotency is modelled on the proven fusion-calibration anomaly dedupe
(PR #819, SHA f6a6e54a34): see execution/fusion_calibration_anomaly_kanban.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# Injectable time function — defaults to real UTC wall-clock; overridden in tests
# via R_MULTIPLE_FRESHNESS_NOW env var containing a Python expression, or by
# direct attribute assignment after load_module().
_DEFAULT_NOW: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _now() -> datetime:
    """Current UTC time — overridable in tests."""
    return _DEFAULT_NOW()


DEFAULT_BOARD = "sycode-trading"
DEFAULT_ASSIGNEE = "sycode-trading-pm"
DEFAULT_PRIORITY = 95

# Default title used for fresh-outage cards. Can be overridden per-call.
DEFAULT_TITLE = "ALERT: r_multiple_labels stale - label accrual may be stopped"

# Table watched by this route; used in the family signature.
WATCHED_TABLE = "public.r_multiple_labels"

IDEMPOTENCY_PREFIX = "sycode-r-multiple-labels-freshness-stale"

INCIDENT_SOURCE_TASK = "t_c9fe72b7"

REPAGE_HOURS = max(1, int(os.environ.get("R_MULTIPLE_LABEL_FRESHNESS_REPAGE_HOURS", "6")))
THRESHOLD_MIN = int(os.environ.get("R_MULTIPLE_LABEL_FRESHNESS_THRESHOLD_MIN", "180"))
BOARD = os.environ.get("R_MULTIPLE_LABEL_FRESHNESS_BOARD", DEFAULT_BOARD)
ASSIGNEE = os.environ.get("R_MULTIPLE_LABEL_FRESHNESS_ASSIGNEE", DEFAULT_ASSIGNEE)
TITLE = os.environ.get("R_MULTIPLE_LABEL_FRESHNESS_TITLE", DEFAULT_TITLE)
STATE_PATH = Path(os.environ.get(
    "R_MULTIPLE_LABEL_FRESHNESS_STATE",
    "/home/frank/.hermes/var/sycode_r_multiple_label_freshness_route_state.json",
))

# Kanban runtime env vars the CLI inherits from workers; scrub them from every
# kanban sub-process so it cannot reuse a worker claim or board accidentally.
KANBAN_ENV_OVERRIDES = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_SESSION_SOURCE",
)

LIVE_STATUSES = ("running", "ready", "review", "blocked", "todo", "scheduled", "triage")
ROUTE_FAMILY_MARKERS = ("r_multiple_labels", "r-multiple", "r multiple")


def _card_text(card: dict[str, Any]) -> str:
    return f"{card.get('title', '')}\\n{card.get('body', '')}".lower()


def _is_live_family_card(card: dict[str, Any]) -> bool:
    return card.get("status") in LIVE_STATUSES and any(
        marker in _card_text(card) for marker in ROUTE_FAMILY_MARKERS
    )


def _list_family_cards(board: str, timeout: int) -> list[dict[str, Any]]:
    """Read non-terminal family cards; never treat done/archived as owners."""
    cards: list[dict[str, Any]] = []
    for status in LIVE_STATUSES:
        proc = run(["hermes", "kanban", "--board", board, "list", "--status", status, "--json"], timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"kanban list failed rc={proc.returncode}: {proc.stderr.strip()[:300]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"kanban list returned invalid JSON: {exc}") from exc
        rows = data if isinstance(data, list) else data.get("tasks", data.get("data", []))
        cards.extend(row for row in rows if isinstance(row, dict) and _is_live_family_card(row))
    return cards


def _role_score(card: dict[str, Any], role: str) -> int:
    text = _card_text(card)
    score = 0
    if f"route_role: {role}" in text or f"route-role: {role}" in text:
        score += 100
    if "canonical" in text:
        score += 10
    if card.get("status") in ("running", "ready", "review"):
        score += 5
    return score


def _create_route_packet(board: str, assignee: str, timeout: int, role: str) -> str:
    """Create a stable role packet when no live canonical card exists."""
    role_key = f"{IDEMPOTENCY_PREFIX}-{role}-v1"
    title = f"OPS: r_multiple_labels freshness {role} packet"
    body = (
        f"route_role: {role}\\n"
        "route_family: sycode-r-multiple-labels-freshness\\n"
        "canonical: true\\n"
        "This is the live routing packet for the r_multiple_labels freshness family. "
        "Producer outage remediation remains separate and Frank-gated; do not add credentials, "
        "deploys, cron edits, DB writes, or trading actions here.\\n"
    )
    proc = run([
        "hermes", "kanban", "--board", board, "create", title,
        "--assignee", assignee, "--priority", str(DEFAULT_PRIORITY),
        "--idempotency-key", role_key, "--created-by",
        "sycode-r-multiple-label-freshness-route", "--body", body, "--json",
    ], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"kanban role packet create failed rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        data = json.loads(proc.stdout)
        return data.get("id") or data.get("task_id") or proc.stdout.strip()
    except json.JSONDecodeError:
        return proc.stdout.strip()


def resolve_route_targets(board: str, assignee: str, timeout: int, *, create_missing: bool = True) -> tuple[str, str]:
    """Resolve live owner/consumer packets, creating each at most once.

    Terminal cards are deliberately excluded. Stable creation keys make a race or
    repeated outage idempotent; role markers make later reads prefer those packets.
    """
    cards = _list_family_cards(board, timeout)
    targets: dict[str, str] = {}
    for role in ("owner", "consumer"):
        matching = sorted(
            (card for card in cards if f"route_role: {role}" in _card_text(card)
             or f"route-role: {role}" in _card_text(card)),
            key=lambda card: _role_score(card, role), reverse=True,
        )
        if matching:
            targets[role] = str(matching[0]["id"])
        elif create_missing:
            targets[role] = _create_route_packet(board, assignee, timeout, role)
        else:
            targets[role] = f"UNRESOLVED_LIVE_{role.upper()}"
    return targets["owner"], targets["consumer"]


def link_alert_card(board: str, parent_task: str, child_task: str, timeout: int) -> None:
    """Link each alert to its live owner without changing producer state."""
    proc = run(["hermes", "kanban", "--board", board, "link", parent_task, child_task], timeout=timeout)
    if proc.returncode != 0 and "already" not in proc.stderr.lower():
        raise RuntimeError(f"kanban link failed rc={proc.returncode}: {proc.stderr.strip()[:300]}")


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a command under a clean env. Host-local Postgres convention only."""
    env = os.environ.copy()
    env.setdefault("PGPASSWORD", "postgres")
    for key in KANBAN_ENV_OVERRIDES:
        env.pop(key, None)
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=env)


def psql_scalar() -> tuple[int, str | None, str]:
    sql = """
    SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(computed_at)))::bigint, -1)::text
           || '|' || COALESCE(max(computed_at)::text, 'NEVER')
           || '|' || count(*)::text
    FROM public.r_multiple_labels;
    """
    proc = run([
        "psql", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres", "-d", "postgres",
        "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ], timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    line = proc.stdout.strip().splitlines()[-1].strip()
    age_s_raw, last_ts, count_raw = (line.split("|") + ["", "", ""])[:3]
    return int(age_s_raw), (None if last_ts == "NEVER" else last_ts), count_raw


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------------------
# Family-stable idempotency (PR #819 pattern)
# ---------------------------------------------------------------------------

def freshness_alert_signature(table: str, last_ts: str | None) -> str:
    """Stable identity of a freshness-alert family.

    Built from table name + high-water mark. Does NOT include report_timestamp,
    age, row count, or any other measured quantity — those would cause the key
    to change every tick and the route to re-fire forever (the original bug).

    A genuinely new table or a recovered table (empty/NEVER ts) forms a distinct
    family and is therefore NOT hidden.
    """
    # Strip trailing whitespace / tz offsets so the signature stays stable.
    safe_ts = last_ts.strip() if last_ts else ""
    return f"{table}|{safe_ts}"


def family_idempotency_key(family_signature: str) -> str:
    """SHA256 digest of the family signature -> stable across runs."""
    digest = hashlib.sha256(family_signature.encode("utf-8")).hexdigest()[:16]
    return f"{IDEMPOTENCY_PREFIX}-{digest}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route Sycode r_multiple_labels freshness stalls to Hermes Kanban."
    )
    parser.add_argument("--board", default=BOARD)
    parser.add_argument("--assignee", default=ASSIGNEE)
    parser.add_argument("--priority", type=int, default=DEFAULT_PRIORITY)
    parser.add_argument("--title", default=TITLE)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true", help="Print would-create JSON")
    return parser.parse_args(argv)


def build_command(**kwargs: str) -> list[str]:
    """Build ``hermes kanban --board <board> create …`` CLI argument list."""
    cmd: list[str] = [
        "hermes", "kanban", "--board", kwargs["board"], "create", kwargs["title"],
    ]
    cmd.extend(["--assignee", kwargs["assignee"]])
    cmd.extend(["--priority", str(kwargs["priority"])])
    cmd.extend(["--idempotency-key", kwargs["idempotency_key"]])
    cmd.extend(["--created-by", "sycode-r-multiple-label-freshness-route"])
    cmd.extend(["--body", kwargs["body"]])
    cmd.append("--json")
    return cmd


def create_alert_card(args: argparse.Namespace, *, payload: dict[str, Any]) -> str:
    """Create an alert card via the kanban CLI. Returns the task id string."""
    body = payload["body"]
    command = build_command(
        board=args.board,
        assignee=args.assignee,
        priority=str(args.priority),
        idempotency_key=payload["idempotency_key"],
        body=body,
        title=args.title,
    )
    proc = run(command, timeout=args.timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"kanban create failed rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        data = json.loads(proc.stdout)
        return data.get("id") or data.get("task_id") or proc.stdout.strip()
    except Exception:
        return proc.stdout.strip()


def build_covered_by(owner_task: str, consumer_task: str, family_key: str) -> dict[str, Any]:
    """Return a covered_by object for dry-run output."""
    return {
        "owner_task": owner_task,
        "consumer_task": consumer_task,
        "family_stable_idempotency_key": family_key,
        "notes": (
            "Repeated threshold-breach runs of this freshness family reuse this "
            f"family-stable idempotency_key; covered by owner_task {owner_task}. "
            "Do not open a new PM triage lane per report cycle. A genuinely new "
            "alert family (different table or recovered producer) forms a new "
            "key and is NOT hidden."
        ),
    }


def repage_bucket(now: datetime | None = None) -> str:
    """Coarse wall-clock bucket so a frozen high-water mark still re-pages.

    Floors UTC now to REPAGE_HOURS. Every run inside the same bucket is deduped;
    the first run of the next bucket re-pages a still-active outage.
    """
    now = now or _now()
    floored = now.replace(minute=0, second=0, microsecond=0)
    floored = floored.replace(hour=(floored.hour // REPAGE_HOURS) * REPAGE_HOURS)
    return floored.strftime("%Y%m%dT%H")


def build_body(*, payload: dict[str, Any], owner_task: str, consumer_task: str,
               family_sig: str, family_key: str, repage: bool,
               now_str: str) -> str:
    """Build the card body with routing + diagnostic evidence."""
    age_min = payload["age_min"]
    last_ts = payload["last_ts"]
    row_count = payload["row_count"]
    threshold = payload["threshold"]

    prefix = "ALERT (CONTINUING)" if repage else "ALERT"
    lines = [
        f"Automated freshness route from sycode_r_multiple_label_freshness_route.py at {now_str}.\n",
        "",
        f"## Routing\n",
        f"- source_table: {WATCHED_TABLE}\n",
        f"- max(computed_at): {last_ts or 'NEVER'}\n",
        f"- age: {age_min}m (threshold {threshold}m)\n",
        f"- row_count: {payload['row_count']}\n",
        f"- dedupe_family: {family_sig}\n",
        f"- family_stable_idempotency_key: {family_key}\n",
        f"- owner_task: {owner_task}\n",
        f"- consumer_task: {consumer_task}\n",
        f"- covered_by: this card resolves to the open owner_task above; do NOT open a new PM triage lane per report cycle.\n",
        f"- source: sycode_r_multiple_label_freshness_route.py consuming r_multiple_labels freshness data\n",
        "",
    ]

    if repage:
        lines += [
            f"This is a RE-PAGE for a CONTINUING outage (re-page cadence {REPAGE_HOURS}h). "
            "The high-water mark has not moved since the previous alert, so the producer is "
            "still dead; an earlier card being closed does NOT mean recovery.\n",
            "",
        ]

    lines += [
        f"This protects incident {INCIDENT_SOURCE_TASK}: missing host-side env / broken labeler cron "
        "must never silently stop label accrual. Investigate the Hermes cron(s) named "
        "sycode-r-multiple-labeler, /home/frank/.hermes/scripts/r_multiple_labeler.sh, "
        "/home/frank/sycode-trading/server/scripts/r-multiple-labeler-recurring.sh, "
        "and DB connectivity. Do not recreate credentials; server/.env.prod is Frank-gated. "
        "Read-only DB/runtime probes are allowed; live trading/deploy/credential changes remain gated.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        age_s, last_ts, row_count = psql_scalar()
    except RuntimeError as exc:
        print(f"ERROR sycode-r-multiple-label-freshness-route: {exc}", file=sys.stderr)
        return 1

    stale = age_s < 0 or age_s > THRESHOLD_MIN * 60

    if not stale:
        state = load_state()
        if state.get("active"):
            save_state({
                "active": False,
                "recovered_at": _now().isoformat(),
                "last_ts": last_ts,
            })
        return 0

    age_min = -1 if age_s < 0 else age_s // 60

    # --- Idempotency decision -------------------------------------------------
    family_sig = freshness_alert_signature(WATCHED_TABLE, last_ts)
    family_key = family_idempotency_key(family_sig)

    # Resolve against live board state; terminal historical cards are excluded.
    owner_task, consumer_task = resolve_route_targets(
        args.board, args.assignee, args.timeout, create_missing=not args.dry_run,
    )

    # Repage bucket for continuing-outage semantics.
    bucket = repage_bucket()

    state = load_state()
    prev_episode_key = state.get("episode_key")
    prev_family_key = state.get("family_key", "")
    was_active = bool(state.get("active"))

    # Is this a continuation of the same episode? Same table+ts => same family_key.
    if was_active and prev_family_key and prev_family_key == family_key:
        # Same family, producer still dead => re-page.
        repage = True
    else:
        repage = False

    now_str = _now().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Full episode key = family_key :bucket (so we still know which bucket alerted us).
    episode_key = f"{family_key}:{bucket}"

    # If the state already recorded the SAME episode_key, this run is fully idempotent
    # (same family AND same bucket). Skip card creation entirely.
    if was_active and prev_episode_key == episode_key:
        return 0

    # Build the alert payload shared between the card body and dry-run output.
    payload: dict[str, Any] = {
        "age_min": age_min,
        "last_ts": last_ts,
        "row_count": row_count,
        "threshold": THRESHOLD_MIN,
        "family_signature": family_sig,
        "family_stable_idempotency_key": family_key,
        "owner_task": owner_task,
        "consumer_task": consumer_task,
        "repage": repage,
        "bucket": bucket,
        "episode_key": episode_key,
    }

    if args.dry_run:
        # DRY RUN: emit the would-be JSON instead of calling the kanban CLI.
        body = build_body(payload=payload, owner_task=owner_task, consumer_task=consumer_task,
                          family_sig=family_sig, family_key=family_key, repage=repage, now_str=now_str)
        output: dict[str, Any] = {
            "created": True,
            "dry_run": True,
            "family_stable": True,
            "dedupe_family": family_sig,
            "family_stable_idempotency_key": family_key,
            "idempotency_key": episode_key,
            "board": args.board,
            "assignee": args.assignee,
            "priority": args.priority,
            "title": args.title,
            "body": body,
            "repage": repage,
            "covered_by": build_covered_by(owner_task, consumer_task, family_key),
        }
        print(json.dumps(output, indent=2, sort_keys=False))
        return 0

    # BUILD & EXECUTE CARD
    body = build_body(payload=payload, owner_task=owner_task, consumer_task=consumer_task,
                      family_sig=family_sig, family_key=family_key, repage=repage, now_str=now_str)
    payload["body"] = body
    payload["idempotency_key"] = episode_key

    task_id = create_alert_card(args, payload=payload)
    link_alert_card(args.board, owner_task, task_id, args.timeout)

    save_state({
        "active": True,
        "episode_key": episode_key,
        "repage": repage,
        "alerted_at": now_str,
        "task_id": task_id,
        "age_min": age_min,
        "last_ts": last_ts,
        "row_count": row_count,
        "family_key": family_key,
    })

    kind = "RE-ALERT (continuing outage)" if repage else "ALERT"
    print(f"{kind} routed: r_multiple_labels stale age={age_min}m threshold={THRESHOLD_MIN}m "
          f"repage_bucket={bucket}h{REPAGE_HOURS} task={task_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR sycode-r-multiple-label-freshness-route: {exc}", file=sys.stderr)
        raise SystemExit(1)
