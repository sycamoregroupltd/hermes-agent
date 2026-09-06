#!/usr/bin/env python3
"""Generate, verify, and fixture-restore a self-contained backup generation.

This module deliberately uses only the Python standard library.  It does not
create snapshots or transfer backups; it validates artifacts already placed in
a generation directory and writes the completion manifest last.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from typing import Any, Iterable


SCHEMA_VERSION = 1
MANIFEST_NAME = "backup-generation-manifest.json"
CHUNK_SIZE = 1024 * 1024
BOARD_STATES = frozenset({"active", "dormant", "denied"})
BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BackupGenerationError(Exception):
    """A validation or restore error suitable for operator output."""


def _reject_constant(value: str) -> None:
    raise BackupGenerationError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupGenerationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular_bytes(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BackupGenerationError(f"missing {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise BackupGenerationError(f"symlinked {label} is not allowed: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupGenerationError(f"{label} is not a regular file: {path}")
    if metadata.st_size == 0:
        raise BackupGenerationError(f"zero-byte {label} is not allowed: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BackupGenerationError(f"cannot read {label} {path}: {exc}") from exc


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular_bytes(path, label)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupGenerationError(f"malformed {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupGenerationError(f"malformed {label} {path}: root must be an object")
    return value, raw


def _require_keys(
    value: dict[str, Any],
    expected: set[str],
    label: str,
    *,
    allow_extra: bool = False,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected) if not allow_extra else []
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing keys {missing}")
        if extra:
            details.append(f"unknown keys {extra}")
        raise BackupGenerationError(f"malformed {label}: {', '.join(details)}")


def _safe_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BackupGenerationError(f"malformed {label}: path must be a non-empty string")
    if "\\" in value or "\x00" in value:
        raise BackupGenerationError(f"unsafe {label}: {value!r}")
    components = value.split("/")
    if (
        value.startswith("/")
        or any(component in {"", ".", ".."} for component in components)
        or any(":" in component for component in components)
    ):
        raise BackupGenerationError(f"unsafe {label}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise BackupGenerationError(f"unsafe {label}: {value!r}")
    return value


def _board_snapshot_path(slug: Any, label: str) -> str:
    if not isinstance(slug, str) or BOARD_SLUG_RE.fullmatch(slug) is None:
        raise BackupGenerationError(
            f"malformed {label}: board slug must be 1-64 lowercase ASCII alphanumeric, "
            "hyphen, or underscore characters and must start with an alphanumeric"
        )
    snapshot = f"kanban-{slug}.db"
    _safe_relative_path(snapshot, f"{label} derived snapshot")
    if PurePosixPath(snapshot).name != snapshot:
        raise BackupGenerationError(f"unsafe {label} derived snapshot: {snapshot!r}")
    return snapshot


def _parse_active_boards(path: Path) -> tuple[list[dict[str, str]], str]:
    document, raw = _load_json(path, "active boards manifest")
    _require_keys(
        document,
        {"version", "boards"},
        "active boards manifest",
        allow_extra=True,
    )
    if type(document["version"]) is not int or document["version"] != SCHEMA_VERSION:
        raise BackupGenerationError(
            f"unsupported active boards manifest version: {document['version']!r}"
        )
    raw_boards = document["boards"]
    if not isinstance(raw_boards, dict):
        raise BackupGenerationError("malformed active boards manifest: boards must be an object")

    boards: list[dict[str, str]] = []
    seen_snapshots: set[str] = set()
    for slug, board in raw_boards.items():
        label = f"active boards manifest boards[{slug!r}]"
        snapshot = _board_snapshot_path(slug, label)
        if not isinstance(board, dict):
            raise BackupGenerationError(f"malformed {label}: board metadata must be an object")
        if "state" not in board:
            raise BackupGenerationError(f"malformed {label}: missing required state")
        state = board["state"]
        if not isinstance(state, str) or state not in BOARD_STATES:
            raise BackupGenerationError(f"malformed {label}: unknown board state {state!r}")
        if state != "active":
            continue
        if snapshot in seen_snapshots:
            raise BackupGenerationError(f"duplicate board snapshot path: {snapshot}")
        seen_snapshots.add(snapshot)
        boards.append({"slug": slug, "snapshot": snapshot})

    boards.sort(key=lambda item: (item["slug"], item["snapshot"]))
    return boards, hashlib.sha256(raw).hexdigest()


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise BackupGenerationError(f"cannot read artifact {path}: {exc}") from exc
    return digest.hexdigest(), size


def _generation_root(path: Path) -> Path:
    root = Path(os.path.abspath(path))
    try:
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise BackupGenerationError(f"generation directory does not exist: {root}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise BackupGenerationError(f"generation directory must not be a symlink: {root}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise BackupGenerationError(f"generation path is not a directory: {root}")
    return root


def _walk_artifacts(root: Path) -> list[tuple[str, Path]]:
    artifacts: list[tuple[str, Path]] = []
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in sorted(directory_names):
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupGenerationError(f"symlinked artifact directory is not allowed: {path}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise BackupGenerationError(f"artifact directory entry is not a directory: {path}")
        directory_names.sort()
        for name in sorted(file_names):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST_NAME:
                continue
            _safe_relative_path(relative, "artifact path")
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupGenerationError(f"symlinked artifact is not allowed: {path}")
            if not stat.S_ISREG(metadata.st_mode):
                raise BackupGenerationError(f"artifact is not a regular file: {path}")
            if metadata.st_size == 0:
                raise BackupGenerationError(f"zero-byte artifact is not allowed: {path}")
            artifacts.append((relative, path))
    artifacts.sort(key=lambda item: item[0])
    return artifacts


def _validate_archive_member_names(members: Iterable[tarfile.TarInfo], archive: Path) -> list[tarfile.TarInfo]:
    checked: list[tarfile.TarInfo] = []
    paths: set[str] = set()
    regular_files = 0
    for member in members:
        raw_name = member.name
        # POSIX tar writers conventionally retain a trailing slash on directory
        # entries.  Remove only that harmless marker before applying the same
        # strict portable-path rules used for generation artifacts.
        candidate_name = raw_name.rstrip("/") if member.isdir() else raw_name
        name = _safe_relative_path(candidate_name, f"archive member in {archive}")
        member.name = name
        if name in paths:
            raise BackupGenerationError(f"duplicate archive member in {archive}: {name}")
        if not (member.isdir() or member.isreg()):
            raise BackupGenerationError(
                f"unsafe archive member type in {archive}: {name} (only files and directories are allowed)"
            )
        for parent in PurePosixPath(name).parents:
            if parent == PurePosixPath("."):
                continue
            parent_name = parent.as_posix()
            if parent_name in paths and not next(item for item in checked if item.name == parent_name).isdir():
                raise BackupGenerationError(f"archive member has a file parent in {archive}: {name}")
        if member.isreg() and any(existing.startswith(name + "/") for existing in paths):
            raise BackupGenerationError(f"archive file shadows another member in {archive}: {name}")
        paths.add(name)
        checked.append(member)
        regular_files += int(member.isreg())
    if regular_files == 0:
        raise BackupGenerationError(f"archive contains no regular files: {archive}")
    return checked


def _validate_archive(path: Path) -> None:
    # Reading gzip to EOF validates truncation, footer length, and CRC even on
    # Python versions where tarfile can stop after the tar end marker.
    decompressed_tail = bytearray()
    try:
        with gzip.open(path, "rb") as compressed:
            while chunk := compressed.read(CHUNK_SIZE):
                decompressed_tail.extend(chunk)
                if len(decompressed_tail) > 1024:
                    del decompressed_tail[:-1024]
    except (OSError, EOFError) as exc:
        raise BackupGenerationError(f"invalid or truncated tar.gz artifact {path}: {exc}") from exc
    if len(decompressed_tail) < 1024 or any(decompressed_tail):
        raise BackupGenerationError(f"partial tar archive (missing end markers): {path}")

    try:
        with tarfile.open(path, mode="r:gz") as archive:
            archive.ignore_zeros = True
            members = archive.getmembers()
            checked = _validate_archive_member_names(members, path)
            for member in checked:
                if not member.isreg():
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupGenerationError(f"cannot read archive member in {path}: {member.name}")
                read_size = 0
                while chunk := extracted.read(CHUNK_SIZE):
                    read_size += len(chunk)
                if read_size != member.size:
                    raise BackupGenerationError(
                        f"truncated archive member in {path}: {member.name} "
                        f"(expected {member.size}, read {read_size})"
                    )
    except BackupGenerationError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupGenerationError(f"invalid or truncated tar.gz artifact {path}: {exc}") from exc


def _validate_sqlite(path: Path) -> None:
    uri = path.absolute().as_uri() + "?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise BackupGenerationError(f"SQLite integrity check failed for {path}: {exc}") from exc
    if rows != [("ok",)]:
        details = "; ".join(str(row[0]) for row in rows[:10]) or "no result"
        raise BackupGenerationError(f"SQLite integrity check failed for {path}: {details}")


def _validate_payload(path: Path, kind: str) -> None:
    if kind == "tar.gz":
        _validate_archive(path)
    elif kind == "sqlite":
        _validate_sqlite(path)


def _artifact_kind(relative: str, board_snapshots: set[str]) -> str:
    if relative in board_snapshots:
        return "sqlite"
    if relative.endswith(".tar.gz"):
        return "tar.gz"
    return "file"


def _build_manifest(root: Path, active_boards_path: Path) -> dict[str, Any]:
    boards, active_sha256 = _parse_active_boards(active_boards_path)
    board_snapshots = {board["snapshot"] for board in boards}
    found = _walk_artifacts(root)
    found_paths = {relative for relative, _ in found}
    missing = sorted(board_snapshots - found_paths)
    if missing:
        raise BackupGenerationError(f"missing board snapshots required by active boards manifest: {missing}")

    artifacts: list[dict[str, Any]] = []
    archive_count = 0
    for relative, path in found:
        kind = _artifact_kind(relative, board_snapshots)
        digest, size = _sha256_and_size(path)
        if size == 0:
            raise BackupGenerationError(f"zero-byte artifact is not allowed: {path}")
        _validate_payload(path, kind)
        archive_count += int(kind == "tar.gz")
        artifacts.append({"kind": kind, "path": relative, "sha256": digest, "size": size})
    if archive_count == 0:
        raise BackupGenerationError("generation must contain at least one .tar.gz artifact")

    return {
        "active_boards": {"boards": boards, "source_sha256": active_sha256},
        "artifacts": artifacts,
        "schema_version": SCHEMA_VERSION,
    }


def generate_manifest(generation: Path, active_boards_path: Path) -> dict[str, Any]:
    """Validate generation artifacts and atomically write the completion manifest."""
    root = _generation_root(generation)
    destination = root / MANIFEST_NAME
    if destination.exists() or destination.is_symlink():
        existing = destination.lstat()
        if stat.S_ISLNK(existing.st_mode):
            raise BackupGenerationError(f"symlinked generation manifest is not allowed: {destination}")
        if not stat.S_ISREG(existing.st_mode):
            raise BackupGenerationError(f"generation manifest is not a regular file: {destination}")
    manifest = _build_manifest(root, active_boards_path)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{MANIFEST_NAME}.", dir=root, delete=False) as stream:
            temporary_name = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return manifest


def _parse_generation_manifest(path: Path) -> dict[str, Any]:
    document, _ = _load_json(path, "generation manifest")
    _require_keys(document, {"schema_version", "active_boards", "artifacts"}, "generation manifest")
    if type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise BackupGenerationError(
            f"unsupported generation manifest schema_version: {document['schema_version']!r}"
        )
    coverage = document["active_boards"]
    if not isinstance(coverage, dict):
        raise BackupGenerationError("malformed generation manifest: active_boards must be an object")
    _require_keys(coverage, {"boards", "source_sha256"}, "generation manifest active_boards")
    source_sha256 = coverage["source_sha256"]
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise BackupGenerationError("malformed generation manifest: invalid active boards SHA-256")
    try:
        int(source_sha256, 16)
    except ValueError as exc:
        raise BackupGenerationError("malformed generation manifest: invalid active boards SHA-256") from exc

    raw_boards = coverage["boards"]
    if not isinstance(raw_boards, list):
        raise BackupGenerationError("malformed generation manifest: active_boards.boards must be an array")
    boards: list[dict[str, str]] = []
    seen_board_slugs: set[str] = set()
    seen_snapshots: set[str] = set()
    for index, board in enumerate(raw_boards):
        label = f"generation manifest active_boards.boards[{index}]"
        if not isinstance(board, dict):
            raise BackupGenerationError(f"malformed {label}: entry must be an object")
        _require_keys(board, {"slug", "snapshot"}, label)
        slug = board["slug"]
        expected_snapshot = _board_snapshot_path(slug, label)
        snapshot = _safe_relative_path(board["snapshot"], f"{label} snapshot")
        if snapshot != expected_snapshot:
            raise BackupGenerationError(
                f"malformed {label}: snapshot {snapshot!r} does not match derived path "
                f"{expected_snapshot!r}"
            )
        if slug in seen_board_slugs or snapshot in seen_snapshots:
            raise BackupGenerationError(f"malformed {label}: duplicate board slug or snapshot")
        seen_board_slugs.add(slug)
        seen_snapshots.add(snapshot)
        boards.append({"slug": slug, "snapshot": snapshot})
    if boards != sorted(boards, key=lambda item: (item["slug"], item["snapshot"])):
        raise BackupGenerationError("malformed generation manifest: board coverage is not sorted")

    raw_artifacts = document["artifacts"]
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise BackupGenerationError("malformed generation manifest: artifacts must be a non-empty array")
    artifacts: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, artifact in enumerate(raw_artifacts):
        label = f"generation manifest artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise BackupGenerationError(f"malformed {label}: entry must be an object")
        _require_keys(artifact, {"kind", "path", "sha256", "size"}, label)
        relative = _safe_relative_path(artifact["path"], f"{label} path")
        kind = artifact["kind"]
        digest = artifact["sha256"]
        size = artifact["size"]
        if kind not in {"file", "sqlite", "tar.gz"}:
            raise BackupGenerationError(f"malformed {label}: invalid kind {kind!r}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise BackupGenerationError(f"malformed {label}: invalid SHA-256")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise BackupGenerationError(f"malformed {label}: invalid SHA-256") from exc
        if type(size) is not int or size <= 0:
            raise BackupGenerationError(f"malformed {label}: size must be a positive integer")
        if relative in seen_paths:
            raise BackupGenerationError(f"malformed generation manifest: duplicate artifact {relative}")
        seen_paths.add(relative)
        artifacts.append({"kind": kind, "path": relative, "sha256": digest, "size": size})
    if artifacts != sorted(artifacts, key=lambda item: item["path"]):
        raise BackupGenerationError("malformed generation manifest: artifacts are not sorted")

    document["active_boards"] = {"boards": boards, "source_sha256": source_sha256}
    document["artifacts"] = artifacts
    return document


def verify_generation(generation: Path, active_boards_path: Path) -> dict[str, Any]:
    """Fail closed unless every declared artifact and board snapshot is complete."""
    root = _generation_root(generation)
    manifest = _parse_generation_manifest(root / MANIFEST_NAME)
    expected_boards, active_sha256 = _parse_active_boards(active_boards_path)
    coverage = manifest["active_boards"]
    if coverage["source_sha256"] != active_sha256:
        raise BackupGenerationError("stale board coverage: active boards manifest SHA-256 changed")
    if coverage["boards"] != expected_boards:
        raise BackupGenerationError("inconsistent board coverage in generation manifest")

    artifacts = manifest["artifacts"]
    declared_paths = {artifact["path"] for artifact in artifacts}
    actual_paths = {relative for relative, _ in _walk_artifacts(root)}
    if actual_paths != declared_paths:
        missing = sorted(declared_paths - actual_paths)
        extra = sorted(actual_paths - declared_paths)
        raise BackupGenerationError(
            f"generation artifact set mismatch: missing={missing}, unmanifested={extra}"
        )

    board_snapshots = {board["snapshot"] for board in expected_boards}
    artifact_by_path = {artifact["path"]: artifact for artifact in artifacts}
    for snapshot in sorted(board_snapshots):
        artifact = artifact_by_path.get(snapshot)
        if artifact is None or artifact["kind"] != "sqlite":
            raise BackupGenerationError(f"board snapshot is not covered as SQLite: {snapshot}")
    if any(artifact["kind"] == "sqlite" and artifact["path"] not in board_snapshots for artifact in artifacts):
        raise BackupGenerationError("generation manifest contains an undeclared board snapshot")
    for artifact in artifacts:
        expected_kind = _artifact_kind(artifact["path"], board_snapshots)
        if artifact["kind"] != expected_kind:
            raise BackupGenerationError(
                f"artifact kind mismatch for {artifact['path']}: "
                f"expected {expected_kind}, got {artifact['kind']}"
            )
    if not any(artifact["kind"] == "tar.gz" for artifact in artifacts):
        raise BackupGenerationError("generation manifest contains no tar.gz artifact")

    for artifact in artifacts:
        path = root.joinpath(*PurePosixPath(artifact["path"]).parts)
        raw_metadata = path.lstat()
        if stat.S_ISLNK(raw_metadata.st_mode):
            raise BackupGenerationError(f"symlinked artifact is not allowed: {path}")
        if not stat.S_ISREG(raw_metadata.st_mode):
            raise BackupGenerationError(f"artifact is not a regular file: {path}")
        if raw_metadata.st_size == 0:
            raise BackupGenerationError(f"zero-byte artifact is not allowed: {path}")
        digest, size = _sha256_and_size(path)
        if size != artifact["size"]:
            raise BackupGenerationError(
                f"artifact size mismatch for {artifact['path']}: expected {artifact['size']}, got {size}"
            )
        if digest != artifact["sha256"]:
            raise BackupGenerationError(f"artifact SHA-256 mismatch for {artifact['path']}")
        _validate_payload(path, artifact["kind"])
    return manifest


def _copy_verified(source: Path, destination: Path, artifact: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        while chunk := input_stream.read(CHUNK_SIZE):
            output_stream.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise BackupGenerationError(f"artifact changed while restoring: {artifact['path']}")


def _extract_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            archive.ignore_zeros = True
            members = _validate_archive_member_names(archive.getmembers(), archive_path)
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise BackupGenerationError(f"cannot extract archive member: {member.name}")
                written = 0
                with target.open("xb") as output:
                    while chunk := source.read(CHUNK_SIZE):
                        output.write(chunk)
                        written += len(chunk)
                if written != member.size:
                    raise BackupGenerationError(f"truncated archive member while restoring: {member.name}")
    except BackupGenerationError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupGenerationError(f"archive extraction failed for {archive_path}: {exc}") from exc


def restore_fixture(generation: Path, active_boards_path: Path, destination: Path) -> dict[str, Any]:
    """Copy and extract a verified generation into an existing empty directory."""
    manifest = verify_generation(generation, active_boards_path)
    root = _generation_root(generation)
    target = Path(os.path.abspath(destination))
    try:
        metadata = target.lstat()
    except FileNotFoundError as exc:
        raise BackupGenerationError(f"restore destination does not exist: {target}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupGenerationError(f"restore destination must be a real directory: {target}")
    if any(target.iterdir()):
        raise BackupGenerationError(f"restore destination must be empty: {target}")

    restored_generation = target / "generation"
    restored_generation.mkdir()
    for artifact in manifest["artifacts"]:
        relative = PurePosixPath(artifact["path"])
        source = root.joinpath(*relative.parts)
        output = restored_generation.joinpath(*relative.parts)
        _copy_verified(source, output, artifact)
    shutil.copyfile(root / MANIFEST_NAME, restored_generation / MANIFEST_NAME)

    # Validate the copied fixture before extracting anything from it.
    verify_generation(restored_generation, active_boards_path)
    extracted_root = target / "extracted"
    extracted_root.mkdir()
    for artifact in manifest["artifacts"]:
        if artifact["kind"] != "tar.gz":
            continue
        relative = PurePosixPath(artifact["path"])
        archive_path = restored_generation.joinpath(*relative.parts)
        archive_destination = extracted_root.joinpath(*relative.parts)
        archive_destination.parent.mkdir(parents=True, exist_ok=True)
        _extract_archive(archive_path, archive_destination)
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("generate", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("generation", type=Path, help="backup generation directory")
        subparser.add_argument(
            "--active-boards-manifest", required=True, type=Path, help="current active boards JSON manifest"
        )
    restore = subparsers.add_parser("restore", help="verify and extract into an isolated empty directory")
    restore.add_argument("generation", type=Path, help="backup generation directory")
    restore.add_argument("--active-boards-manifest", required=True, type=Path)
    restore.add_argument("--destination", required=True, type=Path, help="existing empty fixture directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            manifest = generate_manifest(args.generation, args.active_boards_manifest)
            action = "generated"
        elif args.command == "verify":
            manifest = verify_generation(args.generation, args.active_boards_manifest)
            action = "verified"
        else:
            manifest = restore_fixture(args.generation, args.active_boards_manifest, args.destination)
            action = "restored"
    except (BackupGenerationError, OSError) as exc:
        print(f"backup generation {args.command} failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"backup generation {action}: {len(manifest['artifacts'])} artifacts, "
        f"{len(manifest['active_boards']['boards'])} board snapshots"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
