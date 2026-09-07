#!/usr/bin/env python3
"""Reject the known global Claude Git identity in pull-request commits.

Git author/committer fields are repository metadata, not execution-vendor
proof. This guard only catches the specific ambient identity that has caused
cross-vendor review misclassification; the review contract still requires a
provider receipt for maker/reviewer vendor attribution.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

_LEAKED_NAME = "claude"
_LEAKED_EMAIL = "claude@anthropic.com"


def _pull_request_range(event_path: Path | None) -> tuple[str, str]:
    """Return ``(base_sha, head_sha)`` from the GitHub event payload."""
    path = event_path or (
        Path(os.environ["GITHUB_EVENT_PATH"])
        if os.environ.get("GITHUB_EVENT_PATH")
        else None
    )
    if path is None:
        raise ValueError("GITHUB_EVENT_PATH or --event is required")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pull_request = payload["pull_request"]
        return (
            str(pull_request["base"]["sha"]),
            str(pull_request["head"]["sha"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read pull-request base/head from {path}: {exc}") from exc


def _commit_rows(base: str, head: str) -> Iterable[tuple[str, str, str]]:
    result = subprocess.run(
        ["git", "log", "--format=%H%x00%an%x00%ae", f"{base}..{head}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"git log failed for {base}..{head}: {detail}")
    for line in result.stdout.splitlines():
        fields = line.split("\x00")
        if len(fields) == 3:
            yield fields[0], fields[1], fields[2]


def leaked_authors(base: str, head: str) -> list[tuple[str, str, str]]:
    """Return commits authored with the inherited global Claude identity."""
    return [
        (sha, name, email)
        for sha, name, email in _commit_rows(base, head)
        if name.strip().casefold() == _LEAKED_NAME
        and email.strip().casefold() == _LEAKED_EMAIL
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="PR base commit SHA (for local use)")
    parser.add_argument("--head", help="PR head commit SHA (for local use)")
    parser.add_argument("--event", type=Path, help="GitHub event JSON path")
    args = parser.parse_args(argv)

    try:
        base, head = (
            (args.base, args.head)
            if bool(args.base) and bool(args.head)
            else _pull_request_range(args.event)
        )
        offenders = leaked_authors(base, head)
    except (ValueError, RuntimeError) as exc:
        print(f"git-author-provenance: ERROR: {exc}", file=sys.stderr)
        return 2

    if offenders:
        print(
            "git-author-provenance: FAIL — commit author "
            "Claude <claude@anthropic.com> is the known global identity "
            "and cannot identify a Hermes worker or vendor.",
            file=sys.stderr,
        )
        for sha, name, email in offenders:
            print(f"  {sha[:12]} {name} <{email}>", file=sys.stderr)
        print(
            "Set the repo/worktree-local identity to <profile>@fleet.local and "
            "bind vendor from the execution receipt instead.",
            file=sys.stderr,
        )
        return 1

    print(f"git-author-provenance: PASS — no leaked global identity in {base[:12]}..{head[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
