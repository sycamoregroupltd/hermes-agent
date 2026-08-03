#!/usr/bin/env python3
"""Fleet-wide protocol-violation consumer artifact (kanban t_319196b4).

Single consumer artifact that aggregates dispatcher-emitted protocol_violation
events so violations are visible fleet-wide instead of being rediscovered per
card.  Deterministic, append-only, fail-open; NO kanban DB / schema mutation.

CHANNEL (agreed contract for dispatcher card t_dac02e83)
--------------------------------------------------------
* Artifact: append-only JSONL at
      /home/frank/.hermes/var/log/protocol_violations.jsonl
  (override with env PROTOCOL_VIOLATION_ARTIFACT for tests/isolation).
* One JSON object per line, fields (dispatcher contract):
      event_type            "protocol_violation"            (required)
      card_id               task id, e.g. "t_abc123"        (required)
      run_id                dispatcher run id (int)         (required)
      ts                    epoch seconds (int)             (required)
      exit_code             worker exit code (int)          (default 0)
      missing_terminal_call human text, e.g. "kanban_complete|kanban_block"
      board                 board slug, e.g. "jarvis-os"    (recommended)
      event_id              dedupe key; derived if absent
      synthetic             true for synthetic test events  (optional)
* Dedupe: by event_id.  If the event carries no event_id, one is derived as
  "{board}:{card_id}:{run_id}" (run ids are unique per board DB, so the triple
  is unique fleet-wide even when board is absent).
* Atomicity: appends are flock-serialized (fcntl) so concurrent dispatchers on
  different boards cannot interleave/corrupt the log.
* Fail-open: a malformed line is skipped with a warning; a duplicate event is
  reported but never errors the caller; an unreadable artifact is treated as
  empty (fresh start) not as a crash.

USAGE
-----
  protocol_violation_artifact.py consume [--file EVENTS.jsonl]   # read events (stdin or file), append new ones
  protocol_violation_artifact.py query [--json] [--raw] [--since EPOCH] [--card ID] [--board B]
  protocol_violation_artifact.py inject --card-id C --run-id R --ts T [--board B] [--exit-code N] [--synthetic]
  protocol_violation_artifact.py test                           # synthetic injection acceptance test (isolated)

ACCEPTANCE (card body)
----------------------
Injecting a protocol_violation event increments fleet count once and the
artifact is queryable: `test` proves it against an isolated artifact; the
real-artifact injection in the task evidence shows the same on the live log.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

ARTIFACT_DEFAULT = "/home/frank/.hermes/var/log/protocol_violations.jsonl"

REQUIRED_FIELDS = ("event_type", "card_id", "run_id", "ts")
CONTRACT_EVENT_TYPE = "protocol_violation"


# --------------------------------------------------------------------------- #
# artifact io
# --------------------------------------------------------------------------- #
def _artifact_path() -> Path:
    return Path(os.environ.get("PROTOCOL_VIOLATION_ARTIFACT") or ARTIFACT_DEFAULT)


def _derive_event_id(event: dict) -> str:
    board = str(event.get("board") or "unknown")
    card = str(event.get("card_id") or "?")
    run = str(event.get("run_id") or "?")
    return f"{board}:{card}:{run}"


def _normalize(event: dict, *, synthetic: bool = False) -> dict:
    """Validate the dispatcher contract and fill defaults. Raises ValueError."""
    missing = [f for f in REQUIRED_FIELDS if event.get(f) in (None, "")]
    if missing:
        raise ValueError(f"missing required fields: {missing}")
    if event.get("event_type") != CONTRACT_EVENT_TYPE:
        raise ValueError(
            f"event_type must be {CONTRACT_EVENT_TYPE!r}, got {event.get('event_type')!r}"
        )
    norm = dict(event)
    norm["exit_code"] = int(norm.get("exit_code", 0))
    norm["ts"] = int(norm["ts"])
    try:
        norm["run_id"] = int(norm["run_id"])
    except (TypeError, ValueError):
        raise ValueError(f"run_id must be int, got {event.get('run_id')!r}")
    norm["card_id"] = str(norm["card_id"])
    norm["missing_terminal_call"] = str(
        norm.get("missing_terminal_call")
        or "kanban_complete|kanban_block"
    )
    norm["event_id"] = str(norm.get("event_id") or _derive_event_id(norm))
    if synthetic:
        norm["synthetic"] = True
    return norm


def _read_all(path: Path) -> list[dict]:
    """Return every valid event line in the artifact. Malformed lines are
    skipped (fail-open) and never abort the read."""
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"[protocol_violation_artifact] WARN skipping malformed line: {line[:120]}", file=sys.stderr)
    except OSError as exc:
        print(f"[protocol_violation_artifact] WARN unreadable artifact ({exc}); treating as empty", file=sys.stderr)
    return events


def _append_events(path: Path, events: list[dict]) -> tuple[int, int]:
    """Append events, deduped by event_id. Returns (appended, duplicates).

    Serialized with an exclusive flock so concurrent dispatcher writes cannot
    interleave. Reads existing ids under the same lock (atomic check+append).
    """
    if not events:
        return 0, 0
    path.parent.mkdir(parents=True, exist_ok=True)
    appended = 0
    duplicates = 0
    try:
        with path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                seen: set[str] = set()
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        seen.add(str(json.loads(line).get("event_id") or ""))
                    except json.JSONDecodeError:
                        continue
                for ev in events:
                    eid = str(ev.get("event_id") or _derive_event_id(ev))
                    if eid in seen:
                        duplicates += 1
                        continue
                    seen.add(eid)
                    fh.write(json.dumps(ev, sort_keys=True, ensure_ascii=False) + "\n")
                    appended += 1
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        print(f"[protocol_violation_artifact] ERROR append failed: {exc}", file=sys.stderr)
        raise
    return appended, duplicates


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #
def _utc_day(ts: int) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _query(events: list[dict], *, since: int | None = None,
           card: str | None = None, board: str | None = None) -> dict:
    fleet_total = 0
    by_card: dict[str, int] = {}
    by_run: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for ev in events:
        ts = int(ev.get("ts") or 0)
        if since is not None and ts < since:
            continue
        if card and str(ev.get("card_id")) != card:
            continue
        if board and str(ev.get("board")) != board:
            continue
        fleet_total += 1
        by_card[str(ev.get("card_id") or "?")] = by_card.get(str(ev.get("card_id") or "?"), 0) + 1
        by_run[str(ev.get("run_id") or "?")] = by_run.get(str(ev.get("run_id") or "?"), 0) + 1
        by_day[_utc_day(ts)] = by_day.get(_utc_day(ts), 0) + 1
    return {
        "fleet_total": fleet_total,
        "by_card": dict(sorted(by_card.items())),
        "by_run": dict(sorted(by_run.items())),
        "by_day": dict(sorted(by_day.items())),
    }


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_consume(args: argparse.Namespace) -> int:
    path = _artifact_path()
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            raw_lines = fh.read().splitlines()
    else:
        raw_lines = sys.stdin.read().splitlines()
    parsed: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"[protocol_violation_artifact] WARN invalid JSON line skipped: {exc}", file=sys.stderr)
    normalized: list[dict] = []
    for ev in parsed:
        try:
            normalized.append(_normalize(ev))
        except ValueError as exc:
            print(f"[protocol_violation_artifact] WARN event skipped (contract violation): {exc}", file=sys.stderr)
    appended, duplicates = _append_events(path, normalized)
    out = {"consumed": len(normalized), "appended": appended, "duplicates": duplicates}
    print(json.dumps(out, sort_keys=True))
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    path = _artifact_path()
    event = {
        "event_type": CONTRACT_EVENT_TYPE,
        "card_id": args.card_id,
        "run_id": args.run_id,
        "ts": args.ts,
        "exit_code": args.exit_code,
        "board": args.board,
    }
    if args.missing_terminal_call:
        event["missing_terminal_call"] = args.missing_terminal_call
    try:
        norm = _normalize(event, synthetic=args.synthetic)
    except ValueError as exc:
        print(f"[protocol_violation_artifact] ERROR invalid event: {exc}", file=sys.stderr)
        return 2
    appended, duplicates = _append_events(path, [norm])
    out = {"event_id": norm["event_id"], "appended": appended, "duplicates": duplicates}
    print(json.dumps(out, sort_keys=True))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    path = _artifact_path()
    events = _read_all(path)
    stats = _query(
        events,
        since=args.since,
        card=args.card,
        board=args.board,
    )
    if args.raw:
        for ev in events:
            print(json.dumps(ev, sort_keys=True, ensure_ascii=False))
        return 0
    if args.json:
        print(json.dumps(stats, sort_keys=True))
        return 0
    print(f"protocol_violations artifact: {path}")
    print(f"  fleet_total      : {stats['fleet_total']}")
    print("  by_card:")
    for k, v in stats["by_card"].items():
        print(f"    {k}: {v}")
    print("  by_run:")
    for k, v in stats["by_run"].items():
        print(f"    {k}: {v}")
    print("  by_day:")
    for k, v in stats["by_day"].items():
        print(f"    {k}: {v}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Synthetic-event injection acceptance test (isolated artifact).

    Proves the card acceptance: injecting a protocol_violation event
    increments fleet count once, dedupe holds on re-inject, and the artifact
    is queryable per card / run / day.
    """
    with tempfile.TemporaryDirectory(prefix="pv-artifact-test-") as tmp:
        artifact = Path(tmp) / "protocol_violations.jsonl"
        os.environ["PROTOCOL_VIOLATION_ARTIFACT"] = str(artifact)

        ev_a = {
            "event_type": CONTRACT_EVENT_TYPE,
            "card_id": "t_synthetic_a",
            "run_id": 90001,
            "ts": 1785740000,
            "exit_code": 0,
            "missing_terminal_call": "kanban_complete|kanban_block",
            "board": "jarvis-os",
            "synthetic": True,
        }
        a = _normalize(ev_a)
        ev_b = {
            "event_type": CONTRACT_EVENT_TYPE,
            "card_id": "t_synthetic_b",
            "run_id": 90002,
            "ts": 1785741000,
            "exit_code": 0,
            "missing_terminal_call": "kanban_complete|kanban_block",
            "board": "sycode-trading",
            "synthetic": True,
        }
        b = _normalize(ev_b)

        # 1) First inject -> fleet count 1
        n1, d1 = _append_events(artifact, [a])
        assert n1 == 1 and d1 == 0, f"first inject expected 1 new, got appended={n1} dup={d1}"
        # 2) Re-inject same event_id -> dedupe: no increment
        n2, d2 = _append_events(artifact, [a])
        assert n2 == 0 and d2 == 1, f"re-inject expected dedupe, got appended={n2} dup={d2}"
        # 3) Second distinct event -> fleet count 2
        n3, d3 = _append_events(artifact, [b])
        assert n3 == 1 and d3 == 0, f"second inject expected 1 new, got appended={n3} dup={d3}"
        # 4) Batch consume with a duplicate + a fresh event
        ev_c = {
            "event_type": CONTRACT_EVENT_TYPE,
            "card_id": "t_synthetic_c",
            "run_id": 90003,
            "ts": 1785742000,
            "exit_code": 1,
            "missing_terminal_call": "kanban_complete|kanban_block",
            "board": "jarvis-os",
            "synthetic": True,
        }
        n4, d4 = _append_events(artifact, [_normalize(ev_c), a])
        assert n4 == 1 and d4 == 1, f"batch expected 1 new 1 dup, got appended={n4} dup={d4}"

        events = _read_all(artifact)
        stats = _query(events)
        assert stats["fleet_total"] == 3, f"fleet_total expected 3, got {stats['fleet_total']}"
        assert stats["by_card"] == {"t_synthetic_a": 1, "t_synthetic_b": 1, "t_synthetic_c": 1}, stats["by_card"]
        assert stats["by_run"] == {"90001": 1, "90002": 1, "90003": 1}, stats["by_run"]
        # by_day: both events fall on the same UTC day for their ts range
        assert stats["by_day"], stats["by_day"]
        assert sum(stats["by_day"].values()) == 3, stats["by_day"]

        # 5) Query with filters
        filt = _query(events, board="jarvis-os")
        assert filt["fleet_total"] == 2, f"jarvis-os filter expected 2, got {filt['fleet_total']}"
        filt2 = _query(events, card="t_synthetic_b")
        assert filt2["fleet_total"] == 1, f"card filter expected 1, got {filt2['fleet_total']}"

        print(json.dumps({
            "test": "synthetic protocol_violation injection acceptance",
            "result": "PASS",
            "fleet_total": stats["fleet_total"],
            "dedupe_reinject": "held",
            "batch_dedupe": {"appended": n4, "duplicates": d4},
            "by_card": stats["by_card"],
            "by_run": stats["by_run"],
            "by_day": stats["by_day"],
            "artifact": str(artifact),
        }, sort_keys=True))
        return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="protocol_violation_artifact")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_consume = sub.add_parser("consume", help="read protocol_violation events (stdin or --file) and append new ones")
    p_consume.add_argument("--file", help="path to a JSONL file of events; default stdin")
    p_consume.set_defaults(func=cmd_consume)

    p_inject = sub.add_parser("inject", help="inject a synthetic protocol_violation event (test channel)")
    p_inject.add_argument("--card-id", required=True)
    p_inject.add_argument("--run-id", type=int, required=True)
    p_inject.add_argument("--ts", type=int, required=True)
    p_inject.add_argument("--board", default=None)
    p_inject.add_argument("--exit-code", type=int, default=0)
    p_inject.add_argument("--missing-terminal-call", default=None)
    p_inject.add_argument("--synthetic", action="store_true", help="mark event synthetic: true")
    p_inject.set_defaults(func=cmd_inject)

    p_query = sub.add_parser("query", help="query the fleet-wide artifact")
    p_query.add_argument("--json", action="store_true", help="machine-readable output")
    p_query.add_argument("--raw", action="store_true", help="dump raw artifact lines (evidence)")
    p_query.add_argument("--since", type=int, default=None, help="only events with ts >= since (epoch)")
    p_query.add_argument("--card", default=None, help="filter by card id")
    p_query.add_argument("--board", default=None, help="filter by board")
    p_query.set_defaults(func=cmd_query)

    p_test = sub.add_parser("test", help="run synthetic injection acceptance test (isolated)")
    p_test.set_defaults(func=cmd_test)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except AssertionError as exc:
        print(f"[protocol_violation_artifact] TEST FAILED: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[protocol_violation_artifact] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
