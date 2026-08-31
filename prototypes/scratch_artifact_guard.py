"""Disposable model of the proposed scratch completion artifact contract.

This is not production Hermes code. It is intentionally stdlib-only so an
independent checker can exercise the lifecycle without a board or gateway.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


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
        try:
            for entry in entries:
                source = Path(str(entry.get("source_path", ""))).resolve()
                workspace = task.workspace.resolve()
                if not source.is_relative_to(workspace) or not source.is_file():
                    raise ArtifactGuardError(f"invalid scratch source: {source}")
                filename = Path(str(entry.get("filename") or source.name)).name
                if filename != str(entry.get("filename") or source.name):
                    raise ArtifactGuardError("filename must be a basename")
                source_size, source_hash = sha256_file(source)
                expected_hash = entry.get("expected_sha256")
                if expected_hash is not None and expected_hash != source_hash:
                    raise ArtifactGuardError(f"expected digest mismatch: {source}")
                final_dir.mkdir(parents=True, exist_ok=True)
                final = final_dir / filename
                if final.exists() or any(row[2] == final for row in staged):
                    stem, suffix = final.stem, final.suffix
                    final = final_dir / f"{stem}_1{suffix}"
                with tempfile.NamedTemporaryFile(
                    prefix=f".{filename}.", suffix=".partial", dir=final_dir, delete=False
                ) as temporary:
                    partial = Path(temporary.name)
                try:
                    (copier or shutil.copyfile)(source, partial)
                    with partial.open("rb+") as stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                    copied_size, copied_hash = sha256_file(partial)
                    if (copied_size, copied_hash) != (source_size, source_hash):
                        raise ArtifactGuardError(f"stored digest mismatch: {source}")
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
            receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

            # Simulated write transaction / CAS commit happens only now.
            task.attachments.extend(
                Artifact(source_path=str(source), stored_path=str(final), filename=filename,
                         size=size, sha256=digest, verified_at=self.clock())
                for source, _partial, final, size, digest, filename in staged
            )
            task.recovery_metadata = receipt
            task.status = "done"
            shutil.rmtree(task.workspace)
            receipt["cleanup_status"] = "completed"
            task.recovery_metadata = receipt
            receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
            return True
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
