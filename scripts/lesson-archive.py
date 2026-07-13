#!/usr/bin/env python3
"""lesson-archive.py — rotate superseded + oldest lessons out of the active
lessons/ tree into lessons/archive/, following the same 100-active / 50-archive
discipline as COLLECTIVE_MEMORIES.md.

Rules:
  - Any lesson with propagation_state == 'superseded' is always archived.
  - If active count > 100, archive oldest (by created_at) until at/under 100.
  - Never archive more than needed; preserve fan-out landing maps by moving the
    full INDEX entry (minus the file body, which is re-linked) into the archive.

Reversible: moves files + rewrites INDEX into active + archive halves. No
destructive deletion; the archive dir is the holding area.

Usage:
  lesson-archive.py [--active-cap 100] [--dry-run] [--index PATH]
"""
import argparse
import json
import os
import shutil
import sys

DEFAULT_INDEX = "/home/frank/.hermes/shared-memory/lessons/INDEX.jsonl"
ACTIVE_CAP = 100

# Derive the archive dir from the index file's parent so the script is safe to
# run against a fixture index (e.g. in tests) without touching the real store.
def archive_dir_for(index_path):
    return os.path.join(os.path.dirname(os.path.abspath(index_path)), "archive")


def parse_dt(s):
    if not s:
        return 0
    try:
        return int(__import__("datetime").datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser(description="Rotate lessons into archive/.")
    ap.add_argument("--active-cap", type=int, default=ACTIVE_CAP)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--index", default=DEFAULT_INDEX)
    args = ap.parse_args()
    ARCHIVE_DIR = archive_dir_for(args.index)

    try:
        with open(args.index, "r", encoding="utf-8") as fh:
            entries = [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        print("INDEX not found:", args.index, file=sys.stderr)
        return 1

    # separate superseded (always archive) from active
    superseded = [e for e in entries if e.get("propagation_state") == "superseded"]
    active = [e for e in entries if e.get("propagation_state") != "superseded"]

    # if over cap, archive oldest active until at/under cap
    if len(active) > args.active_cap:
        active_sorted = sorted(active, key=lambda e: parse_dt(e.get("created_at")))
        overflow = len(active_sorted) - args.active_cap
        to_archive = superseded + active_sorted[:overflow]
        kept_active = active_sorted[overflow:]
    else:
        to_archive = superseded
        kept_active = active

    print(f"active={len(active)} cap={args.active_cap} archive_this_run={len(to_archive)}")

    if args.dry_run:
        for e in to_archive:
            print(f"  [dry-run] ARCHIVE {e.get('lesson_id')} ({e.get('propagation_state')})")
        return 0

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    for e in to_archive:
        lid = e.get("lesson_id", "unknown")
        src = e.get("path", "")
        if src and os.path.exists(src):
            dst = os.path.join(ARCHIVE_DIR, os.path.basename(src))
            shutil.move(src, dst)
            e["path"] = dst
            e["archived_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        else:
            print(f"  [warn] no file for {lid}: {src}", file=sys.stderr)
        # mark archived so they are excluded from the active set on re-read
        e["propagation_state"] = "archived"

    with open(args.index, "w", encoding="utf-8") as fh:
        for e in kept_active + to_archive:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    print("INDEX rewritten:", len(kept_active), "active /", len(to_archive), "archived")
    return 0


if __name__ == "__main__":
    sys.exit(main())
