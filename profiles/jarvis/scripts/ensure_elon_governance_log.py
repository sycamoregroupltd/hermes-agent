#!/usr/bin/env python3
"""Fail-closed initializer for the daily Elon governance cycle log."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import tempfile
from pathlib import Path

import yaml

from second_brain_writer import (
    CANONICAL_CONFIDENCE,
    CANONICAL_STATUSES,
    CANONICAL_TYPES,
    write_markdown_atomic,
)


JOB_ID = "e51c9e2fa5df"
DEFAULT_ROOT = Path("/home/frank/obsidian-fleet-vault/Governance")
REQUIRED = {"title", "type", "status", "created", "updated", "confidence", "tags", "sources"}


def properties(day: str) -> dict:
    return {
        "title": f"Elon governor cycle log — {day}",
        "type": "task-evidence",
        "status": "active",
        "created": day,
        "updated": day,
        "confidence": "high",
        "tags": ["governance", "elon", "generated"],
        "sources": [f"cron:{JOB_ID}"],
        "source_job_id": JOB_ID,
        "generated": True,
        "generator": "ensure_elon_governance_log.py",
    }


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RuntimeError("governance log has unterminated frontmatter")
    value = yaml.safe_load(text[4:end])
    if not isinstance(value, dict):
        raise RuntimeError("governance log frontmatter is not a mapping")
    return value, text[end + 5 :]


def validate_existing(value: dict, day: str) -> None:
    missing = sorted(REQUIRED - value.keys())
    if missing:
        raise RuntimeError(f"governance log is missing canonical properties: {', '.join(missing)}")
    if value.get("type") not in CANONICAL_TYPES:
        raise RuntimeError(f"governance log has noncanonical type: {value.get('type')}")
    if value.get("status") not in CANONICAL_STATUSES:
        raise RuntimeError(f"governance log has noncanonical status: {value.get('status')}")
    if value.get("confidence") not in CANONICAL_CONFIDENCE:
        raise RuntimeError(f"governance log has noncanonical confidence: {value.get('confidence')}")
    if str(value.get("created")) != day:
        raise RuntimeError(f"governance log created date does not match filename day: {value.get('created')}")


def ensure_log(path: Path, day: str) -> str:
    if not path.exists():
        write_markdown_atomic(path, f"# Elon governor cycle log — {day}\n", **properties(day))
        return "created"
    text = path.read_text(encoding="utf-8")
    value, body = split_frontmatter(text)
    if not value:
        write_markdown_atomic(path, body, **properties(day))
        return "repaired-missing-frontmatter"
    validate_existing(value, day)
    return "unchanged"


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="elon-governance-log-test-") as temporary:
        root = Path(temporary)
        day = "2026-07-13"
        new = root / f"{day}-new.md"
        assert ensure_log(new, day) == "created"
        assert ensure_log(new, day) == "unchanged"
        existing = root / f"{day}-existing.md"
        body = "# Existing body\n\n- preserved event\n"
        existing.write_text(body, encoding="utf-8")
        assert ensure_log(existing, day) == "repaired-missing-frontmatter"
        rendered = existing.read_text(encoding="utf-8")
        assert body.rstrip() in rendered and 'type: "task-evidence"' in rendered
        broken = root / f"{day}-broken.md"
        broken.write_text("---\ntitle: broken\n", encoding="utf-8")
        try:
            ensure_log(broken, day)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unterminated frontmatter did not fail closed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date", help="UTC date override for deterministic verification")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print(json.dumps({"status": "pass", "mode": "self-test"}))
        return 0
    day = args.date or dt.datetime.now(dt.timezone.utc).date().isoformat()
    path = args.root / f"{day}-elon-governance-cycle-log.md"
    action = ensure_log(path, day)
    print(json.dumps({"status": "ready", "action": action, "path": str(path), "required_append_contract": "preserve canonical frontmatter and append body only"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
