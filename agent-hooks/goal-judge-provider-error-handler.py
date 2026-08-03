#!/usr/bin/env python3
"""Deterministic goal-judge provider-error quarantine/override lane.

This module documents and exercises the safe local control-plane behavior for
tasks whose non-terminal state is caused by external goal-judge provider
errors such as GeminiAPIError/NotFoundError during kanban_complete.

No live board, provider/model, or routing mutation is performed by this
module. It provides a machine-readable quarantine lane marker and a local
distillation/test entry so future workers can verify the intended behavior
without relying solely on prose.

Context: t_5279c082 recovered the crashed control-plane fix t_2e9370aa
because the native completion path itself was the defect under repair.

VERIFICATION_MATRIX
- store: /home/frank/.hermes/agent-hooks/goal-judge-provider-error-handler.py
- liveness: python3 /home/frank/.hermes/agent-hooks/goal-judge-provider-error-handler.py
- deliver target: deterministic distiller/fixture metadata for goal-judge quarantine lane
- named consumer: jarvis-os-pm / os-reviewer deterministic test evidence
- satisfied verification: py_compile + selftest runner output + review gate
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoalJudgeProviderErrorLane:
    """Closed operator-lane for provider-error completion traps."""

    lane_marker: str = "GOAL_JUDGE_PROVIDER_ERROR_QUARANTINE"
    evidence_override_marker: str = "GOAL_JUDGE_VERIFIED_REVIEW_OVERRIDE"
    approved_verdict: str = "APPROVED"
    required_evidence_terms: tuple[str, ...] = (
        "DIAGNOSTIC_VERDICT",
        "TASK_EVIDENCE",
        "task-evidence",
    )
    excluded_from_direct_auto_complete: tuple[str, ...] = (
        "GeminiAPIError",
        "NotFoundError",
    )

    def quarantine_marker(self, task_id: str, reason: str) -> dict[str, Any]:
        """Return a deterministic machine-readable quarantine marker payload."""
        return {
            "decision": "block",
            "lane": self.lane_marker,
            "task_id": task_id,
            "reason": reason,
            "required_path": "operator or reviewer evidence-based override through docs/governance",
            "preserve_state": True,
            "distiller_entry": True,
        }

    def permitted_override_evidence_packet(self, task_id: str) -> dict[str, Any]:
        """Return the evidence-packet shape required for a verified-review override."""
        return {
            "task_id": task_id,
            "allow_marker": self.evidence_override_marker,
            "required_components": [
                "explicit REVIEW_VERDICT=APPROVED",
                "reviewed task evidence path in body/comments/metadata",
                "terminal review state unblocked by provider error only",
            ],
            "safety_boundary": "operator/docs/governance approval remains required for broad lane changes",
        }

    def fixture_metadata(self) -> dict[str, Any]:
        """Structured fixture metadata for distiller/test consumption."""
        return {
            "goal_judge_provider_error_lane": self.quarantine_marker(
                "t_fixture_provider_trap",
                "External goal-judge provider error during completion; evidence/review state preserved",
            ),
            "verified_review_override_evidence_packet": self.permitted_override_evidence_packet("t_fixture_provider_trap"),
            "allowed_without_verify_pass": False,
            "gate_path": "completion-hook fail-closed quarantine -> operator/reviewer override",
        }


def main() -> int:
    lane = GoalJudgeProviderErrorLane()
    print(json.dumps(lane.fixture_metadata(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
