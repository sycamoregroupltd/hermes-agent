#!/usr/bin/env python3
"""Fail-visible monitor for governed dirty files in the live Hermes checkout.

The monitor is intentionally read-only with respect to the checkout. It compares
``git status --porcelain`` and content hashes with a reviewed inventory. Unexpected
paths, hash changes, missing files, or a foreign HEAD are emitted as one stable
alert class so the host card helper can create/update one Kanban card.
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
from typing import Any

ALERT_KEY = "hermes-overlay-drift"
DEFAULT_REPO = Path("/home/frank/.hermes/hermes-agent")
DEFAULT_INVENTORY = Path("/home/frank/.hermes/deploy-state/ops-notes/overlays/hermes-live-overlay-inventory.json")
DEFAULT_CARD_HELPER = Path("/home/frank/.hermes/scripts/fleet-alert-card.sh")
DEFAULT_BUS_HELPER = Path("/home/frank/obsidian-fleet-vault/Orchestration/sessions/bin/session-bus.sh")
DEFAULT_REPORT = Path("/home/frank/.hermes/deploy-state/ops-notes/overlays/hermes-overlay-drift-latest.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed rc={proc.returncode}: {proc.stderr.strip()[:400]}")
    return proc.stdout


def status_paths(repo: Path) -> list[dict[str, str]]:
    raw = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
    )
    if raw.returncode:
        raise RuntimeError(f"git status failed rc={raw.returncode}: {raw.stderr.decode(errors='replace')[:400]}")
    parts = raw.stdout.split(b"\0")
    rows: list[dict[str, str]] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        index += 1
        if not token:
            continue
        if len(token) < 4:
            raise RuntimeError(f"malformed porcelain record: {token!r}")
        status = token[:2].decode(errors="replace")
        path = token[3:].decode(errors="surrogateescape")
        rows.append({"status": status, "path": path})
        if status[0] in "RC" or status[1] in "RC":
            if index >= len(parts) or not parts[index]:
                raise RuntimeError(f"rename/copy record missing source: {token!r}")
            rows.append({"status": status, "path": parts[index].decode(errors="surrogateescape")})
            index += 1
    return rows


def load_inventory(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise RuntimeError(f"inventory unreadable: {path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise RuntimeError("inventory must be an object with an entries list")
    for entry in data["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RuntimeError("inventory entry missing path")
        rel = Path(entry["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(f"inventory path is not repo-relative: {entry['path']!r}")
    return data


def make_report(repo: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    allowed = {entry["path"]: entry for entry in inventory["entries"]}
    rows = status_paths(repo)
    changed = {row["path"] for row in rows}
    unexpected = sorted(changed - set(allowed))
    missing: list[str] = []
    hash_drift: list[dict[str, str]] = []
    for rel, entry in sorted(allowed.items()):
        target = repo / rel
        if not target.exists():
            missing.append(rel)
            continue
        expected = entry.get("live_sha256")
        if expected and sha256(target) != expected:
            hash_drift.append({"path": rel, "expected": expected, "actual": sha256(target)})
    head = git(repo, "rev-parse", "HEAD").strip()
    expected_head = inventory.get("expected_head")
    head_drift = bool(expected_head and head != expected_head)
    unowned = sorted(
        entry["path"] for entry in inventory["entries"]
        if entry.get("kind") in {"unowned-dirty", "quarantined"}
    )
    return {
        "repo": str(repo),
        "expected_head": expected_head,
        "actual_head": head,
        "status": rows,
        "unexpected_paths": unexpected,
        "missing_inventory_files": missing,
        "hash_drift": hash_drift,
        "head_drift": head_drift,
        "unowned_inventory_entries": unowned,
        "clean": not (unexpected or missing or hash_drift or head_drift or unowned),
    }


def write_report(path: Path, *, status: str, report: dict[str, Any] | None = None, error: str | None = None) -> None:
    payload: dict[str, Any] = {
        "schema": "hermes-governed-overlay-drift/v1",
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
    }
    if report is not None:
        payload["report"] = report
    if error is not None:
        payload["error"] = error
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def emit_alert(report: dict[str, Any], args: argparse.Namespace) -> None:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"))
    print(f"OVERLAY-DRIFT {payload}")
    if args.alert_card:
        subprocess.run(
            [str(args.alert_card), ALERT_KEY, "Hermes governed overlay drift", payload],
            stdin=subprocess.DEVNULL,
            check=False,
        )
    if args.bus_session:
        text = f"ALERT {ALERT_KEY}: {payload}"
        subprocess.run(
            [str(args.bus_helper), "event", "--author", args.bus_session, "--text", text],
            stdin=subprocess.DEVNULL,
            check=False,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--alert-card", type=Path, default=None)
    parser.add_argument("--bus-session", default=None)
    parser.add_argument("--bus-helper", type=Path, default=DEFAULT_BUS_HELPER)
    parser.add_argument("--report-file", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    try:
        report = make_report(args.repo, load_inventory(args.inventory))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            write_report(args.report_file, status="error", error=error)
        except Exception as artifact_exc:
            print(f"OVERLAY-MONITOR-ERROR {error}; report-write={type(artifact_exc).__name__}: {artifact_exc}", file=sys.stderr)
            return 3
        print(f"OVERLAY-MONITOR-ERROR {error}", file=sys.stderr)
        return 3
    if report["clean"]:
        write_report(args.report_file, status="clean", report=report)
        print(f"OVERLAY-CLEAN head={report['actual_head']} paths={len(report['status'])}")
        return 0
    write_report(args.report_file, status="drift", report=report)
    emit_alert(report, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
