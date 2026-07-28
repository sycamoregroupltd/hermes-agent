#!/usr/bin/env python3
"""Classify kanban completion payloads for the running-app gate.

Contract output is one of:
- web: true frontend/web/app work; completion must include real VERIFY_PASS evidence.
- readonly_nonapp: explicit non-app/review/report/static artifact work; running-app gate does not apply.
- not_web: no frontend/web/app surface detected.

The ordered CONTRACT_TABLE is intentionally explicit. Maintenance rule: every
future non-app allow override must ship with paired frontend/app negative
fixtures and changed-files-aware tests in gate-kanban-complete.fixtures.json,
proving the same allow wording still blocks when attached to concrete frontend
work or app-surface changed_files metadata.
"""
from __future__ import annotations

import re
import sys
import json
from dataclasses import dataclass
from typing import Callable, Sequence

PatternList = Sequence[str]


@dataclass(frozen=True)
class TaskTypeCategory:
    """Auditable task category mapped to the gate that should verify it."""

    name: str
    decision: str
    verification_gate: str
    patterns: PatternList
    rationale: str


WEB_PATTERNS: PatternList = [
    # UI/app implementation nouns. Deliberately exclude bare "route" and "page";
    # infra/report cards say "route this cron" or "source page content" without
    # being browser/app surfaces.
    r"(^|[^a-z0-9])(marketplace|storefront|frontend|dashboard|render|renders|client|component|middleware|layout|ui)([^a-z0-9]|$)",
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
    r"completion[- ]gate classifier",
    r"completion[- ]gate repair",
    r"completion[- ]gate task[- ]type classification",
    r"completion[- ]gate task[- ]type classifier fix",
    r"static/obsidian qa completion[- ]gate repair",
    r"completion[- ]gate\s*/?\s*review[-/]router",
    r"kanban worker exit[- ]code hardening",
    r"clean[- ]exit without a kanban signal",
]

CONCRETE_WEB_IMPL_PATTERNS: PatternList = [
    r"apps/web",
    r"(^|[^a-z0-9])(react|next\.js|nextjs|vite)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])dashboard([^.\n]{0,80})(route|page|component|frontend|react|ui|app)([^a-z0-9]|$)",
]

APP_CHANGED_FILE_PATTERNS: PatternList = [
    # Completion metadata changed_files entries are authoritative evidence of
    # actual impact. Match each parsed path separately so explanatory metadata
    # prose after the changed_files array cannot masquerade as an edited app file.
    r"(^|/)apps/web/",
    r"(^|/)(src/)?app/[^\n\]]{0,240}(page|layout)\.(tsx|jsx|ts|js)$",
    r"(^|/)(src/)?pages/[^\n\]]{0,240}\.(tsx|jsx|ts|js)$",
    r"(^|/)(src/)?(middleware|layout)\.(tsx|jsx|ts|js)$",
]

# Explicit non-application intents. These may neutralize broad web words only
# when APP_IMPL_PATTERNS do not identify concrete app implementation work.
NONAPP_OVERRIDE_PATTERNS: PatternList = [
    r"kanban[_-]?complete.*false[ -]?positive",
    r"completion[- ]hook mismatch",
    *COMPLETION_HOOK_CLASSIFIER_PATTERNS,
    r"completion[- ]gate bug",
    r"completion[- ]gate misclassification",
    r"non[- ]frontend tasks rejected[^\n]{0,160}verify_pass",
    r"pm acceptance.*non-app.*completion[- ]gate",
    r"non-app.*completion[- ]gate.*app-verification is not applicable",
    r"completion[- ]gate classification mismatch",
    r"completion[- ]gate.*false[ -]?positive",        # also match hyphenated completion-gate (was only space)
    r"\breview\b[^\n]{0,160}\bcompletion[- ]gate repair\b",
    r"\bcompletion[- ]gate repair\b[^\n]{0,160}\breview\b",
    r"\breview\b[^\n]{0,160}\bcompletion[- ]gate\b[^\n]{0,80}\brepair\b",  # non-adjacent combo
    r"\bcompletion[- ]gate\b[^\n]{0,80}\brepair\b[^\n]{0,160}\breview\b",
    r"\bcompletion[- ]gate\b[^\n]{0,80}\breview.*card\b[^\n]{0,80}\b(repair|fixture)\b",  # review-card about gate repair
    r"\breview.*card\b[^\n]{0,80}\b(repair|paired negative|fixture)\b",  # review-card repair task
    r"\bpaired negative repair\b",                    # repair of paired negative fixtures
    r"\bstatic/obsidian qa completion[- ]gate repair\b",
    r"\ba3 proposal\b",                               # A3 proposal cards are not app implementation
    r"\bnonapp false positive\b",                     # nonapp false positive classification cards
    r"\bpackage.lock\b[^\n]{0,120}\bnonapp\b",        # package-lock nonapp classification
    r"\bwhatsapp bridge\b[^\n]{0,120}\b(completion|gate|classifier)\b",  # infra/remediation, not web
    # Review cards about service-gate/Phase 2F fixtures quote code paths
    # from git status/diff without being app implementation.
    r"\breview\b[^\n]{0,200}\b(phase 2[fF]|service[- ]gate|de.?dup|fixtures)\b",
    r"\b(phase 2[fF]|service[- ]gate|de.?dup|fixtures)\b[^\n]{0,200}\breview\b",
    # git diff --check output quoting file paths like plugins/kanban/dashboard/plugin_api.py
    # are evidence, not app surface.
    r"\bgit (diff|status|check)\b[^\n]{0,200}\bdashboard\b",
    r"verify_pass false[ -]?positive",
    r"fix non-app completion gate",
    r"repair .*verify_pass false[ -]?positive",
    r"infra cron/report-only",
    # Cron/config/provider bookkeeping cards often mention that a model/provider
    # is "serving" or that a cron job was "repointed"; those are operational
    # verification terms, not evidence of a browser/app surface.
    r"\bcron/config task\b",
    r"\bcron (job|jobs?)\b[^\n]{0,180}\b(provider|model|pin|repoint|jobs\.json|backup)\b",
    r"\b(provider[- ]state|provider) bookkeeping\b",
    r"\bbookkeeping only\b",
    r"\bno provider routing/fallback changes\b",
    r"\bjobs\.json backup\b",
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
    r"\breview[- ]lane .*terminal[- ]capable evidence contract\b",
    r"\bplatform[- ]reviewer .*terminal evidence (path|contract)\b",
    r"\bcompletion[- ]gate false allow\b",
    r"\bnegated running_app_verification (comment|packet)\b",
    r"\bnegated RUNNING_APP_VERIFICATION (comment|packet)\b",
    r"\brepair (the )?hook\b[^\n]{0,160}\bRUNNING_APP_VERIFICATION\b",
    r"\bminimal reviewer evidence contract fix\b",
    r"\breviewer evidence contract fix\b",
    r"\bprofile-local config/checklist/tool-path repair\b[^\n]{0,180}\bfrontend/web reviewers can run required gates\b",
    r"\bterminal evidence[- ]path fix\b",
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
    # PM scope-validation/typecheck-scope cards can mention page/router/tRPC
    # symbols as inspected compile targets. They are report-only/research cards
    # unless paired with concrete frontend/app implementation language.
    r"\b(scope[- ]validation|typecheck[- ]scope|compile[- ]only review[- ]lane)\b",
    r"\bvalidate [^\n]{0,120}typecheck scope\b",
    r"\bpre[- ]change typecheck context\b",
    r"\btypecheck baseline\b[^\n]{0,160}\b(scope note|scope validation|approval to proceed|block with evidence)\b",
    r"\b(inspected files|observed hotspots/error classes|baseline commands/output counts)\b",
    r"\bno (app|runtime|route|page|component|middleware|api|trpc|auth|tenant|layout|frontend)\b[^\n]{0,160}\b(touched|changed|modified|surface|surfaces)\b",
    # Static/Obsidian QA packets can mention screenshots/render output as audit
    # evidence; they are artifact packets when paired with explicit static scope.
    r"\b(static|authored[- ]only|static/authored[- ]only|static[- ]only)\b[^\n]{0,160}\b(visual/content qa packet|qa packet|visual packet|evidence packet)\b",
    r"\b(visual/content qa packet|qa packet|visual packet|evidence packet)\b[^\n]{0,160}\b(static|authored[- ]only|static/authored[- ]only|static[- ]only|obsidian)\b",
    r"\bobsidian/static qa packet\b",
    r"\bfinal lightweight visual/content qa packet\b",
    r"\bforbidden[- ]capability confirmations\b",
    # SUPERSEDED-banner detector/triage tasks are paper-only Obsidian hygiene.
    # Their candidate note paths may include words such as "dashboard" from
    # stale artifact filenames, but no browser/app surface is changed.
    r"\bsuperseded[- ]banner\b",
    r"\bpaper[- ]only knowledge hygiene\b",
    r"\bpaper[- ]only obsidian markdown hygiene\b",
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
    r"\bno (frontend|web|app)[^\n.]{0,120}(route|page|component|middleware|layout|ui)[^\n.]{0,120}(touched|changed|modified|code edits?|edits?)\b",
    r"\bno [^\n.]{0,80}(frontend/web/app|frontend|web|app)[^\n.]{0,200}(route|page|component|middleware|layout|ui|trpc|browser)[^\n.]{0,200}(is changed|changed|touched|modified|code edits?|edits?)\b",
    # Review/repair cards quote the desired guardrail behavior for real app
    # work. That expectation text is not itself an instruction to implement UI.
    r"\bactual [^\n.]{0,160}(frontend|web|app|route|page|component|middleware|layout)[^\n.]{0,120}work remains blocked unless\b",
    r"\b(concrete|real|actual) [^\n.]{0,160}(frontend|web|app|route|page|component|middleware|layout)[^\n.]{0,120}(blocks?|blocked|remains blocked) (without|unless)\b",
    r"\b(concrete|real|actual) [^\n.]{0,160}(frontend|web|app|route|page|component|middleware|layout)[^\n.]{0,120}work that lacks\b",
    # False-positive classifier repair cards can begin with "FIX:" and quote the
    # frontend/web gate they are repairing. That wording is not an instruction to
    # implement the app surface; concrete apps/web/route implementation still
    # matches APP_IMPL_PATTERNS and is covered by paired negatives.
    r"\bfalsely requires [^\n.]{0,80}(frontend|web|app)[^\n.]{0,120}verify_pass\b",
    r"\bclassified as (a )?(frontend|web|app)[^\n.]{0,120}\b(add|adjust|repair)\b[^\n.]{0,120}\b(classifier|contract|hook|gate)\b",
    r"\bprofile-local config/checklist/tool-path repair\b[^\n.]{0,180}\bfrontend/web reviewers can run required gates\b",
    r"\bpaired frontend negative\b[^\n.]{0,160}\b(still )?blocks?\b[^\n.]{0,160}\bapps/web\b",
    r"\bapps/web\b[^\n.]{0,160}\b(still )?blocks?\b[^\n.]{0,160}\bwithout verify_pass\b",
]

NEGATED_CONCRETE_WEB_REFERENCE_PATTERNS: PatternList = [
    # Review-only classifier/hook cards can quote paired negative cases,
    # including literal apps/web paths, to prove frontend work still blocks.
    # Those acceptance quotes are not concrete implementation signals.
    r"\bpaired frontend negative\b[^\n.]{0,200}\bblocks?\b[^\n.]{0,120}\bapps/web\b",
    r"\bpaired frontend negative\b[^\n.]{0,200}\bapps/web\b[^\n.]{0,120}\bblocks?\b",
    r"\bpaired [^\n.]{0,80}negative\b[^\n.]{0,200}\bconcrete apps/web\b[^\n.]{0,120}\bwithout verify_pass\b",
]

SOURCE_REVIEW_PATTERNS: PatternList = [
    r"\breview_verdict\b",
    r"\b(?:source task|source review|review source task)\b",
]


FRONTEND_WEB_TASK_CATEGORY = TaskTypeCategory(
    name="frontend_web_app_surface",
    decision="web",
    verification_gate="running-app VERIFY_PASS",
    patterns=WEB_PATTERNS,
    rationale="frontend/web/app surface detected in task title/body",
)

FRONTEND_WEB_CHANGED_FILES_CATEGORY = TaskTypeCategory(
    name="frontend_web_changed_files",
    decision="web",
    verification_gate="running-app VERIFY_PASS",
    patterns=APP_CHANGED_FILE_PATTERNS,
    rationale="completion metadata changed_files includes frontend/web app surface files",
)

OPERATIONAL_NONCODE_TASK_CATEGORY = TaskTypeCategory(
    name="operational_noncode_bookkeeping",
    decision="readonly_nonapp",
    verification_gate="evidence-based task-specific verification",
    patterns=NONAPP_OVERRIDE_PATTERNS,
    rationale="cron/config/bookkeeping/review/static/repo-hygiene work with no concrete app implementation signal",
)

READONLY_EVIDENCE_TASK_CATEGORY = TaskTypeCategory(
    name="readonly_evidence_only",
    decision="readonly_nonapp",
    verification_gate="evidence-based task-specific verification",
    patterns=READONLY_PATTERNS,
    rationale="read-only/report/observability evidence without frontend/web task surface",
)


def _any(patterns: PatternList, text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _matches_category(category: TaskTypeCategory, text: str) -> bool:
    return _any(category.patterns, text)


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
    #
    # Review cards about completion-gate repair are NOT app implementation.
    # Match both adjacent "completion-gate repair" and non-adjacent patterns
    # where "completion-gate" and "repair" are within 80 chars of each other.
    raw_app_impl = _any(APP_IMPL_PATTERNS, task_part) and not _any(NEGATED_APP_IMPL_PATTERNS, task_part)
    has_concrete_web_impl = _any(CONCRETE_WEB_IMPL_PATTERNS, task_part) and not _any(
        NEGATED_CONCRETE_WEB_REFERENCE_PATTERNS,
        task_part,
    )
    if (
        _any(NONAPP_OVERRIDE_PATTERNS, task_part)
        and re.search(r"\bcompletion[- ]gate false allow\b|\bcompletion[- ]gate misclassification\b|\bnon[- ]frontend tasks rejected[^\n]{0,160}verify_pass\b|\bnegated running_app_verification (comment|packet)\b", task_part)
        and not has_concrete_web_impl
    ):
        return False
    if (
        re.search(r"\bcompletion[- ]gate classifier\b", task_part)
        and not has_concrete_web_impl
    ):
        return False
    review_completion_gate_nonapp = bool(
        re.search(r"^\s*review:[^\n]{0,300}", task_part)
        and _any(NONAPP_OVERRIDE_PATTERNS, task_part)
        and not raw_app_impl
        and not has_concrete_web_impl
    )
    has_app_impl = raw_app_impl and not review_completion_gate_nonapp
    if re.search(r"^\s*review:[^\n]{0,300}", task_part):
        # The title starts with "review:". Check if this is about completion-gate
        # repair (not app impl) vs. actual frontend review. Concrete app
        # implementation signals win: a review-prefixed card that itself says to
        # implement/fix apps/web routes/pages/components must still be gated.
        if (
            re.search(r"\bcompletion[- ]gate\b[^\n]{0,80}\brepair\b", task_part)
            and not raw_app_impl
            and not has_concrete_web_impl
        ):
            return False
        if (
            re.search(r"\bcompletion[- ]gate\b[^\n]{0,120}\b(classifier|fixture|false positive|nonapp)\b", task_part)
            and not raw_app_impl
            and not has_concrete_web_impl
        ):
            return False
        # Review cards that also match nonapp override patterns (service-gate,
        # Phase 2F, fixtures, de-dup, etc.) are not app implementation.
        # When NONAPP patterns fire, the review is analysis/audit, not UI work.
        if _any(NONAPP_OVERRIDE_PATTERNS, task_part) and not has_app_impl and not has_concrete_web_impl:
            return False
    if (
        _any(COMPLETION_HOOK_CLASSIFIER_PATTERNS, task_part)
        and not _any(CONCRETE_WEB_IMPL_PATTERNS, task_part)
        and not _any(APP_IMPL_PATTERNS, task_part)
    ):
        return False
    # Broaden the adjacent-only check to handle non-adjacent "completion-gate repair"
    # in review context (e.g. "REVIEW: completion-gate review-card paired negative repair")
    if (
        re.search(r"^\s*review:[^\n]{0,160}\bcompletion[- ]gate repair\b", task_part)
        and not raw_app_impl
        and not has_app_impl
        and not has_concrete_web_impl
    ):
        return False
    # Also handle cards starting with "REPAIR:" that mention the completion gate
    # (e.g. "REPAIR: completion-gate classifier review-card false positive fixture")
    if (
        re.search(r"^\s*repair:[^\n]{0,300}\bcompletion[- ]gate\b", task_part)
        and not raw_app_impl
        and not has_app_impl
        and not has_concrete_web_impl
    ):
        return False
    # Also handle A3 proposals about completion-gate classification — these
    # may contain "fix frontend" as a description of the false positive, not
    # as an instruction to implement frontend work.
    if (
        re.search(r"^\s*a3 proposal\b[^\n]{0,300}\bcompletion[- ]gate\b", task_part)
        and not raw_app_impl
        and not has_app_impl
        and not has_concrete_web_impl
    ):
        return False
    return has_app_impl


@dataclass(frozen=True)
class ContractRule:
    name: str
    decision: str
    predicate: Callable[[str, str], bool]
    rationale: str


def _nonapp_override_without_app_impl(task_part: str, raw: str) -> bool:
    return _matches_category(OPERATIONAL_NONCODE_TASK_CATEGORY, task_part) and not _has_app_impl(task_part)


def _fleet_slo_report_without_web(task_part: str, raw: str) -> bool:
    return _any(FLEET_SLO_NONAPP_PATTERNS, task_part) and not _any(FLEET_SLO_WEB_PATTERNS, task_part)


def _changed_files_from_completion_input(raw: str) -> list[str]:
    """Best-effort extraction of JSON metadata.changed_files arrays.

    gate-kanban-complete.sh appends current completion summary/result text and a
    JSON dump of metadata after ---INPUT---. This intentionally parses only the
    changed_files array values, not arbitrary prose later in metadata, because
    reviewer notes may quote apps/web example paths that are not changed files.
    """
    input_text = raw.split("\n---input---\n", 1)[-1]
    decoder = json.JSONDecoder()
    paths: list[str] = []
    for match in re.finditer(r'"changed_files"\s*:', input_text):
        idx = match.end()
        while idx < len(input_text) and input_text[idx].isspace():
            idx += 1
        if idx >= len(input_text) or input_text[idx] != "[":
            continue
        try:
            value, _ = decoder.raw_decode(input_text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            paths.extend(str(item).lower() for item in value if isinstance(item, str))
    return paths


def _completion_metadata_touches_app_surface(task_part: str, raw: str) -> bool:
    # Completion metadata is current evidence about what changed. If changed_files
    # names app/frontend surface files, that concrete impact must win over
    # report-only/typecheck/research wording in the title/body.
    return any(
        _matches_category(FRONTEND_WEB_CHANGED_FILES_CATEGORY, path)
        for path in _changed_files_from_completion_input(raw)
    )


def _readonly_without_web(task_part: str, raw: str) -> bool:
    return _matches_category(READONLY_EVIDENCE_TASK_CATEGORY, raw) and not _matches_category(
        FRONTEND_WEB_TASK_CATEGORY, task_part
    )


def _app_impl_needs_verify_pass(task_part: str, raw: str) -> bool:
    # Catch reduced-token app implementation signals that _has_app_impl detects
    # but WEB_PATTERNS don't cover (e.g. bare "implement route" or "add page"
    # without surface-level qualifiers like frontend/component/layout).
    # This closes the review-prefixed reduced-token bypass class.
    return _has_app_impl(task_part)


def _source_review_without_web(task_part: str, raw: str) -> bool:
    """A source PR review verdict card is explicitly a review of another task.
    It does not implement or change frontend/app surfaces itself.
    Guard: the review verdict + source task reference signals this is a
    source-PR-review verdict card, not an implementation card.
    NOTE: this override fires AFTER changed_files (step 1) and app_impl
    (step 2) checks. Those higher-priority guards already filtered out
    concrete app surface work and implementation-verb cards.
    """
    if not _any(SOURCE_REVIEW_PATTERNS, task_part):
        return False
    # Belt-and-suspenders: no concrete web impl should have snuck through
    if _any(CONCRETE_WEB_IMPL_PATTERNS, task_part):
        return False
    if _has_app_impl(task_part):
        return False
    return True


def _web_surface(task_part: str, raw: str) -> bool:
    return _matches_category(FRONTEND_WEB_TASK_CATEGORY, task_part)


CONTRACT_TABLE: Sequence[ContractRule] = [
    ContractRule(
        name="BLOCK_APP_CHANGED_FILES_NEED_VERIFY_PASS",
        decision=FRONTEND_WEB_CHANGED_FILES_CATEGORY.decision,
        predicate=_completion_metadata_touches_app_surface,
        rationale=FRONTEND_WEB_CHANGED_FILES_CATEGORY.rationale,
    ),
    ContractRule(
        name="BLOCK_APP_IMPL_NEEDS_VERIFY_PASS",
        decision=FRONTEND_WEB_CHANGED_FILES_CATEGORY.decision,
        predicate=_app_impl_needs_verify_pass,
        rationale="concrete app implementation signal detected in task title/body; running-app VERIFY_PASS required by shell hook even without broad web surface keywords",
    ),
    ContractRule(
        name="ALLOW_NONAPP_OVERRIDE_ONLY_WITHOUT_APP_IMPL",
        decision=OPERATIONAL_NONCODE_TASK_CATEGORY.decision,
        predicate=_nonapp_override_without_app_impl,
        rationale=OPERATIONAL_NONCODE_TASK_CATEGORY.rationale,
    ),
    ContractRule(
        name="ALLOW_FLEET_SLO_REPORT_ONLY_WITHOUT_WEB_UI",
        decision="readonly_nonapp",
        predicate=_fleet_slo_report_without_web,
        rationale="report/script SLO artifact, not a browser dashboard implementation",
    ),
    ContractRule(
        name="ALLOW_READONLY_EVIDENCE_ONLY_WITHOUT_WEB_SURFACE",
        decision=READONLY_EVIDENCE_TASK_CATEGORY.decision,
        predicate=_readonly_without_web,
        rationale=READONLY_EVIDENCE_TASK_CATEGORY.rationale,
    ),
    ContractRule(
        name="ALLOW_SOURCE_PR_REVIEW_WITHOUT_WEB_SURFACE",
        decision=OPERATIONAL_NONCODE_TASK_CATEGORY.decision,
        predicate=_source_review_without_web,
        rationale="source PR review verdict card with REVIEW_VERDICT and source task reference; no app/impl/web surface touched by this review card",
    ),
    ContractRule(
        name="BLOCK_WEB_SURFACE_NEEDS_VERIFY_PASS",
        decision=FRONTEND_WEB_TASK_CATEGORY.decision,
        predicate=_web_surface,
        rationale=FRONTEND_WEB_TASK_CATEGORY.rationale,
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
