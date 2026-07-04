"""Tests that Honcho context retrieval errors are surfaced (not silently swallowed).

Verifies that all context-fetch paths log at ERROR or WARNING level
(with context_id, operation, and exception text) instead of silent logger.debug.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from plugins.memory.honcho.session import HonchoSession, HonchoSessionManager


class _ErroringHonchoSession:
    """A mock Honcho session whose context() always raises."""

    def __init__(self, fail_all: bool = False):
        self._fail_all = fail_all
        self.calls = []

    def context(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("simulated Honcho backend failure: context() timeout")

    def add_peers(self, *args, **kwargs):
        pass

    def get_peer_configuration(self, *args, **kwargs):
        if self._fail_all:
            raise RuntimeError("simulated Honcho backend failure: get_peer_configuration")
        return SimpleNamespace(observe_me=None, observe_others=None)


class _FailingPeer:
    """A mock Honcho peer whose every call raises."""

    def context(self, **kwargs):
        raise RuntimeError("simulated peer.context() failure")

    def representation(self, **kwargs):
        raise RuntimeError("simulated peer.representation() failure")

    def get_card(self, **kwargs):
        raise RuntimeError("simulated peer.get_card() failure")


def _make_manager() -> HonchoSessionManager:
    """Build a manager with a minimal config."""
    cfg = SimpleNamespace(
        write_frequency="turn",
        dialectic_reasoning_level="low",
        dialectic_dynamic=True,
        dialectic_max_chars=600,
        observation_mode="directional",
        user_observe_me=True,
        user_observe_others=True,
        ai_observe_me=True,
        ai_observe_others=True,
        message_max_chars=25000,
        dialectic_max_input_chars=10000,
    )
    return HonchoSessionManager(honcho=SimpleNamespace(), config=cfg)


def _cached_failing_manager() -> tuple[HonchoSessionManager, _ErroringHonchoSession]:
    """Build a manager with a cached session whose context() always raises."""
    mgr = _make_manager()
    fake_honcho_session = _ErroringHonchoSession()
    session = HonchoSession(
        key="test-fail",
        user_peer_id="user-fail",
        assistant_peer_id="hermes-fail",
        honcho_session_id="test-fail-session",
    )
    mgr._cache[session.key] = session
    mgr._sessions_cache[session.honcho_session_id] = fake_honcho_session
    return mgr, fake_honcho_session


# ====== get_session_context ======

def test_get_session_context_errors_at_error_level(caplog):
    """get_session_context must log ERROR with context_id, operation, and exception text."""
    caplog.set_level(logging.DEBUG)
    mgr, fake = _cached_failing_manager()

    result = mgr.get_session_context("test-fail", peer="user")

    # Result must be empty (fail-soft)
    assert result == {}, "Must return empty dict on failure (fail-soft)"

    # Must have an ERROR log with the failing session key and the operation name
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "Must produce at least one ERROR-level log"

    match = error_records[0]
    assert "test-fail" in match.message, "ERROR must include session_key/context_id"
    assert "get_session_context" in match.message, "ERROR must include function name"
    assert "context(" in match.message or "honcho_session" in match.message, \
        "ERROR must reference the failing Honcho API call"
    assert "simulated" in match.message or "timeout" in match.message, \
        "ERROR must include exception text"


def test_get_session_context_fallback_without_honcho_session(caplog):
    """When honcho_session is not cached, get_session_context falls back gracefully."""
    caplog.set_level(logging.DEBUG)
    mgr, fake = _cached_failing_manager()

    # Remove the honcho_session from cache so it hits the fallback peer-context path.
    mgr._sessions_cache.pop("test-fail-session", None)

    # Inject a failing peer so the fallback _fetch_peer_context doesn't crash
    mgr._peers_cache["user-fail"] = _FailingPeer()

    result = mgr.get_session_context("test-fail", peer="user")

    # Fallback returns empty representation+card (fail-soft), but its callers
    # (handle_tool_call honcho_context) treat empty dict the same way.
    assert isinstance(result, dict), "Must return a dict on fallback"
    assert not result.get("representation", ""), "Must not have representation on failure"
    assert not result.get("card", ""), "Must not have card on failure"


# ====== get_prefetch_context ======

def test_get_prefetch_context_summary_errors_at_error_level(caplog):
    """get_prefetch_context summary fetch must log ERROR with context_id and operation."""
    caplog.set_level(logging.DEBUG)
    mgr, fake = _cached_failing_manager()

    result = mgr.get_prefetch_context("test-fail", user_message="hello")

    # Must not crash
    assert isinstance(result, dict), "Must return a dict"

    # Summary must be absent (failed fetch)
    assert "summary" not in result or not result.get("summary"), \
        "summary should be absent when context() fails"

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("session summary" in r.message and "test-fail" in r.message
               for r in error_records), \
        "Must produce ERROR log mentioning 'session summary' and the context_id"


def test_get_prefetch_context_ai_peer_errors_at_error_level(caplog):
    """get_prefetch_context AI peer fetch must log ERROR with context_id."""
    caplog.set_level(logging.DEBUG)
    mgr, fake = _cached_failing_manager()

    result = mgr.get_prefetch_context("test-fail")

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("AI peer" in r.message and "hermes-fail" in r.message
               for r in error_records), \
        "Must produce ERROR log mentioning 'AI peer' and the assistant peer ID"


# ====== _fetch_peer_context ======

def test_fetch_peer_context_warning_on_failure(caplog):
    """_fetch_peer_context must log WARNING with peer_id and call details."""
    caplog.set_level(logging.DEBUG)
    mgr = _make_manager()

    # Inject a failing peer into the cache so _get_or_create_peer returns it
    mgr._peers_cache["user-fail"] = _FailingPeer()

    result = mgr._fetch_peer_context("user-fail")

    assert result == {"representation": "", "card": []}, \
        "Must return empty result on failure (fail-soft)"

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("peer.context()" in r.message and "user-fail" in r.message
               for r in warning_records), \
        "Must include WARNING with peer_id and call name"


# ====== get_peer_card ======

def test_get_peer_card_warning_on_failure(caplog):
    """get_peer_card must log WARNING with session_key on failure."""
    caplog.set_level(logging.DEBUG)
    mgr, fake = _cached_failing_manager()

    result = mgr.get_peer_card("test-fail", peer="user")

    assert result == [], "Must return empty list on failure"

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("test-fail" in r.message and "peer card" in r.message.lower()
               for r in warning_records), \
        "Must produce WARNING mentioning session_key and peer card"


# ====== search_context ======

def test_search_context_warning_on_failure(caplog):
    """search_context must log WARNING with session_key and query on failure."""
    caplog.set_level(logging.DEBUG)
    mgr, fake = _cached_failing_manager()

    result = mgr.search_context("test-fail", "some query")

    assert result == "", "Must return empty string on failure"

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("test-fail" in r.message and "search_context" in r.message
               for r in warning_records), \
        "Must produce WARNING mentioning session_key and search_context"


# ====== get_ai_representation ======

def test_get_ai_representation_warning_on_failure(caplog):
    """get_ai_representation must log WARNING with session_key on failure."""
    caplog.set_level(logging.DEBUG)
    mgr, fake = _cached_failing_manager()

    result = mgr.get_ai_representation("test-fail")

    assert result == {"representation": "", "card": ""}, \
        "Must return empty dict on failure"

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("test-fail" in r.message and "AI representation" in r.message
               for r in warning_records), \
        "Must produce WARNING mentioning session_key and AI representation"
