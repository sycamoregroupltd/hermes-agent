"""Sandbox helpers that clear inherited kanban worker routing env.

Reusable by tests and ad-hoc scripts so a run that intends to use an
isolated kanban DB cannot silently target a live board because
``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / ``HERMES_KANBAN_TASK`` /
``HERMES_KANBAN_WORKSPACE`` leaked from a running dispatcher/worker.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple


_WORKER_ROUTING_ENV_KEYS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_BRANCH",
)


def clear_kanban_routing_env() -> None:
    for key in _WORKER_ROUTING_ENV_KEYS:
        os.environ.pop(key, None)


def enter_sandbox(tmp_root: Path) -> Tuple[Path, Path]:
    kanban_home = tmp_root / ".hermes"
    kanban_home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(kanban_home)
    Path.home = lambda *a, **k: tmp_root  # type: ignore[assignment]
    return tmp_root, kanban_home
