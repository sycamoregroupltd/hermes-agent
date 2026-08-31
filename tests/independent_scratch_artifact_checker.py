"""Independent checker for t_90a453ed.

It intentionally does not call the maker test methods or unittest discovery;
it reconstructs each acceptance scenario from the public prototype contract.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from prototypes.scratch_artifact_guard import ArtifactGuardError, CompletionKernel, Task


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        kernel = CompletionKernel(clock=lambda: 7)

        # 1 valid attachment
        ws = root / "valid-ws"; ws.mkdir()
        store = root / "valid-store"
        src = ws / "valid.txt"; src.write_bytes(b"valid")
        task = Task("valid", ws, store)
        manifest = {"schema_version": 1, "policy": "retain", "entries": [{"source_path": str(src), "filename": "valid.txt", "expected_sha256": digest(src)}]}
        assert_true(kernel.complete(task, manifest), "valid completion rejected")
        assert_true(task.status == "done" and not ws.exists(), "valid cleanup/state wrong")
        assert_true(len(task.attachments) == 1 and digest(Path(task.attachments[0].stored_path)) == task.attachments[0].sha256, "valid digest evidence missing")

        # 2 missing declaration
        ws = root / "missing-ws"; ws.mkdir(); (ws / "x.txt").write_bytes(b"x")
        task = Task("missing", ws, root / "missing-store")
        try:
            kernel.complete(task, None)
        except ArtifactGuardError:
            pass
        else:
            raise AssertionError("missing declaration was accepted")
        assert_true(task.status != "done" and ws.exists(), "missing declaration removed workspace")

        # 3 hash mismatch
        ws = root / "hash-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"x")
        task = Task("hash", ws, root / "hash-store")
        bad = {"schema_version": 1, "policy": "retain", "entries": [{"source_path": str(src), "filename": "x.txt", "expected_sha256": "f" * 64}]}
        try:
            kernel.complete(task, bad)
        except ArtifactGuardError:
            pass
        else:
            raise AssertionError("hash mismatch was accepted")
        assert_true(task.status != "done" and ws.exists(), "hash mismatch removed workspace")

        # 4 partial copy
        ws = root / "partial-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"partial")
        task = Task("partial", ws, root / "partial-store")
        def fail_copy(source: Path, destination: Path) -> None:
            destination.write_bytes(source.read_bytes()[:2])
            raise OSError("checker injected partial copy")
        try:
            kernel.complete(task, {"schema_version": 1, "policy": "retain", "entries": [{"source_path": str(src), "filename": "x.txt"}]}, copier=fail_copy)
        except OSError:
            pass
        else:
            raise AssertionError("partial copy was accepted")
        assert_true(task.status != "done" and ws.exists() and not (root / "partial-store").exists(), "partial copy leaked state")

        # 5 repeated completion
        ws = root / "repeat-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"repeat")
        task = Task("repeat", ws, root / "repeat-store")
        manifest = {"schema_version": 1, "policy": "retain", "entries": [{"source_path": str(src), "filename": "x.txt"}]}
        assert_true(kernel.complete(task, manifest), "first completion failed")
        count = len(task.attachments)
        assert_true(not kernel.complete(task, {"schema_version": 1, "policy": "none", "entries": []}), "repeated completion mutated done task")
        assert_true(len(task.attachments) == count, "repeated completion duplicated attachments")

        # 6 recovery metadata
        receipt = task.recovery_metadata
        assert_true(receipt is not None and receipt["state"] == "verified", "missing recovery state")
        assert_true(receipt["cleanup_status"] == "completed", "missing cleanup recovery metadata")
        assert_true(receipt["entries"][0]["verification_status"] == "verified", "missing entry verification metadata")

    print("independent checker: 6/6 PASS")


if __name__ == "__main__":
    main()
