"""Focused stdlib tests for scripts/backup_generation_verify.py."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from typing import Any


SCRIPT = Path(__file__).resolve().parents[1] / "backup_generation_verify.py"
MANIFEST_NAME = "backup-generation-manifest.json"


class BackupGenerationVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.generation = self.root / "generation"
        self.generation.mkdir()
        self.active_boards = self.root / "active-boards.json"
        self.boards: dict[str, dict[str, Any]] = {
            "alpha": {"state": "active", "owner": "ops"},
            "beta": {"state": "active", "priority": 2},
            "retired": {"state": "dormant", "reason": "paused"},
            "quarantined": {"state": "denied", "reason": "policy"},
        }
        self._write_active_boards(self.boards)
        for slug, board in self.boards.items():
            if board["state"] == "active":
                self._write_database(self.generation / f"kanban-{slug}.db", slug)
        self._write_archive(self.generation / "archives/state.tar.gz", {"state/config.json": b'{"ok":true}\n'})
        notes = self.generation / "notes.txt"
        notes.write_text("fixture metadata\n", encoding="utf-8")
        self.assertEqual(self._run("generate").returncode, 0)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_active_boards(self, boards: Any) -> None:
        self.active_boards.write_text(
            json.dumps({"version": 1, "boards": boards}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_database(path: Path, board_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, board TEXT NOT NULL)")
            connection.execute("INSERT INTO cards (board) VALUES (?)", (board_id,))

    @staticmethod
    def _write_archive(path: Path, members: dict[str, bytes]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w:gz") as archive:
            for name, payload in sorted(members.items()):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mtime = 0
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(payload))

    def _run(self, command: str, *extra: str) -> subprocess.CompletedProcess[str]:
        arguments = [
            sys.executable,
            str(SCRIPT),
            command,
            str(self.generation),
            "--active-boards-manifest",
            str(self.active_boards),
            *extra,
        ]
        return subprocess.run(arguments, capture_output=True, check=False, text=True)

    def _manifest(self) -> dict[str, object]:
        return json.loads((self.generation / MANIFEST_NAME).read_text(encoding="utf-8"))

    def _write_manifest(self, manifest: dict[str, object]) -> None:
        (self.generation / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _refresh_artifact_record(self, relative: str) -> None:
        manifest = self._manifest()
        path = self.generation / relative
        payload = path.read_bytes()
        for artifact in manifest["artifacts"]:  # type: ignore[index]
            if artifact["path"] == relative:
                artifact["size"] = len(payload)
                artifact["sha256"] = hashlib.sha256(payload).hexdigest()
                break
        else:
            self.fail(f"artifact missing from test manifest: {relative}")
        self._write_manifest(manifest)

    def test_complete_generation_is_deterministic_and_restores_in_isolation(self) -> None:
        first_manifest = (self.generation / MANIFEST_NAME).read_bytes()
        regenerated = self._run("generate")
        self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
        self.assertEqual((self.generation / MANIFEST_NAME).read_bytes(), first_manifest)

        verified = self._run("verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        destination = self.root / "restore"
        destination.mkdir()
        restored = self._run("restore", "--destination", str(destination))
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(
            (destination / "extracted/archives/state.tar.gz/state/config.json").read_bytes(),
            b'{"ok":true}\n',
        )
        with sqlite3.connect(destination / "generation/kanban-alpha.db") as connection:
            self.assertEqual(connection.execute("SELECT board FROM cards").fetchone(), ("alpha",))

    def test_missing_artifact_is_rejected(self) -> None:
        (self.generation / "notes.txt").unlink()
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact set mismatch", result.stderr)

    def test_generate_discovers_and_requires_each_active_board_snapshot(self) -> None:
        boards = dict(self.boards)
        boards["gamma"] = {"state": "active", "owner": "ops"}
        self._write_active_boards(boards)
        result = self._run("generate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing board snapshots", result.stderr)

    def test_dormant_and_denied_boards_are_not_required_or_covered(self) -> None:
        result = self._run("generate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._manifest()["active_boards"]["boards"],  # type: ignore[index]
            [
                {"slug": "alpha", "snapshot": "kanban-alpha.db"},
                {"slug": "beta", "snapshot": "kanban-beta.db"},
            ],
        )

    def test_manifest_requires_canonical_shape_and_known_lifecycle_states(self) -> None:
        invalid_manifests = [
            ({"version": 1, "boards": []}, "boards must be an object"),
            ({"version": 1, "boards": {"alpha": {}}}, "missing required state"),
            ({"version": 1, "boards": {"alpha": {"state": "archived"}}}, "unknown board state"),
            ({"version": 1, "boards": {"alpha": {"state": True}}}, "unknown board state"),
        ]
        for document, message in invalid_manifests:
            with self.subTest(document=document):
                self.active_boards.write_text(json.dumps(document) + "\n", encoding="utf-8")
                result = self._run("generate")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_manifest_requires_version(self) -> None:
        self.active_boards.write_text(
            json.dumps({"boards": self.boards}) + "\n",
            encoding="utf-8",
        )
        result = self._run("generate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing keys ['version']", result.stderr)

    def test_manifest_accepts_top_level_metadata(self) -> None:
        document = {
            "version": 1,
            "boards": self.boards,
            "updated": "2026-09-06T12:00:00Z",
            "generated_at": "2026-09-06T12:00:00Z",
            "generator": "fleet-board-registry",
            "generated_from": "fleet/boards.yaml",
            "doc": "Canonical fleet boards manifest",
            "states": ["active", "dormant", "denied"],
        }
        self.active_boards.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = self._run("generate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._manifest()["active_boards"]["boards"],  # type: ignore[index]
            [
                {"slug": "alpha", "snapshot": "kanban-alpha.db"},
                {"slug": "beta", "snapshot": "kanban-beta.db"},
            ],
        )

    def test_invalid_board_slugs_are_rejected_without_normalization(self) -> None:
        invalid_slugs = ["", "Alpha", "-alpha", "_alpha", "alpha/beta", "alpha.beta", "a" * 65]
        for slug in invalid_slugs:
            with self.subTest(slug=slug):
                self._write_active_boards({slug: {"state": "active"}})
                result = self._run("generate")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("board slug must be", result.stderr)

    def test_duplicate_board_slug_in_source_json_is_rejected(self) -> None:
        self.active_boards.write_text(
            '{"version":1,"boards":{"alpha":{"state":"active"},'
            '"alpha":{"state":"dormant"}}}\n',
            encoding="utf-8",
        )
        result = self._run("generate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate JSON key: alpha", result.stderr)

    def test_duplicate_and_impossible_generation_coverage_are_rejected(self) -> None:
        manifest = self._manifest()
        coverage = manifest["active_boards"]["boards"]  # type: ignore[index]
        coverage.append(dict(coverage[0]))
        self._write_manifest(manifest)
        duplicate = self._run("verify")
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("duplicate board slug or snapshot", duplicate.stderr)

        self.assertEqual(self._run("generate").returncode, 0)
        manifest = self._manifest()
        manifest["active_boards"]["boards"][0]["snapshot"] = "kanban-not-alpha.db"  # type: ignore[index]
        self._write_manifest(manifest)
        impossible = self._run("verify")
        self.assertNotEqual(impossible.returncode, 0)
        self.assertIn("does not match derived path", impossible.stderr)

    def test_same_size_corruption_is_rejected_by_hash(self) -> None:
        path = self.generation / "notes.txt"
        payload = bytearray(path.read_bytes())
        payload[0] ^= 1
        path.write_bytes(payload)
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch", result.stderr)

    def test_size_mismatch_and_zero_byte_are_rejected(self) -> None:
        manifest = self._manifest()
        manifest["artifacts"][0]["size"] += 1  # type: ignore[index]
        self._write_manifest(manifest)
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("size mismatch", result.stderr)

        self.assertEqual(self._run("generate").returncode, 0)
        (self.generation / "notes.txt").write_bytes(b"")
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("zero-byte", result.stderr)

    def test_symlinked_artifact_is_rejected(self) -> None:
        path = self.generation / "notes.txt"
        path.unlink()
        try:
            path.symlink_to(self.active_boards)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked artifact", result.stderr)

    def test_truncated_archive_is_rejected_even_when_manifest_hash_matches(self) -> None:
        relative = "archives/state.tar.gz"
        archive = self.generation / relative
        archive.write_bytes(archive.read_bytes()[:-8])
        self._refresh_artifact_record(relative)
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("truncated tar.gz", result.stderr)

    def test_stale_and_inconsistent_board_coverage_are_rejected(self) -> None:
        original_active = self.active_boards.read_bytes()
        changed_boards = dict(self.boards)
        changed_boards["gamma"] = {"state": "active"}
        self._write_active_boards(changed_boards)
        stale = self._run("verify")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale board coverage", stale.stderr)

        self.active_boards.write_bytes(original_active)
        manifest = self._manifest()
        manifest["active_boards"]["boards"].pop()  # type: ignore[index]
        self._write_manifest(manifest)
        inconsistent = self._run("verify")
        self.assertNotEqual(inconsistent.returncode, 0)
        self.assertIn("inconsistent board coverage", inconsistent.stderr)

    def test_unsafe_archive_member_is_rejected_before_restore_writes(self) -> None:
        relative = "archives/state.tar.gz"
        self._write_archive(self.generation / relative, {"../escape.txt": b"escaped\n"})
        self._refresh_artifact_record(relative)
        destination = self.root / "restore"
        destination.mkdir()
        result = self._run("restore", "--destination", str(destination))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe archive member", result.stderr)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse((self.root / "escape.txt").exists())

    def test_corrupt_sqlite_is_rejected_even_when_manifest_hash_matches(self) -> None:
        relative = "kanban-alpha.db"
        database = self.generation / relative
        database.write_bytes(b"not sqlite" * 100)
        self._refresh_artifact_record(relative)
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQLite integrity check failed", result.stderr)

    def test_malformed_generation_manifest_is_rejected(self) -> None:
        (self.generation / MANIFEST_NAME).write_text('{"schema_version": 1,', encoding="utf-8")
        result = self._run("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("malformed generation manifest", result.stderr)


if __name__ == "__main__":
    unittest.main()
