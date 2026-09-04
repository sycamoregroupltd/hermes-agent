#!/usr/bin/env python3
"""Focused regression fixtures for SessionStart Git probes."""

from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEAT = ROOT / "scripts" / "seat-live-state.sh"
RECONCILE = ROOT / "scripts" / "reconcile-state.py"
HOOK = ROOT / "scripts" / "seat-live-state-hook.sh"


def run(*args: str, cwd: pathlib.Path | None = None) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


class SessionStartNoPrimaryFetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="sessionstart-guard-"))
        self.repo = self.tmp / "repo"
        self.remote = self.tmp / "remote.git"
        self.vault = self.tmp / "vault"
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.git_log = self.tmp / "git.log"
        self.bin.mkdir()
        self.vault.mkdir()
        self.state.mkdir()
        run("git", "init", "--bare", str(self.remote))
        self.repo.mkdir()
        run("git", "init", str(self.repo))
        run("git", "-C", str(self.repo), "config", "user.email", "fixture@example.invalid")
        run("git", "-C", str(self.repo), "config", "user.name", "fixture")
        (self.repo / "fixture.txt").write_text("fixture\n")
        run("git", "-C", str(self.repo), "add", "fixture.txt")
        self.commit = run("git", "-C", str(self.repo), "commit", "-m", "fixture")
        self.commit = run("git", "-C", str(self.repo), "rev-parse", "HEAD")
        run("git", "-C", str(self.repo), "branch", "-M", "main")
        run("git", "-C", str(self.repo), "remote", "add", "origin", str(self.remote))
        run("git", "-C", str(self.repo), "push", "origin", "main")
        run("git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", self.commit)
        (self.state / "ns-phases.tsv").write_text("P1\tACTIVE\tFixture phase\tFixture next\n")
        (self.state / "ns-phases.frontier").write_text("0\n")
        (self.vault / "STATE.md").write_text("fixture state\n")
        self._write_command("docker", "printf '%s\\n' " + repr(self.commit))
        self._write_command("curl", "printf '%s\\n' '{\"mode\":\"paper\"}'")
        self._write_command("hermes", "printf '%s\\n' 'Model: fixture' 'Provider: fixture'")
        self._write_command(
            "gh",
            "if printf '%s' \"$*\" | grep -q -- '--json'; then printf '%s\\n' '[]'; "
            "elif printf '%s' \"$*\" | grep -q -- '--state merged'; then "
            "if printf '%s' \"$*\" | grep -q -- '--jq length'; then printf '0'; "
            "else printf '%s\\n' '#1 fixture (2026-01-01)'; fi; "
            "elif printf '%s' \"$*\" | grep -q -- '--jq length'; then printf '0'; "
            "else printf '%s\\n' '(none)'; fi",
        )
        real_git = shutil.which("git")
        assert real_git
        self._write_command(
            "git",
            "printf '%s\\n' \"$*\" >> \"$FIXTURE_GIT_LOG\"; "
            "case \" $* \" in *' fetch '*) exit 97;; esac; "
            f"exec {real_git} \"$@\"",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _write_command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def _render(self, source: pathlib.Path, name: str) -> pathlib.Path:
        target = self.tmp / name
        text = source.read_text(encoding="utf-8")
        text = text.replace("/home/frank/sycode-trading", str(self.repo))
        text = text.replace("/home/frank/obsidian/sycode-trading", str(self.vault))
        text = text.replace("/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db", str(self.tmp / "kanban.db"))
        text = text.replace("/home/frank/.hermes/state", str(self.state))
        text = text.replace("/home/frank/.hermes/scripts/seat-live-state.sh", str(self.tmp / "seat.sh"))
        text = text.replace("/home/frank/.hermes/scripts/reconcile-state.py", str(self.tmp / "reconcile.py"))
        target.write_text(text)
        target.chmod(source.stat().st_mode & 0o777)
        return target

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FIXTURE_GIT_LOG"] = str(self.git_log)
        env["HERMES_HOME"] = str(self.tmp / "hermes")
        env["HERMES_STATE_DIR"] = str(self.state)
        env["SYCODE_REPO"] = str(self.repo)
        env["SYCODE_VAULT"] = str(self.vault)
        env["SYCODE_BOARD_DB"] = str(self.tmp / "kanban.db")
        return env

    def _make_board(self) -> None:
        db = sqlite3.connect(self.tmp / "kanban.db")
        db.execute("CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT, priority INTEGER, consecutive_failures INTEGER, last_failure_error TEXT, title TEXT)")
        db.execute("INSERT INTO tasks VALUES ('fixture', 'running', 'fixture', 1, 0, '', 'fixture')")
        db.commit()
        db.close()

    def test_seat_snapshot_schema_and_read_only_remote_probe(self) -> None:
        self._make_board()
        seat = self._render(SEAT, "seat.sh")
        result = subprocess.run(
            ["bash", str(seat)], env=self._environment(), text=True, capture_output=True, check=True
        )
        for field in ("DEPLOY :", "HEAD   :", "MODE   :", "BOARD  :", "PROVIDER:", "OPEN PRs:", "PR FRONTIER:", "NORTH STAR:", "FULL STATE:", "PROBES FAILED:"):
            self.assertIn(field, result.stdout)
        self.assertIn("origin/main remote-checked", result.stdout)
        git_calls = self.git_log.read_text()
        self.assertIn("ls-remote --heads origin refs/heads/main", git_calls)
        self.assertFalse(any(" fetch " in f" {line} " for line in git_calls.splitlines()))

    def test_remote_unavailable_preserves_seat_schema_and_reconcile_headline(self) -> None:
        self._make_board()
        seat = self._render(SEAT, "seat.sh")
        reconcile = self._render(RECONCILE, "reconcile.py")
        hook = self._render(HOOK, "hook.sh")
        run("git", "-C", str(self.repo), "remote", "set-url", "origin", str(self.tmp / "missing-remote"))
        seat_result = subprocess.run(
            ["bash", str(seat)], env=self._environment(), text=True, capture_output=True, check=True
        )
        self.assertIn("origin/main unavailable", seat_result.stdout)
        self.assertIn("git-ls-remote(remote unavailable)", seat_result.stdout)
        for field in ("DEPLOY :", "HEAD   :", "MODE   :", "BOARD  :", "PROBES FAILED:"):
            self.assertIn(field, seat_result.stdout)

        reconcile_result = subprocess.run(
            ["python3", str(reconcile)], env=self._environment(), text=True, capture_output=True, check=True
        )
        self.assertIn("PROBES FAILED: git-ls-remote", reconcile_result.stdout)
        state_text = (self.vault / "STATE.md").read_text()
        for section in ("## Deploy & gate", "## Work lineage", "_PROBES FAILED: git-ls-remote_"):
            self.assertIn(section, state_text)

        (self.vault / "STATE.md").unlink()
        hook_result = subprocess.run(
            ["bash", str(hook)], env=self._environment(), text=True, capture_output=True, check=True
        )
        time.sleep(0.2)
        self.assertIn("SEAT LIVE-STATE", hook_result.stdout)
        self.assertFalse(any(" fetch " in f" {line} " for line in self.git_log.read_text().splitlines()))
        self.assertTrue((self.state / "state-headline.txt").exists())


if __name__ == "__main__":
    unittest.main()
