#!/usr/bin/env python3
"""Unit tests for jarvis-buzz-mention-router (no network, no key read)."""
import importlib.util
from pathlib import Path

P = Path("/home/frank/.hermes/scripts/jarvis-buzz-mention-router.py")
spec = importlib.util.spec_from_file_location("jbmr", P)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HERMES = "b" * 64
GROK = "a" * 64


def test_mentions():
    ev = {"tags": [["p", HERMES], ["h", "ch"]]}
    assert mod._mentions_hermes(ev, HERMES)
    assert not mod._mentions_hermes({"tags": [["p", GROK]]}, HERMES)


def test_replied():
    parent = "1" * 64
    events = [
        {"pubkey": HERMES, "tags": [["e", parent, "", "reply"]]},
    ]
    assert mod._hermes_replied(events, parent, HERMES)
    assert not mod._hermes_replied(events, "2" * 64, HERMES)


if __name__ == "__main__":
    test_mentions()
    test_replied()
    print("test_jarvis_buzz_mention_router OK")
