#!/usr/bin/env python3
"""lesson-lookup.py — pure-read retrieval hook for the cross-PM learning fabric.

Reads lessons/INDEX.jsonl, filters by the requesting PM, ranks by tag/token
overlap with the supplied --tags / --task text, and prints the top <=N
lessons as compact pointer lines. No network, no writes, sub-100ms.

Usage:
  lesson-lookup.py --pm <pm_name> --tags <t1,t2> --task "<title/body text>"
                   [--top N] [--index PATH] [--json]

Output line format:
  [Cross-PM lesson] <lesson_id> — <title> — <one-line rule> — <path>

Hard gate: read-only. Touches nothing but the INDEX file (read).
"""
import argparse
import json
import os
import re
import sys

DEFAULT_INDEX = "/home/frank/.hermes/shared-memory/lessons/INDEX.jsonl"
STOPWORDS = set("the a an of to in for on and or with that this is are be by from at as it its not no if when then can will should must may do does done into out up down over under between within which what who how why all any each".split())


def load_index(index_path):
    out = []
    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # skip malformed lines rather than crash the read hook
                    continue
    except FileNotFoundError:
        return []
    return out


def tokenize(text):
    if not text:
        return set()
    toks = re.findall(r"[a-z0-9_]+", text.lower())
    return {t for t in toks if t not in STOPWORDS and len(t) > 1}


def relevant_to_pm(entry, pm):
    rt = entry.get("relevant_to") or []
    if "all" in rt:
        return True
    return pm in rt


def rank(entry, q_tags, q_task_tokens):
    e_tags = set(entry.get("tags") or [])
    tag_hits = len(q_tags & e_tags)
    blob = " ".join([
        entry.get("title", ""),
        entry.get("rule", ""),
        " ".join(e_tags),
        entry.get("root_cause", ""),
    ])
    blob_tokens = tokenize(blob)
    task_hits = len(q_task_tokens & blob_tokens)
    # weight tag overlap 3x over free-text token overlap
    return tag_hits * 3 + task_hits


def main():
    ap = argparse.ArgumentParser(description="Cross-PM lesson lookup (pure read).")
    ap.add_argument("--pm", required=True, help="Requesting PM profile name (e.g. sycode-trading-pm).")
    ap.add_argument("--tags", default="", help="Comma-separated tags to match.")
    ap.add_argument("--task", default="", help="Task title/body text to rank against.")
    ap.add_argument("--top", type=int, default=5, help="Max lessons to return (<=5 enforced).")
    ap.add_argument("--index", default=DEFAULT_INDEX, help="Path to INDEX.jsonl.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text lines.")
    args = ap.parse_args()

    entries = load_index(args.index)
    q_tags = set(t.strip().lower() for t in args.tags.split(",") if t.strip())
    q_task_tokens = tokenize(args.task)

    scored = []
    for e in entries:
        # Surface only active, non-superseded, non-archived lessons
        if e.get("propagation_state") in ("superseded", "archived"):
            continue
        if not relevant_to_pm(e, args.pm):
            continue
        sc = rank(e, q_tags, q_task_tokens)
        scored.append((sc, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_n = min(max(args.top, 0), 5)
    results = [e for _, e in scored[:top_n]]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    if not results:
        # explicit no-match signal so callers can fall back cleanly
        print("[Cross-PM lesson] (none relevant)")
        return 0

    for e in results:
        line = "[Cross-PM lesson] {lid} — {title} — {rule} — {path}".format(
            lid=e.get("lesson_id", "?"),
            title=(e.get("title") or "").replace("\n", " ").strip(),
            rule=" ".join((e.get("rule") or "").split()),
            path=e.get("path", ""),
        )
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
