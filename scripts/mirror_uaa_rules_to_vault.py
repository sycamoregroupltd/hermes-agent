#!/usr/bin/env python3
"""Mirror approved UAA rule Markdown into the fleet vault canonically."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from second_brain_writer import render_markdown, write_markdown_atomic


DEFAULT_SOURCE = Path("/home/frank/uaa-rules")
DEFAULT_DESTINATION = Path("/home/frank/obsidian-fleet-vault/Orchestration/uaa-rules-mirror")
SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api|secret|private)_key\s*=\s*\S{12,}"),
)
CANONICAL_KEYS = {"title", "type", "status", "created", "updated", "confidence", "tags", "sources"}
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\[\]]+?)\]\]")


def split(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}, text
    raw, body = text[4:].split("\n---\n", 1)
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}, body
    return (value if isinstance(value, dict) else {}), body


def iso_date(value: Any, fallback: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else fallback


def title(meta: dict[str, Any], body: str, source: Path) -> str:
    if meta.get("title"):
        return str(meta["title"])
    match = re.search(r"(?m)^#\s+(.+)$", body)
    return match.group(1).strip() if match else re.sub(r"[-_]", " ", source.stem).title()


def safe_extra_properties(meta: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in meta.items():
        if key in CANONICAL_KEYS or not KEY_RE.fullmatch(str(key)):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            result[str(key)] = value
        elif isinstance(value, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in value):
            result[str(key)] = value
    return result


def source_candidates(root: Path) -> list[Path]:
    paths = list((root / "known-fixes").glob("*.md"))
    for pattern in ("*classification*.md", "*registry*.md", "HERMES-NATIVE-DECISIONS.md"):
        paths.extend(root.glob(pattern))
    return sorted(set(path for path in paths if path.is_file()))


def mirror_target(source: Path, destination: Path, source_root: Path) -> Path:
    """Give generated sources a stable, collision-proof Obsidian basename."""
    relative = source.relative_to(source_root).with_suffix("")
    slug = "--".join(relative.parts)
    return destination / f"uaa-rule--{slug}.md"


def mirror_link_map(sources: list[Path], destination: Path, source_root: Path) -> dict[str, str]:
    candidates: dict[str, list[str]] = {}
    for source in sources:
        relative = source.relative_to(source_root).with_suffix("").as_posix()
        target = "Orchestration/uaa-rules-mirror/" + mirror_target(source, destination, source_root).stem
        for key in (relative, Path(relative).name):
            candidates.setdefault(key.lower(), []).append(target)
    return {key: values[0] for key, values in candidates.items() if len(set(values)) == 1}


def rewrite_mirror_links(body: str, links: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        payload = match.group(1)
        raw_target, separator, alias = payload.partition("|")
        path_part, anchor_separator, anchor = raw_target.partition("#")
        normalized = path_part.removesuffix(".md").lstrip("/").lower()
        target = links.get(normalized) or links.get(Path(normalized).name)
        if not target:
            return match.group(0)
        if anchor_separator:
            target += "#" + anchor
        label = alias if separator else raw_target
        return f"[[{target}|{label}]]"

    return WIKILINK_RE.sub(replace, body)


def mirror_one(source: Path, destination: Path, source_root: Path, links: dict[str, str]) -> bool:
    text = source.read_text(encoding="utf-8", errors="replace")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return False
    meta, body = split(text)
    body = rewrite_mirror_links(body, links)
    file_date = dt.datetime.fromtimestamp(source.stat().st_mtime, tz=dt.timezone.utc).date().isoformat()
    name_date = iso_date(source.name, file_date)
    tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
    properties: dict[str, Any] = {
        "title": title(meta, body, source),
        "type": "source",
        "status": "active",
        "created": iso_date(meta.get("created"), name_date),
        "updated": iso_date(meta.get("updated"), file_date),
        "confidence": "high",
        "tags": list(dict.fromkeys([*(str(item) for item in tags), "uaa-rule", "mirror"])),
        "sources": [str(source)],
        **safe_extra_properties(meta),
        "canonical_source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "generated": True,
        "generator": "mirror_uaa_rules_to_vault.py",
        "mirror_namespace": "uaa-rules",
    }
    rendered = render_markdown(body, properties)
    if destination.is_file() and destination.read_text(encoding="utf-8", errors="replace") == rendered:
        return False
    write_markdown_atomic(destination, body, **properties)
    return True


def render_index(destination: Path, mirrored: list[Path], source_root: Path) -> bool:
    rows = []
    for path in sorted(mirrored):
        relative = path.relative_to(destination).with_suffix("").as_posix()
        source_label = path.name.removeprefix("uaa-rule--").removesuffix(".md").replace("--", "/")
        rows.append(f"- [[Orchestration/uaa-rules-mirror/{relative}|{source_label}]]")
    body = "# UAA rules mirror\n\nAuto-managed canonical source representations. Edit the operational files under `/home/frank/uaa-rules`; never hand-edit this folder.\n\n## Rules\n\n" + "\n".join(rows)
    today = dt.datetime.fromtimestamp(max((path.stat().st_mtime for path in mirrored), default=0), tz=dt.timezone.utc).date().isoformat()
    if today == "1970-01-01":
        today = dt.date.today().isoformat()
    properties = {
        "title": "UAA Rules Mirror",
        "type": "moc",
        "status": "active",
        "created": "2026-06-26",
        "updated": today,
        "confidence": "high",
        "tags": ["uaa-rules", "mirror", "index"],
        "sources": [str(source_root)],
        "generated": True,
        "generator": "mirror_uaa_rules_to_vault.py",
    }
    target = destination / "_INDEX.md"
    rendered = render_markdown(body, properties)
    if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == rendered:
        return False
    write_markdown_atomic(target, body, **properties)
    return True


def run_mirror(source_root: Path, destination: Path) -> dict[str, Any]:
    if not source_root.is_dir():
        raise ValueError(f"source root is missing: {source_root}")
    destination.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    skipped: list[str] = []
    mirrored: list[Path] = []
    sources = source_candidates(source_root)
    links = mirror_link_map(sources, destination, source_root)
    for source in sources:
        target = mirror_target(source, destination, source_root)
        if any(pattern.search(source.read_text(encoding="utf-8", errors="replace")) for pattern in SECRET_PATTERNS):
            skipped.append(str(source))
            continue
        if mirror_one(source, target, source_root, links):
            changed.append(str(target))
        mirrored.append(target)
    expected = {path.resolve(strict=False) for path in mirrored}
    for stale in sorted(destination.rglob("*.md")):
        if stale.name == "_INDEX.md" or stale.resolve(strict=False) in expected:
            continue
        meta, _body = split(stale.read_text(encoding="utf-8", errors="replace"))
        if meta.get("generated") is True and meta.get("generator") == "mirror_uaa_rules_to_vault.py":
            stale.unlink()
            changed.append(str(stale))
    for directory in sorted((path for path in destination.rglob("*") if path.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    if render_index(destination, mirrored, source_root):
        changed.append(str(destination / "_INDEX.md"))
    return {"status": "pass", "sources": len(mirrored), "changed": changed, "secret_skipped": skipped}


def self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="uaa-rules-mirror-test-") as directory:
        root = Path(directory)
        source = root / "source"
        destination = root / "vault" / "Orchestration" / "uaa-rules-mirror"
        (source / "known-fixes").mkdir(parents=True)
        (source / "known-fixes" / "2026-07-13-rule.md").write_text("# Fixture Rule\n\nBody.\n", encoding="utf-8")
        (source / "approvals-registry.md").write_text("# Registry\n\nRelated: [[2026-07-13-rule]].\n", encoding="utf-8")
        first = run_mirror(source, destination)
        second = run_mirror(source, destination)
        notes = list(destination.rglob("*.md"))
        if len(notes) != 3 or len(first["changed"]) != 3 or second["changed"]:
            raise AssertionError("mirror idempotence failed")
        if not all("type: \"source\"" in path.read_text() for path in notes if path.name != "_INDEX.md"):
            raise AssertionError("mirrored source schema failed")
        if not all(path.name.startswith("uaa-rule--") for path in notes if path.name != "_INDEX.md"):
            raise AssertionError("mirror namespace failed")
        registry = destination / "uaa-rule--approvals-registry.md"
        if "[[Orchestration/uaa-rules-mirror/uaa-rule--known-fixes--2026-07-13-rule|2026-07-13-rule]]" not in registry.read_text():
            raise AssertionError("mirror link rewrite failed")
        if not all(not list(path.parent.glob(f".{path.name}.incoming-*")) for path in notes):
            raise AssertionError("atomic staging artifacts remain")
    return {"status": "pass", "sources": 2, "idempotent": True, "atomic": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    result = self_test() if args.self_test else run_mirror(args.source, args.destination)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
