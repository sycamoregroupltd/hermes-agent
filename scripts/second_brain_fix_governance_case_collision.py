#!/usr/bin/env python3
"""Canonical one-shot namespace repair: move the stray case-variant note from
obsidian/sycode-trading/Governance/ into the canonical governance/ namespace and
remove the colliding directory. Reviewed scope: exactly one file, one rmdir."""
import os
import shutil
import sys

SRC = "/home/frank/obsidian/sycode-trading/Governance/2026-07-28-blocked-to-running-soak-monitor.md"
DST = "/home/frank/obsidian/sycode-trading/governance/2026-07-28-blocked-to-running-soak-monitor.md"
STRAY_DIR = "/home/frank/obsidian/sycode-trading/Governance"

def main() -> int:
    if not os.path.isdir(STRAY_DIR):
        print("stray Governance/ already absent; nothing to do")
        return 0
    contents = os.listdir(STRAY_DIR)
    if contents != ["2026-07-28-blocked-to-running-soak-monitor.md"]:
        print(f"REFUSING: stray dir has unexpected contents {contents}")
        return 1
    if os.path.exists(DST):
        print("REFUSING: destination already exists")
        return 1
    shutil.move(SRC, DST)
    os.rmdir(STRAY_DIR)
    print(f"moved -> {DST}; removed {STRAY_DIR}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
