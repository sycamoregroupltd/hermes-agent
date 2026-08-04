#!/usr/bin/env python3
"""prune_fallback_chain.py — remove named models from fallback_providers in Hermes configs.

WHY (2026-08-04, Frank): deepseek-v4-flash-0731 was throwing HTTP 503 "temporarily
unavailable due to upstream capacity limits" (2,644x in August, 2,383 in one hour).
That is a Nous-side supply problem, not a credit or quota problem. Each 503 triggered
a failover, and because the top two rungs were broken -- llama-3.3-70b (groq) returning
1,928 BadRequestErrors, deepseek-v4-pro (nvidia) rate-limited 672x -- the chain
cascaded down to nvidia/nemotron-3-ultra-550b, which costs 48x on prompt and 144x on
completion vs the primary. 1,212 failovers on Aug 4 alone.

Removes entries by model substring. Text-based on purpose: yaml.safe_load + dump would
reformat and strip comments across ~70 live config files. Verifies each result parses
and contains exactly the expected remaining models before writing.

Usage:
  prune_fallback_chain.py --dry-run
  prune_fallback_chain.py --apply
"""
from __future__ import annotations

import argparse
import glob
import re
import shutil
import sys
from datetime import datetime, timezone

import yaml

DEFAULT_REMOVE = ("nemotron", "llama-3.3-70b")
REMOVE: tuple[str, ...] = DEFAULT_REMOVE
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def entries(block: str) -> list[str]:
    """Split a YAML block-sequence into per-entry text chunks, preserving formatting.

    Matches the item marker at ANY indent. These configs are not uniform: 66 files
    start entries at column 0 ("- provider:"), 3 -- including the active jarvis
    profile -- indent them two spaces. A column-0-only split collapsed those into a
    single chunk, so the whole chain matched the first model and would have been
    deleted wholesale. The empty-result guard caught it, but the real bug was here.
    """
    marker = re.compile(r"^\s*-\s")
    out, cur = [], []
    for line in block.splitlines(keepends=True):
        if marker.match(line):
            if cur:
                out.append("".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        out.append("".join(cur))
    return out


def model_of(entry: str) -> str:
    m = re.search(r"^\s*[-\s]*model:\s*(\S+)", entry, re.M)
    return m.group(1) if m else ""


def process(path: str, apply: bool) -> tuple[str, list[str], list[str]] | None:
    txt = open(path, errors="replace").read()
    m = re.search(r"^fallback_providers:\n((?:[ -].*\n)+)", txt, re.M)
    if not m:
        return None
    block = m.group(1)
    chunks = entries(block)
    before = [model_of(c) for c in chunks]
    keep = [c for c in chunks if not any(r in model_of(c) for r in REMOVE)]
    after = [model_of(c) for c in keep]
    if before == after:
        return None  # nothing to do

    if not keep:
        print(f"  !! {path}: every entry would be removed — SKIPPING (refusing to empty a chain)")
        return None

    new_txt = txt[: m.start(1)] + "".join(keep) + txt[m.end(1) :]

    # Verify BEFORE writing: must parse, and must contain exactly the expected models.
    try:
        parsed = yaml.safe_load(new_txt)
    except yaml.YAMLError as e:
        print(f"  !! {path}: result does not parse — SKIPPING ({str(e)[:80]})")
        return None
    got = [e.get("model") for e in (parsed.get("fallback_providers") or [])]
    if got != after:
        print(f"  !! {path}: post-parse mismatch {got} != {after} — SKIPPING")
        return None

    if apply:
        shutil.copy2(path, f"{path}.bak-fallback-prune-{STAMP}")
        open(path, "w").write(new_txt)
    return path, before, after


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--only", help="restrict to one path (for a supervised first run)")
    ap.add_argument("--remove", help=f"comma-separated model substrings (default: {','.join(DEFAULT_REMOVE)})")
    a = ap.parse_args()

    global REMOVE
    if a.remove:
        REMOVE = tuple(s.strip() for s in a.remove.split(",") if s.strip())
    print(f"removing entries whose model matches: {list(REMOVE)}\n")

    files = [a.only] if a.only else ["config.yaml"] + sorted(glob.glob("profiles/*/config.yaml"))
    changed = 0
    for f in files:
        r = process(f, apply=a.apply)
        if r:
            path, before, after = r
            changed += 1
            removed = [b for b in before if b not in after]
            print(f"  {'APPLIED' if a.apply else 'WOULD CHANGE'}  {path}")
            print(f"      remove: {removed}")
            print(f"      result: {' -> '.join(after)}")
    print(f"\n{'applied to' if a.apply else 'would change'} {changed} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
