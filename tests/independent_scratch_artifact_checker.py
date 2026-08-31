"""Independent checker for t_90a453ed.

It intentionally does not call the maker test methods or unittest discovery;
 it reconstructs each acceptance scenario from the public prototype contract.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prototypes.scratch_artifact_guard import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    ArtifactGuardError,
    CompletionKernel,
    Task,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(path: Path) -> dict:
    return {
        "schema_version": 1,
        "policy": "retain",
        "entries": [
            {
                "source_path": str(path),
                "filename": path.name,
                "size": path.stat().st_size,
                "sha256": digest(path),
            }
        ],
    }


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def expect_guard_error(action, fragment: str) -> None:
    try:
        action()
    except ArtifactGuardError as exc:
        assert_true(fragment in str(exc), f"wrong guard error: {exc}")
    else:
        raise AssertionError(f"expected ArtifactGuardError containing {fragment!r}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        kernel = CompletionKernel(clock=lambda: 7)

        # 1 valid attachment
        ws = root / "valid-ws"; ws.mkdir()
        store = root / "valid-store"
        src = ws / "valid.txt"; src.write_bytes(b"valid")
        task = Task("valid", ws, store)
        valid = manifest(src)
        valid["entries"][0]["expected_sha256"] = digest(src)
        assert_true(kernel.complete(task, valid), "valid completion rejected")
        assert_true(task.status == "done" and not ws.exists(), "valid cleanup/state wrong")
        assert_true(len(task.attachments) == 1, "valid attachment row missing")
        assert_true(digest(Path(task.attachments[0].stored_path)) == task.attachments[0].sha256, "valid digest missing")

        # 2 missing declaration
        ws = root / "missing-ws"; ws.mkdir(); (ws / "x.txt").write_bytes(b"x")
        task = Task("missing", ws, root / "missing-store")
        expect_guard_error(lambda: kernel.complete(task, None), "declaration")
        assert_true(task.status != "done" and ws.exists(), "missing declaration removed workspace")

        # 3 hash mismatch
        ws = root / "hash-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"x")
        task = Task("hash", ws, root / "hash-store")
        bad = manifest(src); bad["entries"][0]["sha256"] = "f" * 64
        expect_guard_error(lambda: kernel.complete(task, bad), "digest mismatch")
        assert_true(task.status != "done" and ws.exists(), "hash mismatch removed workspace")

        # 4 invalid digest format
        ws = root / "format-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"x")
        task = Task("format", ws, root / "format-store")
        bad = manifest(src); bad["entries"][0]["sha256"] = "not-a-digest"
        expect_guard_error(lambda: kernel.complete(task, bad), "64 lowercase")
        assert_true(task.status != "done" and ws.exists(), "invalid digest removed workspace")

        # 5 oversize declaration
        ws = root / "size-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"x")
        task = Task("size", ws, root / "size-store")
        bad = manifest(src); bad["entries"][0]["size"] = MAX_ARTIFACT_BYTES + 1
        expect_guard_error(lambda: kernel.complete(task, bad), "exceeds maximum")
        assert_true(task.status != "done" and ws.exists(), "oversize declaration removed workspace")

        # 6 partial copy
        ws = root / "partial-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"partial")
        task = Task("partial", ws, root / "partial-store")

        def fail_copy(source: Path, destination: Path) -> None:
            destination.write_bytes(source.read_bytes()[:2])
            raise OSError("checker injected partial copy")

        try:
            kernel.complete(task, manifest(src), copier=fail_copy)
        except OSError:
            pass
        else:
            raise AssertionError("partial copy was accepted")
        assert_true(task.status != "done" and ws.exists() and not (root / "partial-store").exists(), "partial copy leaked state")

        # 7 source mutation after the copy
        ws = root / "mutation-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"before")
        task = Task("mutation", ws, root / "mutation-store")

        def mutate_copy(source: Path, destination: Path) -> None:
            shutil.copyfile(source, destination)
            source.write_bytes(b"after")

        expect_guard_error(lambda: kernel.complete(task, manifest(src), copier=mutate_copy), "source mutated")
        assert_true(task.status != "done" and ws.exists(), "source mutation was accepted")

        # 8 repeated completion
        ws = root / "repeat-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"repeat")
        task = Task("repeat", ws, root / "repeat-store")
        assert_true(kernel.complete(task, manifest(src)), "first completion failed")
        count = len(task.attachments)
        assert_true(not kernel.complete(task, {"schema_version": 1, "policy": "none", "entries": []}), "repeat mutated done task")
        assert_true(len(task.attachments) == count, "repeat duplicated attachments")

        # 9 cleanup failure and recovery metadata
        ws = root / "cleanup-ws"; ws.mkdir(); src = ws / "x.txt"; src.write_bytes(b"deferred")
        task = Task("cleanup", ws, root / "cleanup-store")

        def fail_cleanup(_workspace: Path) -> None:
            raise OSError("checker injected cleanup failure")

        assert_true(kernel.complete(task, manifest(src), cleaner=fail_cleanup), "cleanup-failure commit rejected")
        assert_true(task.status == "done" and ws.exists(), "cleanup failure changed committed state")
        assert_true(task.recovery_metadata["cleanup_status"] == "deferred", "deferred status missing")  # type: ignore[index]
        receipt = root / "cleanup-store" / "cleanup" / "recovery.json"
        assert_true(receipt.exists(), "recovery receipt missing")
        assert_true("deferred" in receipt.read_text(encoding="utf-8"), "deferred receipt not persisted")

    print("independent checker: 9/9 PASS")


if __name__ == "__main__":
    main()
