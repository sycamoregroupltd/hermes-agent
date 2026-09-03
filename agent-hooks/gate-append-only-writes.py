#!/usr/bin/env python3
"""Fail-open pre_tool_call veto for append-only arena journals."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import sys

from hermes_constants import get_hermes_home

LOG = get_hermes_home() / "logs" / "append-only-write-gate.log"
REDIRECT = re.compile(r"(?:^|[;&|]\s*)[^;&|]*?(?P<op>>>?|\btee\b)(?:\s+-a)?\s+(?P<path>[^;&|]+)")
TRUNCATE = re.compile(r"\b(?:truncate|sed\s+-i|perl\s+-i)\b")


def emit(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":")))


def allow() -> None:
    emit({})


def block(reason: str, target: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} BLOCK target={target} reason={reason}\n")
    except Exception:
        pass
    emit({"decision": "block", "action": "block", "reason": reason, "message": reason})


def resolved(path: str) -> Path:
    # Strip/unquote redirect targets
    path = path.strip()
    if path.startswith(("'", '"')) and path.endswith(path[0]):
        path = path[1:-1]
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise


def append_only(path: str) -> bool:
    try:
        p = resolved(path)
    except (OSError, RuntimeError, ValueError):
        return False
    parts = p.parts
    if p.name == "IMPROVEMENTS.md" and p.parent.name == "trading-arena" and "trading-arena" in parts:
        return True
    return p.name == "journal.md" and "trading-arena" in parts


def strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return []


def tool_data(payload: dict) -> tuple[str, dict, str]:
    tool = str(payload.get("tool_name") or "").strip()
    args = payload.get("tool_input") or payload.get("args") or {}
    if not isinstance(args, dict):
        return tool, {}, ""
    command = args.get("command") or args.get("cmd") or args.get("script") or ""
    if isinstance(command, list):
        command = " ".join(map(str, command))
    return tool, args, str(command)


def patch_preserves(path: Path, args: dict) -> bool:
    current = path.read_text(encoding="utf-8")
    old = args.get("old_string", args.get("old_str"))
    new = args.get("new_string", args.get("new_str"))
    replace_all = args.get("replace_all", False)
    
    if isinstance(old, str) and isinstance(new, str):
        if old not in current:
            return False
        # Honor replace_all cardinality
        count = -1 if replace_all else 1
        candidate = current.replace(old, new, count)
        if len(candidate) < len(current):
            return False
        body = current.split("\n---\n", 1)[-1] if "\n---\n" in current else current
        return all(line in candidate for line in body.splitlines() if line.strip())
    
    patch = args.get("patch")
    if isinstance(patch, str):
        # Parse V4A headers properly - each on its own line
        for line in patch.splitlines():
            line = line.strip()
            if line.startswith("*** ") and str(path) in line:
                if any(header in line for header in ["*** Delete File:", "*** Move File:"]):
                    return False
                if "*** Update File:" in line:
                    # Check if patch removes lines (not counting context marker lines)
                    return not re.search(r"^-(?![-]{2,3}\s)", patch, re.M)
        return True
    return False


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return allow()
        tool, args, command = tool_data(payload)
        path = str(args.get("path") or args.get("file_path") or args.get("filename") or "")
        targets = [(path, resolved(path))] if path else []
        if not path and isinstance(args.get("patch"), str):
            targets += [(s, resolved(s)) for s in strings(args.get("patch")) if append_only(s)]
        for raw, target in targets:
            if not append_only(raw):
                continue
            if tool == "write_file":
                if target.is_file():
                    return block("append-only path refuses write_file full replace; use patch to append and also write journal-<taskid>.md", raw)
                continue
            if tool in {"patch", "patch_file", "apply_patch"} or "old_string" in args or "patch" in args:
                if target.is_file() and not patch_preserves(target, args):
                    return block("append-only path patch would drop or shrink journal history; use a preserving append patch", raw)
                continue
            if tool == "terminal" or command:
                if ">>" in command and not TRUNCATE.search(command):
                    continue
                if TRUNCATE.search(command) or re.search(r"(?:^|\s)>\s*", command) or re.search(r"\btee\b(?!\s+-a)", command):
                    return block("append-only path terminal rewrite is blocked; use >> or tee -a", raw)
        if tool == "terminal" and command:
            # Inspect each redirect operator separately
            for match in REDIRECT.finditer(command):
                op = match.group("op")
                raw = match.group("path")
                # Strip/unquote before append_only check
                if append_only(raw):
                    # Sibling >> must not allow >
                    if op == ">":
                        return block("append-only path terminal rewrite is blocked; use >> or tee -a", raw)
                    if TRUNCATE.search(command):
                        return block("append-only path terminal rewrite is blocked; use >> or tee -a", raw)
        allow()
    except Exception:
        allow()


if __name__ == "__main__":
    main()
