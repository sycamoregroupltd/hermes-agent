from types import SimpleNamespace

import pytest

import cli


class _Console:
    def print(self, *_args, **_kwargs):
        pass


def _make_fake_cli(monkeypatch, result):
    """Build a FakeCLI mirroring test_human_single_query_main_finalizes_after_query,
    with `chat()` populating `_last_turn_result` the way `_chat_settle_turn` does."""
    calls = []

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = _Console()
            self.session_id = "single-query-session"
            self._last_turn_result = None
            self.agent = SimpleNamespace(
                session_id="single-query-session",
                platform="cli",
            )

        def _claim_active_session(self, surface, *, stderr=False):
            calls.append(("claim", surface, stderr))
            return True

        def _show_security_advisories(self):
            calls.append("advisories")

        def chat(self, query, images=None):
            calls.append(("chat", query, images))
            self._last_turn_result = result
            return "done"

        def _print_exit_summary(self, clear_screen=True):
            calls.append("summary")

    monkeypatch.setattr(cli, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "_finalize_single_query",
        lambda fake_cli: calls.append(("finalize", fake_cli.session_id)),
    )
    return calls


def test_nonquiet_kanban_rate_limit_exits_with_sentinel(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    calls = _make_fake_cli(
        monkeypatch,
        {"final_response": "", "failed": True, "failure_reason": "rate_limit"},
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(query="hello", quiet=False, toolsets="terminal")

    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
    assert exc_info.value.code == KANBAN_RATE_LIMIT_EXIT_CODE
    # finally: block still runs _finalize_single_query even though sys.exit() fired.
    assert ("finalize", "single-query-session") in calls


def test_nonquiet_kanban_billing_exits_with_sentinel(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    calls = _make_fake_cli(
        monkeypatch,
        {"final_response": "", "failed": True, "failure_reason": "billing"},
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(query="hello", quiet=False, toolsets="terminal")

    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
    assert exc_info.value.code == KANBAN_RATE_LIMIT_EXIT_CODE
    assert ("finalize", "single-query-session") in calls


def test_nonquiet_kanban_other_failure_exits_1(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    calls = _make_fake_cli(
        monkeypatch,
        {"final_response": "", "failed": True, "failure_reason": "protocol_violation"},
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(query="hello", quiet=False, toolsets="terminal")

    assert exc_info.value.code == 1
    assert ("finalize", "single-query-session") in calls


def test_nonquiet_kanban_success_no_exit(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    calls = _make_fake_cli(
        monkeypatch,
        {"final_response": "all good", "failed": False},
    )

    # No SystemExit: main() returns normally, falling through to implicit exit 0.
    cli.main(query="hello", quiet=False, toolsets="terminal")

    assert ("finalize", "single-query-session") in calls


def test_nonquiet_human_path_rate_limit_no_exit(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    calls = _make_fake_cli(
        monkeypatch,
        {"final_response": "", "failed": True, "failure_reason": "rate_limit"},
    )

    # HERMES_KANBAN_TASK unset -> human `-q` path unaffected, no SystemExit.
    cli.main(query="hello", quiet=False, toolsets="terminal")

    assert ("finalize", "single-query-session") in calls
