from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from prototypes.scratch_artifact_guard import (
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
        entry = {"source_path": str(path), "filename": path.name}
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
        with self.assertRaisesRegex(ArtifactGuardError, "digest mismatch"):
            self.kernel.complete(task, self.manifest(source, "0" * 64))
        self.assertEqual(task.status, "running")
        self.assertTrue(self.ws.exists())
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

    def test_repeated_completion_is_idempotent(self) -> None:
        source = self.ws / "report.md"
        source.write_bytes(b"one completion")
        task = self.task()
        self.assertTrue(self.kernel.complete(task, self.manifest(source)))
        stored = list(task.attachments)
        self.assertFalse(self.kernel.complete(task, {"schema_version": 1, "policy": "none", "entries": []}))
        self.assertEqual(task.attachments, stored)

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
