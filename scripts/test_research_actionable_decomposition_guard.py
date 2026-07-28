#!/usr/bin/env python3
"""Regression coverage for sycode-trading/t_1243d100.

The guard prevents automated research-actionable decomposers from creating one
child card per copied markdown bullet/specification line. Legitimate cards must
be grouped independent workstreams with owner, acceptance criteria, and gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kanban_create_guard_blocks_bullet_only_research_actionable() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    reason = guard.research_actionable_block_reason(
        "RESEARCH-ACTIONABLE: sycode-trading/t_deadbeef — 3. JWT token requirement",
        "Copied bullet from parent task.",
        "trading-devops",
    )

    assert reason is not None
    assert "one card per bullet" in reason
    assert "distinct owner, acceptance test, and gate" in reason


def test_kanban_create_guard_allows_grouped_independent_workstream() -> None:
    guard = load_module("kanban_dedupe_guard", ROOT / "scripts" / "kanban_dedupe_guard.py")

    reason = guard.research_actionable_block_reason(
        "RESEARCH-ACTIONABLE: sycode-trading/t_deadbeef — grouped data-quality repair digest",
        """
        Owner: trading-devops
        Acceptance criteria: focused dry-run demonstrates duplicate rows are grouped and no single bullet child is emitted.
        Gate class: A1, source/test only; no live trading and no credentials.
        """,
        "trading-devops",
    )

    assert reason is None


def test_gap_analyzer_filters_markdown_fragments_and_keeps_imperative_actions() -> None:
    analyzer = load_module("research_impl_gap_analyzer", ROOT / "scripts" / "research-impl-gap-analyzer.py")

    is_fp, real_actions = analyzer.has_false_positive_actions(
        ["| Field | Value |", "3. JWT token requirement", "and route to review"]
    )
    assert is_fp is True
    assert real_actions == []

    is_fp, real_actions = analyzer.has_false_positive_actions(
        ["Implement grouped research-actionable digest creation with a dry-run regression"]
    )
    assert is_fp is False
    assert real_actions == ["Implement grouped research-actionable digest creation with a dry-run regression"]


def test_child_creator_parses_real_actions_as_one_grouped_digest(tmp_path, monkeypatch) -> None:
    creator = load_module("research_impl_child_creator", ROOT / "scripts" / "research-impl-child-creator.py")
    report = tmp_path / "research-impl-gap-analysis.md"
    report.write_text(
        """
### 1. 🆕 sycode-trading/t_deadbeef — Parent task title

| Field | Value |
|---|---|
| Board | `sycode-trading` |
| Task | `t_deadbeef` |
| Assignee | trading-devops |
| Classification | **real_gap** |
| Confidence | 80% |
| Reason | 2 independently actionable item(s) found; group into one digest child for the source task |

**Real actions identified:**
- `Implement source repair with fixture-backed test`
- `Verify dry-run output groups duplicate bullets`

""".strip()
    )
    monkeypatch.setattr(creator, "GAP_ANALYSIS_PATH", report)

    items = creator.parse_gap_analysis()

    assert len(items) == 1
    assert items[0]["actions"] == [
        "Implement source repair with fixture-backed test",
        "Verify dry-run output groups duplicate bullets",
    ]
