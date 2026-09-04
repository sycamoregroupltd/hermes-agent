#!/usr/bin/env python3
"""Canonical, atomic Markdown/JSON writer for scheduled second-brain producers."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


CANONICAL_TYPES = {
    "project", "agent", "skill", "entity", "concept", "decision",
    "research", "source", "query", "comparison", "runbook", "incident",
    "task-evidence", "moc", "template",
}
CANONICAL_STATUSES = {"draft", "active", "review", "blocked", "contested", "superseded", "archived"}
CANONICAL_CONFIDENCE = {"high", "medium", "low", "unknown"}
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _yaml_value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported frontmatter value: {type(value).__name__}")


def _validate_properties(properties: dict[str, Any]) -> None:
    required = {"title", "type", "status", "created", "updated", "confidence", "tags", "sources"}
    missing = sorted(required - properties.keys())
    if missing:
        raise ValueError(f"missing canonical properties: {', '.join(missing)}")
    if properties["type"] not in CANONICAL_TYPES:
        raise ValueError(f"invalid note type: {properties['type']}")
    if properties["status"] not in CANONICAL_STATUSES:
        raise ValueError(f"invalid note status: {properties['status']}")
    if properties["confidence"] not in CANONICAL_CONFIDENCE:
        raise ValueError(f"invalid confidence: {properties['confidence']}")
    for field in ("created", "updated"):
        if not isinstance(properties[field], str) or not DATE_RE.fullmatch(properties[field]):
            raise ValueError(f"{field} must be an ISO date")
    for field in ("tags", "sources"):
        if not isinstance(properties[field], (list, tuple)):
            raise ValueError(f"{field} must be a flat list")
    invalid_keys = sorted(key for key in properties if not KEY_RE.fullmatch(key))
    if invalid_keys:
        raise ValueError(f"invalid property keys: {', '.join(invalid_keys)}")


def render_markdown(body: str, properties: dict[str, Any]) -> str:
    """Return one canonical note, rejecting nested or duplicate frontmatter."""
    _validate_properties(properties)
    if body.lstrip().startswith("---"):
        raise ValueError("body already contains frontmatter")
    frontmatter = ["---"]
    frontmatter.extend(f"{key}: {_yaml_value(value)}" for key, value in properties.items())
    frontmatter.append("---")
    return "\n".join(frontmatter) + "\n" + body.lstrip("\n").rstrip() + "\n"


def write_text_atomic(path: str | Path, content: str, *, mode: int = 0o644) -> Path:
    """Fsync a same-filesystem candidate, rename it atomically, then fsync its directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.incoming-", dir=destination.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        staged.chmod(mode)
        os.replace(staged, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some network/virtual filesystems do not support directory fsync;
            # the atomic same-filesystem replacement has still completed.
            pass
        return destination
    finally:
        staged.unlink(missing_ok=True)


def write_markdown_atomic(path: str | Path, body: str, **properties: Any) -> Path:
    return write_text_atomic(path, render_markdown(body, properties))


def write_json_atomic(path: str | Path, payload: Any) -> Path:
    return write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_markdown_event(
    path: str | Path,
    event: str,
    *,
    initial_body: str,
    idempotency_key: str | None = None,
    **properties: Any,
) -> Path:
    """Serialize an append stream and replace the complete note atomically."""
    destination = Path(path)
    lock_name = hashlib.sha256(str(destination.resolve()).encode()).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"second-brain-writer-{lock_name}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if destination.exists():
                current = destination.read_text(encoding="utf-8")
                if not current.startswith("---\n"):
                    raise ValueError(f"refusing to append to noncanonical note: {destination}")
            else:
                current = render_markdown(initial_body, properties)
            if idempotency_key and idempotency_key in current:
                return destination
            combined = current.rstrip() + "\n\n" + event.strip() + "\n"
            return write_text_atomic(destination, combined)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="second-brain-writer-test-") as temporary:
        root = Path(temporary)
        note = root / "evidence.md"
        properties = {
            "title": "Writer self-test",
            "type": "task-evidence",
            "status": "active",
            "created": "2026-07-12",
            "updated": "2026-07-12",
            "confidence": "high",
            "tags": ["second-brain", "test"],
            "sources": ["source/path.md"],
            "project": "control-plane",
            "owners": ["jarvis"],
            "generated": True,
            "generator": "second_brain_writer.py",
        }
        write_markdown_atomic(note, "# Evidence\n\nBody", **properties)
        text = note.read_text(encoding="utf-8")
        assert text.startswith("---\n") and text.endswith("\n")
        assert 'type: "task-evidence"' in text
        assert "generated: true" in text
        assert not list(root.glob(".*.incoming-*"))
        write_json_atomic(root / "evidence.json", {"status": "pass"})
        assert json.loads((root / "evidence.json").read_text()) == {"status": "pass"}
        assert not list(root.glob(".*.incoming-*"))
        stream = root / "stream.md"
        append_markdown_event(stream, "## Event one", initial_body="# Stream", **properties)
        append_markdown_event(stream, "## Event two", initial_body="# Stream", **properties)
        append_markdown_event(
            stream,
            "## Event two duplicate <!-- idempotency-key:self-test-event-two -->",
            initial_body="# Stream",
            idempotency_key="idempotency-key:self-test-event-two",
            **properties,
        )
        append_markdown_event(
            stream,
            "## Event two duplicate <!-- idempotency-key:self-test-event-two -->",
            initial_body="# Stream",
            idempotency_key="idempotency-key:self-test-event-two",
            **properties,
        )
        stream_text = stream.read_text(encoding="utf-8")
        assert stream_text.count("---") == 2
        assert stream_text.count("## Event") == 3
        assert stream_text.count("idempotency-key:self-test-event-two") == 1
        assert not list(root.glob(".*.incoming-*"))
        try:
            render_markdown("---\ninvalid\n---", properties)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate-frontmatter guard did not fire")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.self_test:
        parser.error("this utility has no standalone write mode; pass --self-test")
    self_test()
    print(json.dumps({"status": "pass", "tested_at": dt.datetime.now(dt.timezone.utc).isoformat()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
