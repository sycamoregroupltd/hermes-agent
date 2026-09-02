"""Producer-side guard for the fleet loop registry.

The fleet registry is an optional control-plane store.  When the active
Hermes home is the Jarvis profile, enabled jobs must have a complete active
registry row before they are persisted.  Other profiles retain the upstream
cron behavior because their loop registries are not owned by this control
plane.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REQUIRED_ROW_FIELDS = (
    "id",
    "name",
    "kind",
    "status",
    "owner",
    "project",
    "trigger",
    "oracle",
    "budget",
    "consumer",
    "retirement",
    "store",
    "job_id",
    "skills",
)


def _active_hermes_home() -> Path:
    """Resolve the current profile home without caching it at import time."""
    from hermes_constants import get_hermes_home

    return get_hermes_home().expanduser().resolve()


def _loop_registry_path() -> Path:
    """Return the registry beside the active Hermes root.

    ``get_default_hermes_root`` maps ``<root>/profiles/jarvis`` back to
    ``<root>``, which keeps isolated tests and custom Hermes roots hermetic.
    """
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root().expanduser().resolve() / "loop-registry" / "registry.yaml"


def _is_jarvis_profile(home: Path) -> bool:
    return home.name == "jarvis" and home.parent.name == "profiles"


def _load_rows(registry_path: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    rows = data.get("loops") or []
    if not isinstance(rows, list):
        raise ValueError(f"Jarvis loop registry loops must be a list: {registry_path}")
    return {
        str(row.get("job_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("job_id")
    }


def _assert_registered(job: dict[str, Any], registry_path: Path) -> None:
    job_id = str(job.get("id") or job.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("enabled Jarvis cron job must have an id before persistence")
    if not registry_path.exists():
        raise ValueError(
            f"Jarvis loop registry is missing at {registry_path}; "
            "register the loop before enabling it"
        )
    row = _load_rows(registry_path).get(job_id)
    if row is None:
        raise ValueError(
            f"enabled Jarvis cron job {job_id} is not registered; "
            "register it before enabling"
        )
    missing = [field for field in _REQUIRED_ROW_FIELDS if row.get(field) in (None, "", [])]
    if missing:
        raise ValueError(
            f"Jarvis cron registry row {job_id} missing required fields: {', '.join(missing)}"
        )
    if row.get("status") != "active":
        raise ValueError(
            f"enabled Jarvis cron job {job_id} has registry status {row.get('status')!r}; "
            "only active rows may be enabled"
        )
    if str(row.get("job_id")) != job_id:
        raise ValueError(f"Jarvis cron registry row job_id mismatch for {job_id}")


def assert_enabled_job_registered(job: dict[str, Any]) -> None:
    """Fail closed for enabled jobs created or updated in the Jarvis profile.

    The check is intentionally read-only.  Registration requires the caller
    to provide the complete consumer/governance metadata; this guard never
    invents a consumer or mutates the registry as a side effect.
    """
    if not bool(job.get("enabled")):
        return
    home = _active_hermes_home()
    if not _is_jarvis_profile(home):
        return
    _assert_registered(job, _loop_registry_path())


__all__ = ["assert_enabled_job_registered"]
