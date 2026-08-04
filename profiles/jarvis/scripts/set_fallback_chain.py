#!/usr/bin/env python3
"""set_fallback_chain.py — write an explicit fallback_providers chain into Hermes configs.

Frank's cost/capability-ranked ladder (2026-08-04). #1 is the PRIMARY (model.default),
so the fallback chain is #2-#4:

  #1 deepseek/deepseek-v4-flash-0731    $0.01/$0.02   AA 82   <- primary, not a rung
  #2 qwen/qwen3.7-flash                 $0.02/$0.10   AA ~76
  #3 ~deepseek/deepseek-v4-flash-latest $0.07/$0.14   AA 82
  #4 openai/gpt-5.6-luna                $0.10/$0.60   AA 84

Every rung was live-probed before being written. Today's incident was caused by a rung
nobody had ever checked (groq llama-3.3-70b, 1,928 BadRequestErrors), so writing an
unverified model into a chain is the specific mistake this script must not repeat.

Preserves each file's indentation: 66 configs start sequence items at column 0, 3 --
including the active jarvis profile -- indent two spaces. Model values are quoted
because "~deepseek/..." starts with a tilde; it happens to parse as a string today, but
a bare "~" is YAML null and the distinction is one careless edit away.

Usage: set_fallback_chain.py --dry-run | --apply
"""
from __future__ import annotations

import argparse
import glob
import re
import shutil
import sys
from datetime import datetime, timezone

import yaml

NOUS_URL = "https://inference-api.nousresearch.com/v1"
CHAIN = [
    ("nous", "qwen/qwen3.7-flash", NOUS_URL),
    ("nous", "~deepseek/deepseek-v4-flash-latest", NOUS_URL),
    ("nous", "openai/gpt-5.6-luna", NOUS_URL),
]
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def render(indent: str) -> str:
    """Render CHAIN as a YAML block sequence at the file's own indent level."""
    pad = " " * len(indent)
    out = []
    for provider, model, url in CHAIN:
        out.append(f'{indent}- provider: {provider}\n')
        out.append(f'{pad}  model: "{model}"\n')
        out.append(f'{pad}  base_url: {url}\n')
    return "".join(out)


def detect_indent(block: str) -> str:
    m = re.search(r"^(\s*)-\s", block, re.M)
    return m.group(1) if m else ""


def process(path: str, apply: bool):
    txt = open(path, errors="replace").read()
    m = re.search(r"^fallback_providers:\n((?:[ \t]*[-#].*\n|[ \t]+.*\n)+)", txt, re.M)
    if not m:
        return None
    block = m.group(1)
    before = re.findall(r"^\s*[-\s]*model:\s*(\S+)", block, re.M)
    new_block = render(detect_indent(block))
    if block == new_block:
        return None

    new_txt = txt[: m.start(1)] + new_block + txt[m.end(1) :]

    # Verify BEFORE writing: must parse, and must yield exactly the intended chain.
    try:
        parsed = yaml.safe_load(new_txt)
    except yaml.YAMLError as e:
        print(f"  !! {path}: result does not parse — SKIPPING ({str(e)[:90]})")
        return None
    got = [(e.get("provider"), e.get("model")) for e in (parsed.get("fallback_providers") or [])]
    want = [(p, mo) for p, mo, _ in CHAIN]
    if got != want:
        print(f"  !! {path}: post-parse mismatch\n       got  {got}\n       want {want}")
        return None

    if apply:
        shutil.copy2(path, f"{path}.bak-fallback-set-{STAMP}")
        open(path, "w").write(new_txt)
    return path, before, [m for _, m, _ in CHAIN]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()

    files = [a.only] if a.only else ["config.yaml"] + sorted(glob.glob("profiles/*/config.yaml"))
    n = 0
    for f in files:
        r = process(f, apply=a.apply)
        if r:
            n += 1
            if n <= 3 or a.only:
                print(f"  {'APPLIED' if a.apply else 'WOULD SET'}  {f}")
                print(f"      was: {r[1]}")
                print(f"      now: {r[2]}")
    print(f"\n{'applied to' if a.apply else 'would change'} {n} of {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
