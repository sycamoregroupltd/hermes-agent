#!/usr/bin/env python3
"""Hermes pre-tool gate for canonical second-brain writes.

The gate is deliberately narrow. It inspects only mutations aimed at the two
canonical DGX vaults. Read-only commands and all work outside those vaults are
allowed. On ambiguity it fails open; when an invalid Markdown envelope or a
case-colliding destination is provable before execution, it blocks with a
repairable reason.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml


FLEET_ROOT = Path("/home/frank/obsidian-fleet-vault")
SYCODE_ROOT = Path("/home/frank/obsidian/quant-team")
SYCODE_ALIAS = Path("/home/frank/obsidian/sycode-trading")
LOG_PATH = Path("/home/frank/.hermes/logs/second-brain-write-gate.log")

REQUIRED_FIELDS = (
    "title",
    "type",
    "status",
    "created",
    "updated",
    "confidence",
    "tags",
    "sources",
)
CANONICAL_TYPES = {
    "project",
    "agent",
    "skill",
    "entity",
    "concept",
    "decision",
    "research",
    "source",
    "query",
    "comparison",
    "runbook",
    "incident",
    "task-evidence",
    "moc",
    "template",
}
CANONICAL_STATUSES = {
    "draft",
    "active",
    "review",
    "blocked",
    "contested",
    "superseded",
    "archived",
}
CANONICAL_CONFIDENCES = {"high", "medium", "low", "unknown"}
EXCLUDED_PARTS = {
    ".git",
    ".obsidian",
    ".tmp",
    ".trash",
    "node_modules",
    "artifacts",
    "sessions",
    "activity",
    "backups",
    ".backups",
    "raw",
    "_attic",
    "_archive",
    ".archive",
    "archive",
    "archives",
}
SECRET_PATTERNS = {
    "openai-key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "github-token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
}
APPROVAL_PAYLOAD_PATTERNS = {
    "approval-payload-field": re.compile(
        r"(?im)^[ \t]*(?:approval_payload|approval_phrase|exact_approval_phrase)[ \t]*:"
    ),
    "replayable-approval-command": re.compile(
        r"(?i)\bapprove[ \t]+(?:draft-|approval-|packet-)[A-Za-z0-9][A-Za-z0-9._:-]{12,}\b"
    ),
}
FRONTMATTER_LINE = re.compile(
    r"^(?:---|title:|type:|status:|created:|updated:|confidence:|tags:|sources:)",
    re.I,
)
MUTATING_COMMAND = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:tee|touch|mkdir|rm|mv|cp|install|truncate|sed\s+-i|perl\s+-i|python\d*\s+-c)\b|>>?|<<",
    re.I,
)
TRUSTED_COMMAND_MARKERS = (
    "/home/frank/.hermes/scripts/second_brain_",
    "/home/frank/.hermes/scripts/audit_second_brain.py",
    "/home/frank/.hermes/scripts/generate_knowledge_catalogs.py",
    "/home/frank/.hermes/scripts/migrate_second_brain_metadata.py",
    "/home/frank/.hermes/scripts/repair_second_brain_links.py",
    "/home/frank/.hermes/scripts/mirror_uaa_rules_to_vault.py",
    "/Orchestration/sessions/bin/session-bus.sh",
    "/Orchestration/sessions/bin/session-heartbeat.py",
)


class GateViolation(ValueError):
    """A deterministic, repairable write-policy violation."""


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def allow() -> None:
    emit({})


def block(reason: str, *, tool: str, path: str | None = None, profile: str = "?") -> None:
    message = f"Second-brain write gate: {reason}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(
                f"{dt.datetime.now(dt.timezone.utc).isoformat()} BLOCK "
                f"profile={profile} tool={tool or '?'} path={path or '-'} reason={reason[:500]}\n"
            )
    except OSError:
        pass
    emit({"decision": "block", "action": "block", "reason": message, "message": message})


def resolve_path(raw: str, cwd: str | None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    return path.resolve(strict=False)


def owning_root(path: Path, roots: Iterable[Path] = (FLEET_ROOT, SYCODE_ROOT)) -> Path | None:
    resolved = path.resolve(strict=False)
    for root in roots:
        candidate = root.resolve(strict=False)
        try:
            resolved.relative_to(candidate)
            return candidate
        except ValueError:
            continue
    return None


def iso_date(value: Any) -> bool:
    if isinstance(value, (dt.date, dt.datetime)):
        return True
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise GateViolation("active Markdown must begin with a closed YAML frontmatter block")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise GateViolation("frontmatter is unterminated; add the closing `---` delimiter before the body")
    try:
        value = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise GateViolation(f"frontmatter YAML is invalid: {str(exc).splitlines()[0]}") from exc
    if not isinstance(value, dict):
        raise GateViolation("frontmatter must be a YAML mapping")
    return value, text[end + 5 :]


def validate_markdown(text: str) -> None:
    frontmatter, _body = split_frontmatter(text)
    missing = [key for key in REQUIRED_FIELDS if key not in frontmatter]
    if missing:
        raise GateViolation(f"missing required properties: {', '.join(missing)}")
    title = frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        raise GateViolation("`title` must be a non-empty string")
    if frontmatter.get("type") not in CANONICAL_TYPES:
        raise GateViolation(
            f"noncanonical `type: {frontmatter.get('type')}`; use one of {sorted(CANONICAL_TYPES)}"
        )
    if frontmatter.get("status") not in CANONICAL_STATUSES:
        raise GateViolation(
            f"noncanonical `status: {frontmatter.get('status')}`; use one of {sorted(CANONICAL_STATUSES)}"
        )
    if frontmatter.get("confidence") not in CANONICAL_CONFIDENCES:
        raise GateViolation(
            "noncanonical `confidence: "
            f"{frontmatter.get('confidence')}`; use one of {sorted(CANONICAL_CONFIDENCES)}"
        )
    for key in ("created", "updated"):
        if not iso_date(frontmatter.get(key)):
            raise GateViolation(f"`{key}` must be a valid YYYY-MM-DD date")
    for key in ("tags", "sources"):
        if not isinstance(frontmatter.get(key), list):
            raise GateViolation(f"`{key}` must be a YAML list (an empty list is valid)")
    for kind, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            raise GateViolation(f"secret-like material detected ({kind}); store a safe reference, never the value")
    for kind, pattern in APPROVAL_PAYLOAD_PATTERNS.items():
        if pattern.search(text):
            raise GateViolation(
                f"reusable approval payload detected ({kind}); keep the exact phrase and authenticated evidence "
                "on the owning tracker, and store only a sanitized disposition plus tracker link in the vault"
            )


def case_collision_error(path: Path, root: Path) -> str | None:
    """Return an error when any destination component differs only by case."""
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return None
    parent = root.resolve(strict=False)
    for part in relative.parts:
        if parent.is_dir():
            try:
                variants = [child.name for child in parent.iterdir() if child.name.lower() == part.lower()]
            except OSError:
                return None
            exact = [name for name in variants if name == part]
            canonical_override = None
            if parent == SYCODE_ROOT.resolve(strict=False) and part.lower() == "reviews":
                canonical_override = "Reviews"
            if canonical_override and part != canonical_override:
                return (
                    f"destination casing collides with canonical `{canonical_override}`; "
                    f"use `{canonical_override}` exactly (top-level Sycode reviews never use `reviews/`)"
                )
            if len(set(variants)) > 1 and canonical_override is None:
                return (
                    f"parent already contains unresolved case-colliding variants {sorted(set(variants))}; "
                    "resolve the namespace before writing another note"
                )
            if variants and not exact:
                return (
                    f"destination casing collides with existing `{variants[0]}`; "
                    f"use `{variants[0]}` exactly (for Sycode top-level reviews, use `Reviews/`, not `reviews/`)"
                )
        parent = parent / part
    return None


def validate_destination(path: Path, root: Path, content: str | None = None) -> None:
    collision = case_collision_error(path, root)
    if collision:
        raise GateViolation(collision)
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return
    if path.suffix.lower() != ".md" or any(part in EXCLUDED_PARTS for part in relative.parts):
        return
    if content is not None:
        validate_markdown(content)


def patch_targets(patch_text: str, cwd: str | None) -> list[tuple[str, Path, Path | None]]:
    targets: list[tuple[str, Path, Path | None]] = []
    header = re.compile(r"^\*\*\*\s*(Update|Add|Delete)\s+File:\s*(.+?)\s*$", re.M)
    move = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+?)\s*$", re.M)
    for match in header.finditer(patch_text):
        targets.append((match.group(1).lower(), resolve_path(match.group(2), cwd), None))
    for match in move.finditer(patch_text):
        targets.append(("move", resolve_path(match.group(1), cwd), resolve_path(match.group(2), cwd)))
    return targets


def patch_touches_frontmatter(patch_text: str) -> bool:
    for line in patch_text.splitlines():
        if not line.startswith(("+", "-", " ")) or line.startswith(("+++", "---")):
            continue
        if FRONTMATTER_LINE.match(line[1:].lstrip()):
            return True
    return False


def handle_write(tool_input: dict[str, Any], cwd: str | None) -> tuple[str | None, str | None]:
    raw = tool_input.get("path")
    content = tool_input.get("content")
    if not isinstance(raw, str) or not isinstance(content, str):
        return None, None
    path = resolve_path(raw, cwd)
    root = owning_root(path)
    if root is None:
        return None, None
    validate_destination(path, root, content)
    return str(path), None


def handle_replace(tool_input: dict[str, Any], cwd: str | None) -> tuple[str | None, str | None]:
    raw = tool_input.get("path")
    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not all(isinstance(value, str) for value in (raw, old, new)):
        return None, None
    path = resolve_path(raw, cwd)
    root = owning_root(path)
    if root is None:
        return None, None
    validate_destination(path, root)
    if path.suffix.lower() != ".md" or not path.is_file():
        return str(path), None
    current = path.read_text(encoding="utf-8", errors="replace")
    if old not in current:
        return str(path), None  # the real patch tool will reject it without writing
    if tool_input.get("replace_all"):
        candidate = current.replace(old, new)
    else:
        candidate = current.replace(old, new, 1)
    validate_destination(path, root, candidate)
    return str(path), None


def handle_v4a(tool_input: dict[str, Any], cwd: str | None) -> tuple[str | None, str | None]:
    patch_text = tool_input.get("patch")
    if not isinstance(patch_text, str):
        return None, None
    touched: list[str] = []
    for operation, source, destination in patch_targets(patch_text, cwd):
        candidate_path = destination if operation == "move" and destination else source
        root = owning_root(candidate_path)
        if root is None:
            continue
        touched.append(str(candidate_path))
        validate_destination(candidate_path, root)
        if operation == "add" and candidate_path.suffix.lower() == ".md":
            raise GateViolation("create vault Markdown with `write_file` so the complete canonical envelope can be validated")
        if operation == "update" and candidate_path.suffix.lower() == ".md":
            if not source.is_file():
                continue
            current = source.read_text(encoding="utf-8", errors="replace")
            validate_destination(source, owning_root(source) or root, current)
            if patch_touches_frontmatter(patch_text):
                raise GateViolation(
                    "V4A patch touches frontmatter; rewrite the complete note with `write_file` or use replace mode so the final envelope can be validated"
                )
    return (", ".join(touched) if touched else None), None


def handle_terminal(tool_input: dict[str, Any], cwd: str | None) -> tuple[str | None, str | None]:
    command = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("script")
    if isinstance(command, list):
        command = " ".join(str(item) for item in command)
    if not isinstance(command, str) or not MUTATING_COMMAND.search(command):
        return None, None
    if any(marker in command for marker in TRUSTED_COMMAND_MARKERS):
        return None, None
    cwd_path = resolve_path(cwd or os.getcwd(), None)
    cwd_root = owning_root(cwd_path)
    references_vault = cwd_root is not None or any(
        marker in command
        for marker in (
            str(FLEET_ROOT),
            str(SYCODE_ROOT),
            str(SYCODE_ALIAS),
            "/home/frank/obsidian-fleet-vault",
            "/home/frank/obsidian/quant-team",
            "/home/frank/obsidian/sycode-trading",
        )
    )
    if references_vault:
        raise GateViolation(
            "direct shell mutation of a canonical vault is blocked; use `write_file`/`patch` for interactive edits or a reviewed canonical writer/migration script"
        )
    return None, None


def process(payload: dict[str, Any]) -> dict[str, Any]:
    tool = str(payload.get("tool_name") or "").strip()
    tool_input = payload.get("tool_input") or payload.get("args") or {}
    if not isinstance(tool_input, dict):
        return {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    profile = str(extra.get("profile") or payload.get("profile") or os.environ.get("HERMES_PROFILE") or "?")
    try:
        path: str | None = None
        if tool == "write_file":
            path, _ = handle_write(tool_input, cwd)
        elif tool == "patch":
            if str(tool_input.get("mode") or "replace") == "patch":
                path, _ = handle_v4a(tool_input, cwd)
            else:
                path, _ = handle_replace(tool_input, cwd)
        elif tool in {"terminal", "bash", "shell"}:
            path, _ = handle_terminal(tool_input, cwd)
        return {"_path": path} if path else {}
    except GateViolation as exc:
        return {
            "decision": "block",
            "action": "block",
            "reason": f"Second-brain write gate: {exc}",
            "message": f"Second-brain write gate: {exc}",
            "_profile": profile,
        }
    except Exception:
        return {}  # fail open on parser, filesystem, or environment ambiguity


def self_test() -> None:
    valid = """---
title: "Gate test"
type: task-evidence
status: active
created: 2026-07-13
updated: 2026-07-13
confidence: high
tags: [test]
sources: [fixture]
---
# Gate test
"""
    validate_markdown(valid)
    cases = {
        "missing": valid.replace("updated: 2026-07-13\n", ""),
        "enum": valid.replace("confidence: high", "confidence: evidence"),
        "open": valid.replace("\n---\n# Gate", "\n# Gate"),
        "secret": valid + "sk-" + "A" * 30,
        "approval": valid + "Approve draft-fixture-packet-20260715T211104Z\n",
    }
    for name, content in cases.items():
        try:
            validate_markdown(content)
        except GateViolation:
            continue
        raise AssertionError(f"{name} fixture was not blocked")
    with tempfile.TemporaryDirectory(prefix="second-brain-write-gate-") as directory:
        root = Path(directory)
        (root / "Reviews").mkdir()
        collision = case_collision_error(root / "reviews" / "note.md", root)
        if not collision or "Reviews" not in collision:
            raise AssertionError("case-collision fixture did not block")
        note = root / "Reviews" / "note.md"
        note.write_text(valid, encoding="utf-8")
        validate_destination(note, root, valid)
    body_patch = "*** Begin Patch\n*** Update File: note.md\n@@\n-Old body\n+New body\n*** End Patch"
    fm_patch = "*** Begin Patch\n*** Update File: note.md\n@@\n-confidence: high\n+confidence: evidence\n*** End Patch"
    if patch_touches_frontmatter(body_patch):
        raise AssertionError("body-only patch was misclassified")
    if not patch_touches_frontmatter(fm_patch):
        raise AssertionError("frontmatter patch was not detected")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print(json.dumps({"status": "pass", "fixtures": 9}))
        return 0
    if os.environ.get("ALLOW_SECOND_BRAIN_WRITE") == "1":
        allow()
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
        return 0
    if not isinstance(payload, dict):
        allow()
        return 0
    result = process(payload)
    path = result.pop("_path", None)
    profile = result.pop("_profile", "?")
    if result:
        block(result["reason"].removeprefix("Second-brain write gate: "), tool=str(payload.get("tool_name") or ""), path=path, profile=profile)
    else:
        allow()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
