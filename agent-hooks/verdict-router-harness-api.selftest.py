#!/usr/bin/env python3
"""Importable API self-test for verdict-router-harness.py.

This exercises only local fixture data and isolated router-script temp DBs. It must
not read or mutate live kanban boards.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "agent-hooks" / "verdict-router-harness.py"
ROUTER_PATH = ROOT / "scripts" / "verdict_router.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("verdict_router_harness", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load harness from {HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assert_common_surface(result: dict) -> dict:
    # Surface diagnostics before asserting so a script/module divergence is
    # debuggable instead of a bare AssertionError (AC1: the prior assertion
    # discarded the whole result dict).
    if not result.get("ok"):
        print("assert_common_surface FAILED:")
        print("  ok:", result.get("ok"))
        print("  errors:", result.get("errors"))
        print("  implementation:", result.get("implementation"))
        print("  results[0].errors:", (result.get("results") or [{}])[0].get("errors"))
    assert result["ok"] is True
    assert result["live_side_effects_possible"] is False
    assert result["errors"] == []
    assert len(result["results"]) == 1
    item = result["results"][0]
    for key in (
        "parsed_verdict",
        "safety_classification",
        "planned_mutations",
        "comments",
        "unblock_actions",
        "completion_actions",
        "ignored_noop_results",
        "errors",
    ):
        assert key in item, f"missing structured field {key}"
    return item


def main() -> int:
    harness = load_harness()
    comments = [
        {
            "id": 501,
            "author": "os-reviewer",
            "created_at": 1783110501,
            "body": "REVIEW_VERDICT=APPROVED\nTarget: jarvis-os/t_a1b2c301\nSource-only harness API approved.",
        }
    ]

    dry = harness.run_harness(
        board="jarvis-os",
        task={
            "id": "t_a1b2c301",
            "status": "blocked",
            "title": "Source-only harness API card",
            "body": "Source docs and tests only.",
            "block_reason": "review-required",
        },
        comments=comments,
        mode="dry-run",
    )
    dry_item = assert_common_surface(dry)
    assert dry["mode"] == "dry-run"
    assert dry_item["parsed_verdict"]["value"] == "APPROVED"
    assert dry_item["safety_classification"] == "source_docs_spec_test_only"
    assert dry_item["planned_mutations"] == ["complete"]
    assert dry_item["completion_actions"][0]["task_id"] == "t_a1b2c301"
    assert dry_item["comments"] == []
    assert dry_item["unblock_actions"] == []

    planned = harness.run_harness(
        board="jarvis-os",
        task={
            "id": "t_a1b2c302",
            "status": "blocked",
            "title": "Source-only harness API rework card",
            "body": "Source docs and tests only.",
            "block_reason": "review-required",
        },
        comments=[
            {
                "id": 502,
                "author": "os-reviewer",
                "created_at": 1783110502,
                "body": "REVIEW_VERDICT=CHANGES_REQUESTED\nTarget: t_a1b2c302\nBlocking finding: add the isolated API surface.",
            }
        ],
        mode="mutation-plan",
    )
    planned_item = assert_common_surface(planned)
    assert planned["mode"] == "mutation-plan"
    assert planned_item["planned_mutations"] == ["comment", "unblock"]
    assert "isolated API surface" in planned_item["comments"][0]["body"]
    assert planned_item["unblock_actions"][0]["task_id"] == "t_a1b2c302"

    script = harness.run_harness(
        board="jarvis-os",
        task={
            "id": "t_a1b2c303",
            "status": "blocked",
            "title": "Source-only router script card",
            "body": "Source docs and tests only.",
            "block_reason": "review-required",
        },
        comments=[
            {
                "id": 503,
                "author": "os-reviewer",
                "created_at": 1783110503,
                "body": "REVIEW_VERDICT=APPROVED\nTarget: t_a1b2c303\nSource-only router script approved.",
            }
        ],
        mode="dry-run",
        router_script=str(ROUTER_PATH),
    )
    script_item = assert_common_surface(script)
    assert script["implementation"] == "router-script"
    assert script_item["planned_mutations"] == ["complete"]
    assert script_item["completion_actions"][0]["task_id"] == "t_a1b2c303"
    print("VERDICT_ROUTER_HARNESS_API_SELFTEST_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
