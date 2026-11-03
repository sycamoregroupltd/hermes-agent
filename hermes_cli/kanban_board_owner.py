"""Board-owner default for Kanban cards created without an assignee.

One rule, applied by :func:`hermes_cli.kanban_db.create_task` so every entry
point (``hermes kanban create``, the ``kanban_create`` model tool, graph
builders) behaves identically:

* explicit assignee            -> kept unchanged;
* no assignee, ``triage=True`` -> stays unassigned (intentional triage path);
* no assignee, ``triage=False`` -> the target board's registered ``owner``
  from ``<kanban_home>/kanban/boards-manifest.json``.

The manifest is the fleet's board registry (shape: ``{"boards": {"<slug>":
{"owner": "<profile>", ...}}}``), living beside ``kanban/boards/`` under the
shared kanban root (:func:`hermes_cli.kanban_db.kanban_home`). The global
``kanban.default_assignee`` config is deliberately NOT consulted here.

Fail-closed: when a manifest is present but cannot be safely read, has no
entry/owner for the board, is ambiguous (duplicate slug keys, non-string
owner) or names a profile that is invalid or not on disk, the create raises
:class:`BoardOwnerError` (a ``ValueError``) before any row is written.

No manifest file at all means the board-owner policy is not configured on
this install: the create keeps the historical unassigned behaviour. A
dangling symlink or any other non-file at the manifest path is treated as
"cannot be safely read", not as absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

BOARDS_MANIFEST_FILENAME = "boards-manifest.json"


class BoardOwnerError(ValueError):
    """The board owner could not be resolved; the create must not proceed."""


def boards_manifest_path() -> Path:
    """``<kanban_home>/kanban/boards-manifest.json`` (shared across profiles,
    same root as ``kanban/boards/`` — see ``kanban_db.kanban_home``)."""
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / "kanban" / BOARDS_MANIFEST_FILENAME


def effective_board_slug(board: Optional[str]) -> str:
    """Slug of the board whose DB a ``create_task(board=board)`` call writes to.

    Mirrors ``kanban_db._board_path``: an explicit ``board=`` / scoped
    ``--board`` wins unless a ``HERMES_KANBAN_DB`` pin applies (dispatched
    workers, fenced children, machine flows), in which case the pinned DB is
    the board named by ``HERMES_KANBAN_BOARD`` (the dispatcher sets both). A
    pinned DB with no board name cannot be attributed to a board -> error.
    """
    from hermes_cli import kanban_db as kb

    slug = kb._explicit_board_slug(board)
    pin = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pin and (slug is None or kb._explicit_board_intent_pinned()):
        pinned_board = kb._normalize_board_slug(os.environ.get("HERMES_KANBAN_BOARD", ""))
        if not pinned_board:
            raise BoardOwnerError(
                "cannot resolve the board owner for an unassigned task: HERMES_KANBAN_DB "
                "is pinned but HERMES_KANBAN_BOARD is not set, so the target board is "
                "unknown. Pass an explicit assignee (or triage=true)."
            )
        return pinned_board
    return slug or kb.get_current_board()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise BoardOwnerError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _load_manifest(path: Path) -> Optional[dict]:
    """Parsed manifest, ``None`` when no manifest exists; raises when unsafe."""
    if not path.exists() and not path.is_symlink():
        return None
    where = f"boards manifest {path}"
    if not path.is_file():
        raise BoardOwnerError(f"{where} is not a readable file")
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except BoardOwnerError as exc:
        raise BoardOwnerError(f"{where} is ambiguous: {exc}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise BoardOwnerError(f"cannot read {where}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("boards"), dict):
        raise BoardOwnerError(f"{where} has no 'boards' object")
    return data


def resolve_board_owner(board: str) -> Optional[str]:
    """Canonical owner profile for ``board``; ``None`` only when no manifest
    exists. Raises :class:`BoardOwnerError` for every unsafe/unresolvable case."""
    path = boards_manifest_path()
    data = _load_manifest(path)
    if data is None:
        return None
    return _owner_from_manifest(data, path, board)


def _owner_from_manifest(data: dict, path: Path, board: str) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import normalize_profile_name, profile_exists, validate_profile_name

    slug = kb._normalize_board_slug(board) or kb.DEFAULT_BOARD
    hint = ("Register an owner for it in the manifest, or pass an explicit assignee "
            "(or triage=true).")

    matches = []
    for key, cfg in data["boards"].items():
        try:
            if (kb._normalize_board_slug(key) or "") == slug:
                matches.append((key, cfg))
        except ValueError:
            continue  # a malformed foreign slug can never be this board
    if not matches:
        raise BoardOwnerError(
            f"board {slug!r} has no entry in boards manifest {path}; refusing to create "
            f"an unassigned task. {hint}")
    if len(matches) > 1:
        keys = ", ".join(repr(k) for k, _ in matches)
        raise BoardOwnerError(
            f"board {slug!r} is ambiguous in boards manifest {path} (entries {keys}). {hint}")

    _, cfg = matches[0]
    owner = cfg.get("owner") if isinstance(cfg, dict) else None
    if owner is None or (isinstance(owner, str) and not owner.strip()):
        raise BoardOwnerError(
            f"board {slug!r} has no owner in boards manifest {path}. {hint}")
    if not isinstance(owner, str):
        raise BoardOwnerError(
            f"board {slug!r} owner in boards manifest {path} is ambiguous "
            f"(expected one profile name, got {type(owner).__name__}: {owner!r}). {hint}")
    try:
        canon = normalize_profile_name(owner)
        validate_profile_name(canon)
    except ValueError as exc:
        raise BoardOwnerError(
            f"board {slug!r} owner {owner!r} in boards manifest {path} is not a valid "
            f"profile name: {exc}") from exc
    if not profile_exists(canon):
        raise BoardOwnerError(
            f"board {slug!r} owner {canon!r} in boards manifest {path} is not an existing "
            f"profile. {hint}")
    return canon


def resolve_create_assignee(
    assignee: Optional[str], *, triage: bool, board: Optional[str],
) -> Optional[str]:
    """Apply the create-time assignee rule (see module docstring)."""
    if assignee is not None:
        return assignee
    if triage:
        return None
    path = boards_manifest_path()
    data = _load_manifest(path)
    if data is None:
        return None  # policy not configured on this install: historical behaviour
    return _owner_from_manifest(data, path, effective_board_slug(board))
