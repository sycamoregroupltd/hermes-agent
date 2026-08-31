"""Disposable model of the proposed scratch completion artifact contract.

This is not production Hermes code. It is intentionally stdlib-only so an
independent checker can exercise the lifecycle without a board or gateway.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ArtifactGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class Artifact:
    source_path: str
    stored_path: str
    filename: str
    size: int
    sha256: str
    verified_at: int


@dataclass
class Task:
    task_id: str
    workspace: Path
    attachment_root: Path
    status: str = "running"
    run_id: int = 1
    attachments: list[Artifact] = field(default_factory=list)
    recovery_metadata: Optional[dict] = None


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _validate_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ArtifactGuardError(f"{field_name} must be 64 lowercase hexadecimal characters")
    return value


def _copy_bounded(source: Path, destination: Path) -> None:
    """Copy in bounded chunks and reject growth beyond the configured cap."""
    copied = 0
    with source.open("rb") as source_stream, destination.open("wb") as destination_stream:
        while chunk := source_stream.read(1024 * 1024):
            copied += len(chunk)
            if copied > MAX_ARTIFACT_BYTES:
                raise ArtifactGuardError("artifact exceeds maximum size during copy")
            destination_stream.write(chunk)


def _write_receipt(path: Path, receipt: dict) -> None:
    path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")


class CompletionKernel:
    """Small model of two-phase stage -> CAS commit -> scratch cleanup."""

    def __init__(self, clock: Callable[[], int] | None = None):
        self.clock = clock or (lambda: 1_700_000_000)

    def complete(
        self,
        task: Task,
        artifact_manifest: Optional[dict],
        *,
        expected_run_id: int = 1,
        copier: Optional[Callable[[Path, Path], None]] = None,
        cleaner: Optional[Callable[[Path], None]] = None,
        receipt_writer: Optional[Callable[[Path, dict], None]] = None,
    ) -> bool:
        if task.status == "done":
            return False
        if artifact_manifest is None:
            raise ArtifactGuardError("artifact declaration required for scratch completion")
        if artifact_manifest.get("schema_version") != 1:
            raise ArtifactGuardError("unsupported artifact manifest")
        policy = artifact_manifest.get("policy")
        entries = artifact_manifest.get("entries")
        if policy not in {"none", "retain"} or not isinstance(entries, list):
            raise ArtifactGuardError("invalid artifact policy or entries")
        if policy == "none" and entries:
            raise ArtifactGuardError("policy=none cannot carry entries")
        if policy == "retain" and not entries:
            raise ArtifactGuardError("policy=retain requires entries")
        if task.run_id != expected_run_id:
            return False

        staged: list[tuple[Path, Path, Path, int, str, str]] = []
        final_dir = task.attachment_root / task.task_id
        workspace = task.workspace.resolve()
        try:
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ArtifactGuardError("artifact entry must be an object")
                raw_source = entry.get("source_path")
                source = Path(str(raw_source or "")).resolve()
                if not source.is_relative_to(workspace) or not source.is_file():
                    raise ArtifactGuardError(f"invalid scratch source: {source}")

                raw_filename = entry.get("filename")
                filename = Path(str(raw_filename or source.name)).name
                if filename != str(raw_filename or source.name):
                    raise ArtifactGuardError("filename must be a basename")

                declared_size = entry.get("size")
                if isinstance(declared_size, bool) or not isinstance(declared_size, int):
                    raise ArtifactGuardError("size must be an integer")
                if declared_size < 0 or declared_size > MAX_ARTIFACT_BYTES:
                    raise ArtifactGuardError("artifact size exceeds maximum")
                declared_hash = _validate_digest(entry.get("sha256"), "sha256")
                expected_hash = entry.get("expected_sha256")
                if expected_hash is not None:
                    expected_hash = _validate_digest(expected_hash, "expected_sha256")

                source_size, source_hash = sha256_file(source)
                if source_size > MAX_ARTIFACT_BYTES:
                    raise ArtifactGuardError("artifact exceeds maximum size")
                if declared_size != source_size:
                    raise ArtifactGuardError(f"declared size mismatch: {source}")
                if declared_hash != source_hash:
                    raise ArtifactGuardError(f"declared digest mismatch: {source}")
                if expected_hash is not None and expected_hash != source_hash:
                    raise ArtifactGuardError(f"expected digest mismatch: {source}")

                final_dir.mkdir(parents=True, exist_ok=True)
                final = final_dir / filename
                suffix_number = 1
                while final.exists() or any(row[2] == final for row in staged):
                    stem, suffix = Path(filename).stem, Path(filename).suffix
                    final = final_dir / f"{stem}_{suffix_number}{suffix}"
                    suffix_number += 1
                with tempfile.NamedTemporaryFile(
                    prefix=f".{filename}.", suffix=".partial", dir=final_dir, delete=False
                ) as temporary:
                    partial = Path(temporary.name)
                try:
                    (copier or _copy_bounded)(source, partial)
                    with partial.open("rb+") as stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                    copied_size, copied_hash = sha256_file(partial)
                    if (copied_size, copied_hash) != (source_size, source_hash):
                        raise ArtifactGuardError(f"stored digest mismatch: {source}")
                    source_after_size, source_after_hash = sha256_file(source)
                    if (source_after_size, source_after_hash) != (source_size, source_hash):
                        raise ArtifactGuardError(f"source mutated during staging: {source}")
                    os.replace(partial, final)
                    final_size, final_hash = sha256_file(final)
                    if (final_size, final_hash) != (source_size, source_hash):
                        raise ArtifactGuardError(f"post-rename digest mismatch: {source}")
                    staged.append((source, partial, final, final_size, final_hash, filename))
                finally:
                    partial.unlink(missing_ok=True)

            # The manifest is the recovery receipt. In production this is
            # stored in task_runs/event metadata inside the existing txn.
            final_dir.mkdir(parents=True, exist_ok=True)
            receipt = {
                "schema_version": 1,
                "state": "verified",
                "policy": policy,
                "entries": [
                    {
                        "source_path": str(source),
                        "stored_path": str(final),
                        "filename": filename,
                        "size": size,
                        "sha256": digest,
                        "verification_status": "verified",
                    }
                    for source, _partial, final, size, digest, filename in staged
                ],
                "cleanup_status": "pending",
            }
            receipt_path = final_dir / "recovery.json"
            write_receipt = receipt_writer or _write_receipt
            write_receipt(receipt_path, receipt)

            # Simulated write transaction / CAS commit happens only now.
            task.attachments.extend(
                Artifact(
                    source_path=str(source),
                    stored_path=str(final),
                    filename=filename,
                    size=size,
                    sha256=digest,
                    verified_at=self.clock(),
                )
                for source, _partial, final, size, digest, filename in staged
            )
            task.recovery_metadata = receipt
            task.status = "done"
        except Exception:
            # No status transition and no exposed final attachment on failure.
            for _source, partial, final, _size, _digest, _filename in staged:
                partial.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
            if task.status != "done":
                task.attachments.clear()
                task.recovery_metadata = None
                shutil.rmtree(final_dir, ignore_errors=True)
                try:
                    task.attachment_root.rmdir()
                except OSError:
                    pass
            raise

        # Cleanup is deliberately post-commit. Its failure is recoverable and
        # must not make a successfully committed task appear incomplete.
        cleanup = cleaner or shutil.rmtree
        try:
            cleanup(task.workspace)
        except Exception as exc:
            receipt["cleanup_status"] = "deferred"
            receipt["cleanup_error_type"] = type(exc).__name__
            task.recovery_metadata = receipt
            try:
                write_receipt(receipt_path, receipt)
            except OSError:
                # The previously persisted pending receipt is recoverable; do
                # not report a post-commit exception or claim completed proof.
                receipt["recovery_metadata_write"] = "deferred"
                task.recovery_metadata = receipt
            return True

        receipt["cleanup_observed"] = "completed"
        receipt["cleanup_status"] = "completed"
        task.recovery_metadata = receipt
        try:
            write_receipt(receipt_path, receipt)
        except OSError:
            # Completion is committed, but the durable receipt still carries
            # the pre-commit pending state. Keep recovery fail-closed: mark the
            # state deferred and let the normal sweep reconcile it.
            receipt["cleanup_status"] = "deferred"
            receipt["recovery_metadata_write"] = "deferred"
            task.recovery_metadata = receipt
        return True
