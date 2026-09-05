#!/usr/bin/env python3
"""editable-pymodules-doctor — catch stale editable MAPPING before cron imports fail.

Proven gap 2026-09-05 ~18:04 BST: hermes_startup_watchdog / state_* were listed in
pyproject py-modules + egg-info but missing from __editable___*_finder.MAPPING after
a partial pyproject edit. Cron imports then ModuleNotFoundError while files existed
on disk. Fix was `pip install -e . --no-deps` (NOT hermes update).

This doctor imports every py-modules entry from a non-repo cwd (/tmp) using the
active venv, and exits non-zero only on miss. Silent on OK (no_agent-friendly).

Isolation-safe: read-only, no pip, no hermes update, no gateway restart.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("HERMES_AGENT_ROOT", "/home/frank/.hermes/hermes-agent"))
PYPROJECT = REPO / "pyproject.toml"


def _py_modules() -> list[str]:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S)
    if not m:
        print("editable-pymodules-doctor: FAIL — no py-modules list in", PYPROJECT)
        sys.exit(2)
    return re.findall(r"\"([^\"]+)\"", m.group(1))


def main() -> int:
    # Force non-repo cwd so we exercise editable/sys.path, not accidental local imports.
    os.chdir("/tmp")
    names = _py_modules()
    miss: list[str] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — report every miss
            miss.append(f"{name}: {type(exc).__name__}: {exc}")
    if not miss:
        # Silent OK for cron/no_agent wrappers.
        return 0
    print(f"editable-pymodules-doctor: FAIL — {len(miss)}/{len(names)} py-modules import miss from /tmp")
    print("Likely stale editable MAPPING. Isolation-safe heal:")
    print(f"  cd {REPO} && ./venv/bin/pip install -e . --no-deps")
    print("(NOT hermes update.)")
    for line in miss:
        print("  MISS", line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
