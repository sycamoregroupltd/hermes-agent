#!/usr/bin/env python3
"""Classify kanban completion payloads for the running-app gate.

Contract output is one of:
- web: true frontend/web/app work; completion must include real VERIFY_PASS evidence.
- readonly_nonapp: explicit non-app/review/report/static artifact work; running-app gate does not apply.
- not_web: no frontend/web/app surface detected.

The ordered CONTRACT_TABLE is intentionally explicit. New allow overrides must be
paired with negative fixtures in gate-kanban-complete.fixtures.json proving that
the same wording still blocks when attached to concrete frontend work.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

PatternList = Sequence[str]


WEB_PATTERNS: PatternList = [
    # UI/app implementation nouns. Deliberately exclude bare "route" and "page";
    # infra/report cards say "route this cron" or "source page content" without
    # being browser/app surfaces.
    r"(^|[^a-z0-9])(marketplace|storefront|frontend|dashboard|render|renders|serve|serving|client|component|middleware|layout|ui)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])(page\.tsx|web page|app page|route page|page component)([^a-z0-9]|$)",
    r"apps/web",
    r"(^|[^a-z0-9])(trpc|next\.js|nextjs|react|vite)([^a-z0-9]|$)",
    # Route only counts when it is clearly an app/web/API route surface, not a
    # generic verb like "route an enabled cron".
    r"(^|[^a-z0-9])((app|web|api|frontend) route|route handler|running route|route page|route component)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])app([^a-z0-9].{0,80})route([^a-z0-9]|$)",
    r"(^|[^a-z0-9])route([^a-z0-9].{0,80})app([^a-z0-9]|$)",
]

READONLY_PATTERNS: PatternList = [
    r"read-only",
    r"select-only",
    r"log-only",
    r"report-only",
    r"no-agent/report-only",
    r"http/select",
    r"data-health",
    r"delta baseline",
    r"durable .*baseline",
    r"evidence card",
    r"monitor(ing)?/script\+cron",
    r"script\+cron",
    r"cron-store",
    r"observability",
    r"no runtime (service )?operation",
    r"no .*write-path",
    r"no .*order-path write",
    r"no process kills/restarts",
]

COMPLETION_HOOK_CLASSIFIER_PATTERNS: PatternList = [
    r"completion[- ]hook classifier",
    r"ordered completion[- ]hook contract table",
    r"completion[- ]hook contract table",
]

# Explicit non-application intents. These may neutralize broad web words only
# when APP_IMPL_PATTERNS do not identify concrete app implementation work.
NONAPP_OVERRIDE_PATTERNS: PatternList = [
    r"kanban[_-]?complete.*false[ -]?positive",
    r"completion[- ]hook mismatch",
    *COMPLETION_HOOK_CLASSIFIER_PATTERNS,
    r"completion[- ]gate bug",
    r"pm acceptance.*non-app.*completion[- ]gate",
    r"non-app.*completion[- ]gate.*app-verification is not applicable",
    r"completion[- ]gate classification mismatch",
    r"completion gate.*false[ -]?positive",
    r"verify_pass false[ -]?positive",
    r"fix non-app completion gate",
    r"repair .*verify_pass false[ -]?positive",
    r"infra cron/report-only",
    # Documentation/index/report artifacts can mention Mission Control,
    # north-star boards, or dashboard/runbook context without changing a web route.
    r"mission control.*jarvis bible.*index",
    r"jarvis bible.*runbook.*index",
    r"internal .*runbook index",
    r"docs/index",
    r"markdown (index|pointer)",
    r"knowledge vault",
    r"cron portability",
    # CLI/script or Python/shell words are not enough by themselves; true web
    # cards often mention helper scripts. Exempt only with non-web/non-UI or
    # gate/preflight-repair context.
    r"\bnon[- ]web\b.*\b(cli|script|python|shell|cli/script|python/shell)\b",
    r"\b(cli|script|python|shell|cli/script|python/shell)\b.*\bnon[- ]web\b",
    r"\bnon[- ]ui\b.*\b(cli|script|python|shell|cli/script|python/shell)\b",
    r"\b(cli|script|python|shell|cli/script|python/shell)\b.*\bnon[- ]ui\b",
    r"\b(cli/script|python/shell)\b.*\b(preflight|completion[- ]gate|kanban[_-]?complete|false[ -]?positive|classification)\b",
    r"\b(preflight|completion[- ]gate|kanban[_-]?complete|false[ -]?positive|classification)\b.*\b(cli/script|python/shell)\b",
    r"worker[-_ ]visibility preflight",
    r"profile catalog",
    r"skill bundle hygiene",
    r"mcp/tool inventory",
    # PM/review-lane routing cards inspect and route another task handoff. They
    # can quote product surfaces but do not modify or serve the app.
    r"\bpm routing\b",
    r"\breview[- ]lane (task|routing|work)\b",
    r"\breview[- ]required handoff\b",
    r"\bguardian review child\b",
    r"\bdependency[- ]stalled guardian\b",
    r"\bunlinked? (the )?dependency\b",
    r"\bverified .*\brunning on guardian\b",
    r"\bno (app|product|frontend|web) code changes\b",
    r"\bno app/product code changes\b",
    # Package/type/schema review cards can mention apps/api tsc, tRPC routers,
    # or downstream web/API surfaces as evidence from typecheck output.
    r"\breview\b[^\n]{0,160}\bpackages/types\b",
    r"\bpackages/types\b[^\n]{0,200}\b(review|typescript|typecheck|tsc|zod|schema|gate|policy)\b",
    # Repo hygiene / checkout reconciliation can quote app paths from git status.
    r"\brepo[- ]hygiene\b",
    r"\bcheckout[- ]reconciliation\b",
    r"\breconcile dirty .*checkout\b",
    r"\bdirty (canonical )?checkout\b",
    r"\.git/info/exclude",
    r"\breversible stash\b",
    r"\bstash preservation\b",
    r"\bgit status\b[^\n]{0,120}\bclean\b",
    r"\bobsidian (note|markdown|log)\b",
    r"\bno (app|runtime|route|page|component|middleware|api|trpc|auth|tenant|layout|frontend)\b[^\n]{0,160}\b(touched|changed|modified|surface|surfaces)\b",
    # Static/Obsidian QA packets can mention screenshots/render output as audit
    # evidence; they are artifact packets when paired with explicit static scope.
    r"\b(static|authored[- ]only|static/authored[- ]only|static[- ]only)\b[^\n]{0,160}\b(visual/content qa packet|qa packet|visual packet|evidence packet)\b",
    r"\b(visual/content qa packet|qa packet|visual packet|evidence packet)\b[^\n]{0,160}\b(static|authored[- ]only|static/authored[- ]only|static[- ]only|obsidian)\b",
    r"\bobsidian/static qa packet\b",
    r"\bfinal lightweight visual/content qa packet\b",
    r"\bforbidden[- ]capability confirmations\b",
]

FLEET_SLO_NONAPP_PATTERNS: PatternList = [
    r"fleet slo report",
    r"slo report script",
    r"slo report.*artifact",
]

FLEET_SLO_WEB_PATTERNS: PatternList = [
    r"apps/web",
    r"(^|[^a-z0-9])(frontend|react|next\.js|nextjs|vite|component|client|ui)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])(page\.tsx|web page|app page|route page|page component)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])((app|web|api|frontend) route|route handler|running route|route component)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])(build|implement|ship|add|create)([^.\n]{0,80})dashboard([^a-z0-9]|$)",
    r"(^|[^a-z0-9])dashboard([^.\n]{0,80})(web|page|component|frontend|react|route|ui|app)([^a-z0-9]|$)",
]

APP_IMPL_PATTERNS: PatternList = [
    # Bare apps/web is not concrete implementation here: repo hygiene cards quote
    # dirty git-status paths. WEB_PATTERNS still sees the path; this stricter list
    # only decides whether an allow override may neutralize broad web signals.
    r"(^|[^a-z0-9])(build|implement|ship|add|create|modify|fix|update)([^.\n]{0,120})(frontend|web|app|dashboard|route|component|page|middleware|layout)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])(frontend|web|app|dashboard|route|component|page|middleware|layout)([^a-z0-9][^.\n]{0,120})(build|implement|ship|add|create|modify|fix|update)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])(route component|page component|app page|web page|page\.tsx|middleware|layout)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])(react|next\.js|nextjs|vite)([^.\n]{0,120})(page|route|component|ui|frontend|app)([^a-z0-9]|$)",
]

EXPLICIT_NO_APP_CHANGE_PATTERNS: PatternList = [
    r"\bno (app|product|frontend|web) code changes\b",
    r"\bno app/product code changes\b",
    r"\bdo not (modify|touch|change) (frontend|web|app)\b",
    r"\bno (frontend|web|app)[^\n.]{0,80}(route|page|component|middleware|layout|ui)[^\n.]{0,80}(touched|changed|modified)\b",
]

NEGATED_APP_IMPL_PATTERNS: PatternList = [
    r"\bdo not (modify|touch|change) (frontend|web|app)\b",
    r"\bno (frontend|web|app)[^\n.]{0,80}(route|page|component|middleware|layout|ui)[^\n.]{0,80}(touched|changed|modified)\b",
]


def _any(patterns: PatternList, text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _split_hook_text(raw: str) -> tuple[str, str]:
    """Return (task_part, all_text).

    gate-kanban-complete.sh passes title/body, comments, and completion input with
    sentinels. Only title/body classify the task surface; comments/input are
    allowed to contribute to explicit read-only evidence but not to broad web
    detection, because they often quote "frontend/web false positive".
    """
    lowered = raw.lower()
    task_part = lowered.split("\n---comments---\n", 1)[0]
    return task_part, lowered


def _has_app_impl(task_part: str) -> bool:
    # Concrete implementation signals must win over contradictory "no app" or
    # "no product" disclaimers. Review proved that an app task can include a
    # stale/noisy disclaimer (for example "Implement apps/web ... No app/product
    # code changes are requested"); allowing that disclaimer to neutralize the
    # implementation signal bypasses the running-app VERIFY_PASS gate.
    # Keep explicit "do not modify/touch/change frontend" review-routing wording
    # as a negation so PM/reviewer handoff cards are not misread as app work.
    if _any(COMPLETION_HOOK_CLASSIFIER_PATTERNS, task_part) and not _any(FLEET_SLO_WEB_PATTERNS, task_part):
        return False
    return _any(APP_IMPL_PATTERNS, task_part) and not _any(NEGATED_APP_IMPL_PATTERNS, task_part)


@dataclass(frozen=True)
class ContractRule:
    name: str
    decision: str
    predicate: Callable[[str, str], bool]
    rationale: str


def _nonapp_override_without_app_impl(task_part: str, raw: str) -> bool:
    return _any(NONAPP_OVERRIDE_PATTERNS, task_part) and not _has_app_impl(task_part)


def _fleet_slo_report_without_web(task_part: str, raw: str) -> bool:
    return _any(FLEET_SLO_NONAPP_PATTERNS, task_part) and not _any(FLEET_SLO_WEB_PATTERNS, task_part)


def _readonly_without_web(task_part: str, raw: str) -> bool:
    return _any(READONLY_PATTERNS, raw) and not _any(WEB_PATTERNS, task_part)


def _web_surface(task_part: str, raw: str) -> bool:
    return _any(WEB_PATTERNS, task_part)


CONTRACT_TABLE: Sequence[ContractRule] = [
    ContractRule(
        name="ALLOW_NONAPP_OVERRIDE_ONLY_WITHOUT_APP_IMPL",
        decision="readonly_nonapp",
        predicate=_nonapp_override_without_app_impl,
        rationale="explicit non-app/review/repo/static packet intent, with no concrete app implementation signal",
    ),
    ContractRule(
        name="ALLOW_FLEET_SLO_REPORT_ONLY_WITHOUT_WEB_UI",
        decision="readonly_nonapp",
        predicate=_fleet_slo_report_without_web,
        rationale="report/script SLO artifact, not a browser dashboard implementation",
    ),
    ContractRule(
        name="ALLOW_READONLY_EVIDENCE_ONLY_WITHOUT_WEB_SURFACE",
        decision="readonly_nonapp",
        predicate=_readonly_without_web,
        rationale="read-only/report/observability evidence without frontend/web task surface",
    ),
    ContractRule(
        name="BLOCK_WEB_SURFACE_NEEDS_VERIFY_PASS",
        decision="web",
        predicate=_web_surface,
        rationale="frontend/web/app surface detected; running-app VERIFY_PASS required by shell hook",
    ),
    ContractRule(
        name="ALLOW_DEFAULT_NOT_WEB",
        decision="not_web",
        predicate=lambda task_part, raw: True,
        rationale="no frontend/web/app surface detected",
    ),
]


def classify(raw: str) -> str:
    task_part, all_text = _split_hook_text(raw)
    for rule in CONTRACT_TABLE:
        if rule.predicate(task_part, all_text):
            return rule.decision
    # Unreachable because the default rule always matches, but keep fail-open.
    return "not_web"


def main() -> int:
    print(classify(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
