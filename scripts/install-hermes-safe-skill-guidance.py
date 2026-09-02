#!/usr/bin/env python3
"""Install the reviewed safe skill-smoke guidance into both consumers.

This installer is intentionally separate from the smoke wrapper.  It can be
run against a fixture root for review and requires exact source anchors before
it changes a live skill, so drift fails closed instead of being overwritten.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

MARKER = "SAFE_SKILL_SMOKE_WRAPPER"
RAW_NESTED_SMOKE = "hermes --accept-hooks --skills"

GAP_SMOKE_START = "## 8. Smoke-test pattern\n"
GAP_SMOKE_END = "\nFor mechanism fixtures"
GAP_SMOKE_REPLACEMENT = """## 8. Smoke-test pattern

<!-- SAFE_SKILL_SMOKE_WRAPPER: do not replace with an inline nested Hermes command. -->

For loadability or provider smoke probes, use the reviewed wrapper from a
non-worker shell:

```bash
/home/frank/.hermes/hermes-agent/scripts/hermes-safe-skill-smoke.sh <skill-name>
```

The wrapper refuses inherited Kanban, session, delegated-child, and supervisor
context before launching Hermes. It passes `--toolsets \"\"` and a harmless
no-tools prompt only after that fail-closed check. Never run an inline nested
Hermes command from a Kanban worker.

For mechanism fixtures"""

SECTOR_ANCHOR = (
    "Do not create/resume a cron, change permissions/branch protection, deploy, "
    "mutate live data, or spawn a dynamic workforce from this skill.\n"
)
SECTOR_SECTION = """

## Safe skill-load verification

<!-- SAFE_SKILL_SMOKE_WRAPPER: use this wrapper only; never inline a nested Hermes. -->

A SECTOR/controller skill-load check is a read-only preload check, not a loop
run. From a non-worker shell, use:

```bash
/home/frank/.hermes/hermes-agent/scripts/hermes-safe-skill-smoke.sh sector-development-codebase-loop
```

The wrapper fails closed with exit 78 if any `HERMES_KANBAN_*`,
`HERMES_SESSION_*`, delegated-child, or supervisor context is inherited. Do not
use `-z`, a nested `--profile`, or a hand-built dispatcher-shaped command from
inside a worker. Separately verify the controller and ledger through their
registered board/runtime owners, read-only, without targeting a live worker card.
"""


def _targets(skills_root: Path) -> tuple[Path, Path]:
    root = skills_root.resolve()
    return (
        root / "devops" / "gap-plugging" / "SKILL.md",
        root / "devops" / "sector-development-codebase-loop" / "SKILL.md",
    )


def _is_safe(content: str) -> bool:
    return MARKER in content and "hermes-safe-skill-smoke.sh" in content and RAW_NESTED_SMOKE not in content


def _render_gap(content: str) -> str:
    start = content.find(GAP_SMOKE_START)
    if start < 0:
        raise ValueError("gap-plugging smoke section anchor not found")
    end = content.find(GAP_SMOKE_END, start)
    if end < 0:
        raise ValueError("gap-plugging mechanism-fixture anchor not found")
    return content[:start] + GAP_SMOKE_REPLACEMENT + content[end + len(GAP_SMOKE_END) :]


def _render_sector(content: str) -> str:
    if MARKER in content:
        return content
    if content.count(SECTOR_ANCHOR) != 1:
        raise ValueError("sector activation-boundary anchor is missing or ambiguous")
    return content.replace(SECTOR_ANCHOR, SECTOR_ANCHOR + SECTOR_SECTION, 1)


def _backup_and_replace(path: Path, content: str, stamp: str) -> Path:
    backup = path.with_name(f"{path.name}.bak-safe-skill-smoke-{stamp}")
    shutil.copy2(path, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=Path("/home/frank/.hermes/skills"),
        help="skills root containing devops consumer skills",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify both consumers are already wrapper-only without changing files",
    )
    args = parser.parse_args(argv)
    gap, sector = _targets(args.skills_root)

    try:
        contents = {path: path.read_text(encoding="utf-8") for path in (gap, sector)}
    except OSError as exc:
        print(f"safe skill guidance: cannot read target: {exc}", file=sys.stderr)
        return 2

    unsafe = [str(path) for path, content in contents.items() if not _is_safe(content)]
    if args.check:
        if unsafe:
            print("safe skill guidance: NOT_INSTALLED: " + ", ".join(unsafe), file=sys.stderr)
            return 1
        print("safe skill guidance: CHECK_PASS")
        return 0

    if not unsafe:
        print("safe skill guidance: already installed")
        return 0

    try:
        rendered_gap = _render_gap(contents[gap])
        rendered_sector = _render_sector(contents[sector])
        rendered = {gap: rendered_gap, sector: rendered_sector}
        if any(not _is_safe(content) for content in rendered.values()):
            raise ValueError("rendered guidance did not satisfy wrapper-only invariant")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backups = [_backup_and_replace(path, rendered[path], stamp) for path in (gap, sector)]
    except (OSError, ValueError) as exc:
        print(f"safe skill guidance: refusing partial install: {exc}", file=sys.stderr)
        return 2

    print("safe skill guidance: installed wrapper-only guidance")
    for path, backup in zip((gap, sector), backups):
        print(f"updated={path} backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
