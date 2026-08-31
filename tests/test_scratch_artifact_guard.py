from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

# Keep the documented direct-script command runnable from the repository root.
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prototypes.scratch_artifact_guard import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    ArtifactGuardError,
    CompletionKernel,
    Task,
)


class ScratchArtifactGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "workspace"
        self.store = root / "attachments"
        self.ws.mkdir()
        self.kernel = CompletionKernel(clock=lambda: 42)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def task(self) -> Task:
        return Task("t_test", self.ws, self.store)

    def manifest(self, path: Path, expected: str | None = None) -> dict:
        payload = path.read_bytes()
        entry = {
            "source_path": str(path),
            "filename": path.name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if expected is not None:
            entry["expected_sha256"] = expected
        return {"schema_version": 1, "policy": "retain", "entries": [entry]}

    def test_complete_with_valid_attachment(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"verified report")
        task = self.task()
        self.assertTrue(self.kernel.complete(task, self.manifest(source)))
        self.assertEqual(task.status, "done")
        self.assertFalse(self.ws.exists())
        self.assertEqual(len(task.attachments), 1)
        saved = Path(task.attachments[0].stored_path)
        self.assertEqual(saved.read_bytes(), b"verified report")
        self.assertEqual(task.attachments[0].sha256, hashlib.sha256(saved.read_bytes()).hexdigest())

    def test_explicit_none_completion_is_valid_and_cleans_up(self) -> None:
        source = self.ws / "working-note.txt"
        source.write_bytes(b"intentionally no deliverable")
        task = self.task()
        manifest = {"schema_version": 1, "policy": "none", "entries": []}
        self.assertTrue(self.kernel.complete(task, manifest))
        self.assertEqual(task.status, "done")
        self.assertFalse(self.ws.exists())
        self.assertEqual(task.attachments, [])
        self.assertEqual(task.recovery_metadata["policy"], "none")  # type: ignore[index]
        self.assertEqual(task.recovery_metadata["cleanup_status"], "completed")  # type: ignore[index]
        receipt_file = self.store / task.task_id / "recovery.json"
        self.assertTrue(receipt_file.exists())
        self.assertEqual(json.loads(receipt_file.read_text(encoding="utf-8"))["entries"], [])

    def test_missing_attachment_declaration_fails_closed(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"must not vanish")
        task = self.task()
        with self.assertRaisesRegex(ArtifactGuardError, "declaration"):
            self.kernel.complete(task, None)
        self.assertEqual(task.status, "running")
        self.assertTrue(source.exists())
        self.assertEqual(task.attachments, [])

    def test_hash_mismatch_fails_closed(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"original")
        task = self.task()
        bad = self.manifest(source)
        bad["entries"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ArtifactGuardError, "digest mismatch"):
            self.kernel.complete(task, bad)
        self.assertEqual(task.status, "running")
        self.assertTrue(self.ws.exists())
        self.assertFalse(self.store.exists())

    def test_expected_digest_mismatch_fails_closed(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"expected digest assertion")
        task = self.task()
        bad = self.manifest(source, expected="0" * 64)
        with self.assertRaisesRegex(ArtifactGuardError, "expected digest mismatch"):
            self.kernel.complete(task, bad)
        self.assertEqual(task.status, "running")
        self.assertTrue(source.exists())
        self.assertFalse(self.store.exists())

    def test_invalid_digest_format_fails_closed(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"bad digest format")
        task = self.task()
        bad = self.manifest(source)
        bad["entries"][0]["sha256"] = "not-a-sha256"
        with self.assertRaisesRegex(ArtifactGuardError, "64 lowercase"):
            self.kernel.complete(task, bad)
        self.assertEqual(task.status, "running")
        self.assertTrue(source.exists())

    def test_oversize_declaration_fails_closed(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"small source")
        task = self.task()
        bad = self.manifest(source)
        bad["entries"][0]["size"] = MAX_ARTIFACT_BYTES + 1
        with self.assertRaisesRegex(ArtifactGuardError, "exceeds maximum"):
            self.kernel.complete(task, bad)
        self.assertEqual(task.status, "running")
        self.assertTrue(source.exists())
        self.assertFalse(self.store.exists())

    def test_partial_copy_leaves_no_final_attachment(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"partial copy must roll back")
        task = self.task()

        def failing_copy(src: Path, dst: Path) -> None:
            dst.write_bytes(src.read_bytes()[:5])
            raise OSError("injected copy failure")

        with self.assertRaises(OSError):
            self.kernel.complete(task, self.manifest(source), copier=failing_copy)
        self.assertEqual(task.status, "running")
        self.assertTrue(self.ws.exists())
        self.assertFalse(self.store.exists())

    def test_source_mutation_during_staging_fails_closed(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"before staging")
        task = self.task()

        def mutating_copy(src: Path, dst: Path) -> None:
            shutil.copyfile(src, dst)
            src.write_bytes(b"mutated after copy")

        with self.assertRaisesRegex(ArtifactGuardError, "source mutated"):
            self.kernel.complete(task, self.manifest(source), copier=mutating_copy)
        self.assertEqual(task.status, "running")
        self.assertTrue(source.exists())
        self.assertFalse(self.store.exists())

    def test_repeated_completion_is_idempotent(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"one completion")
        task = self.task()
        self.assertTrue(self.kernel.complete(task, self.manifest(source)))
        stored = list(task.attachments)
        self.assertFalse(self.kernel.complete(task, {"schema_version": 1, "policy": "none", "entries": []}))
        self.assertEqual(task.attachments, stored)

    def test_cleanup_failure_records_deferred_recovery(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"cleanup can be retried")
        task = self.task()

        def failing_cleanup(_workspace: Path) -> None:
            raise OSError("injected cleanup failure")

        self.assertTrue(self.kernel.complete(task, self.manifest(source), cleaner=failing_cleanup))
        self.assertEqual(task.status, "done")
        self.assertTrue(self.ws.exists())
        self.assertEqual(task.recovery_metadata["cleanup_status"], "deferred")  # type: ignore[index]
        self.assertEqual(task.recovery_metadata["cleanup_error_type"], "OSError")  # type: ignore[index]
        receipt_file = self.store / task.task_id / "recovery.json"
        self.assertEqual(json.loads(receipt_file.read_text(encoding="utf-8"))["cleanup_status"], "deferred")
        self.assertEqual(len(task.attachments), 1)

    def test_receipt_write_failure_is_deferred_after_commit(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"receipt update can be retried")
        task = self.task()
        writes = 0

        def fail_final_receipt(path: Path, receipt: dict) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("injected receipt write failure")
            path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

        self.assertTrue(self.kernel.complete(task, self.manifest(source), receipt_writer=fail_final_receipt))
        self.assertEqual(writes, 2)
        self.assertEqual(task.status, "done")
        self.assertFalse(self.ws.exists())
        self.assertEqual(len(task.attachments), 1)
        self.assertEqual(task.recovery_metadata["cleanup_status"], "deferred")  # type: ignore[index]
        self.assertEqual(task.recovery_metadata["cleanup_observed"], "completed")  # type: ignore[index]
        self.assertEqual(task.recovery_metadata["recovery_metadata_write"], "deferred")  # type: ignore[index]
        receipt_file = self.store / task.task_id / "recovery.json"
        self.assertEqual(json.loads(receipt_file.read_text(encoding="utf-8"))["cleanup_status"], "pending")

    def test_recovery_metadata_is_verified_and_persisted(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"recovery receipt")
        task = self.task()
        self.assertTrue(self.kernel.complete(task, self.manifest(source)))
        receipt = task.recovery_metadata
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt["state"], "verified")
        self.assertEqual(receipt["cleanup_status"], "completed")
        self.assertEqual(receipt["entries"][0]["verification_status"], "verified")
        receipt_file = self.store / task.task_id / "recovery.json"
        self.assertEqual(json.loads(receipt_file.read_text(encoding="utf-8")), receipt)


if __name__ == "__main__":
    unittest.main()
