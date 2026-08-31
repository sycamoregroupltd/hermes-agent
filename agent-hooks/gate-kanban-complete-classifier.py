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

VERIFICATION_MATRIX
- store: /home/frank/.hermes/agent-hooks/gate-kanban-complete-classifier.py
- liveness: python3 /home/frank/.hermes/agent-hooks/gate-kanban-complete-classifier.py < /dev/null
- deliver target: task-type classification for gate-kanban-complete.sh pre_tool_call hook
- named consumer: jarvis-os-pm / os-reviewer deterministic completion-gate evidence
- satisfied verification: gate-kanban-complete selftest output + py_compile + this task's review
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
    r"(^|[^a-z0-9])(trpc)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])(react|next\.js|nextjs|vite)([^.\n]{0,120})(page|route|component|ui|frontend|app)([^a-z0-9]|$)",
    # Route only counts when it is clearly an app/web/API route surface, not a
    # generic verb like "route an enabled cron".
    r"(^|[^a-z0-9])((app|web|api|frontend) route|route handler|running route|route page|route component)([^a-z0-9]|$)",
    # 2026-08-31: the two proximity rules below span up to 80 chars, so an
    # unrelated "app" and "route" in the SAME SENTENCE collide. Real case:
    # ai-restaurant/t_bde415c4 (a canary/portability TEST card) was classified
    # `web` off "...the documented future no-install DMG route reference, and
    # confirmation that the app was not mutated..." — a distribution route and
    # a negated app mention, 0% web content. It then failed the running-app
    # gate it should never have been subject to. Exclude the non-web senses of
    # "route" (distribution/delivery/escalation/network) from the proximity
    # rules; the explicit surface forms above still match real app routes.
    r"(^|[^a-z0-9])app([^a-z0-9](?:(?!\b(?:dmg|installer|install|no-install|download|delivery|distribution|escalat|network|traffic|migration|shipping)\b).){0,80})route([^a-z0-9]|$)",
    r"(^|[^a-z0-9])route((?:(?!\b(?:dmg|installer|install|no-install|download|delivery|distribution|escalat|network|traffic|migration|shipping)\b)[^a-z0-9].){0,80})app([^a-z0-9]|$)",
    # 2026-09-01: same proximity-rule class as the 2026-08-31 fix, new instance.
    # Real case: ai-restaurant/t_6769a438 (SOUS Blender/glTF canary/portability
    # TEST card) says "...do not mutate the app. Document a preferred future
    # no-install route..." — a negated app-mutation clause and a DMG
    # distribution-route noun phrase, 0% web content. "no-install" was not
    # excluded because the prior exclusion list only matched the word
    # "installer", not "install"/"no-install". Added both forms above.
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
    r"(^|[^a-z0-9])(react|next\.js|nextjs|vite)([^.\n]{0,120})(page|route|component|ui|frontend|app)([^a-z0-9]|$)",
    r"(^|[^a-z0-9])dashboard([^.\n]{0,80})(route|page|component|frontend|react|ui|app)([^a-z0-9]|$)",
]

# Profile names are often hyphen/underscore compounds (for example
# ``upero-ui-builder`` and ``frontend-builder``).  WEB_PATTERNS intentionally
# recognizes standalone UI vocabulary, so its boundary sees the embedded
# ``ui``/``frontend`` token in these names.  The guard below only scrubs a
# web-token compound when the task also has profile/roster administration
# context; ordinary prose such as ``build a frontend page`` is untouched.
PROFILE_IDENTIFIER_WEB_TOKEN_RE = re.compile(
    r"(?<![a-z0-9])(?:[a-z0-9]+[-_])*"
    r"(?:marketplace|storefront|frontend|dashboard|renders?|client|component|middleware|layout|ui)"
    r"(?:[-_][a-z0-9]+)+(?![a-z0-9])"
)
PROFILE_ADMIN_CONTEXT_PATTERNS: PatternList = [
    r"\bprofiles?\b",
    r"\broster\b",
    r"\bretir(?:e|ed|ement|ing)\b",
    r"\barchiv(?:e|ed|ing)\b",
    r"\bclone(?:d|s)?\b",
    r"\bdirector(?:y|ies)\b",
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
    r"\b(?:retir(?:e|ed|ement|ing)|archiv(?:e|ed|ing)|clone(?:d|s)?)\b[^.\n]{0,180}\b(?:profiles?|roster|director(?:y|ies))\b",
    r"\b(?:profiles?|roster|director(?:y|ies))\b[^.\n]{0,180}\b(?:retir(?:e|ed|ement|ing)|archiv(?:e|ed|ing)|clone(?:d|s)?)\b",
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
    # Monitor/report/script cards that describe a metric "renders" as 0pp,
    # MEASUREMENT_UNAVAILABLE, or a missing section are describing report
    # output, not a browser/app render surface. Bare "render|renders" in
    # WEB_PATTERNS must not wedge read-only monitor/report completion. Paired
    # negative fixture proves the same wording attached to concrete apps/web
    # work still blocks.
    r"\b(monitor|report|script)\b[^\n]{0,120}\brenders?\b[^\n]{0,160}\b(0pp|metric|number|value|output|section|marker|unmeasured|unavailable)\b",
    # Cron/report/CLI task cards often describe command/list output using
    # "renders" language; that is report/output phrasing, not browser rendering.
    r"\b(cli|python|shell|command|script|cron|report)\b[^\n]{0,160}\brenders?\b[^\n]{0,120}\b(output|list|line|item|items|rows?|values?|count|marker|result|results|command)\b",
    # PM planning cards issue concrete-looking PM verbs (route/create/expand)
    # alongside an explicit "create one implementation child" instruction,
    # which the broad APP_IMPL verb+route regex latches onto as app work (false
    # positive). When the body pairs that with a paper-only safety clause, it is
    # a PM routing/delegation instruction, not frontend implementation. Concrete
    # apps/web work still matches APP_IMPL_PATTERNS/CONCRETE_WEB_IMPL_PATTERNS
    # and is caught by paired negatives; this override only relaxes the false
    # positive on PM planning + paper-only safety wording.
    r"\bcreate one implementation child\b[\s\S]{0,900}\b(paper[- ]?only|no (app|product|frontend|web) code changes|no [\s\S]{0,80}(route|page|component|middleware|layout|ui|trpc|browser)[\s\S]{0,200}(is changed|changed|touched|modified))\b",
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
    # Noun-side leading boundary (t_17b2b7ed): require a real word break before
    # the app noun so "route" cannot match inside compound nouns (router,
    # reroute, pm-reroute, verdict-router, api-router, message_router,
    # trade-route, unblocker-route). Hyphen/underscore count as word chars.
    # Mirror of the t_660a588a verb-boundary fix. Standalone "route"/"routes"
    # still match.
    r"(^|[^a-z0-9])(build|implement|ship|add|create|modify|fix|update)([^.\n]{0,120})(^|[^a-z0-9_-])(frontend|web|app|dashboard|routes?|component|page|middleware|layout)([^a-z0-9]|$)",
    # Verb-last variant: <app noun> ... <verb>. The verb alternation REQUIRES a
    # leading boundary (BOL or non-alnum) so "ship" inside "ownership" or "add"
    # inside "upshot" can never match the verb group. Without the boundary,
    # "route ... strict human ownership" wedged a static SOUS review card as web
    # (verb "ship" matched inside "ownership" within 120 chars of noun "route").
    r"(^|[^a-z0-9])(frontend|web|app|dashboard|route|component|page|middleware|layout)([^a-z0-9][^.\n]{0,120})(^|[^a-z0-9])(build|implement|ship|add|create|modify|fix|update)([^a-z0-9]|$)",
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
    # Qualified SQL GUC/schema identifier like `app.bypass_append_only` is a
    # database namespace reference, not a frontend app surface. A bare dotted
    # app.<snake_ident> reference must not trigger the fix/build ... app impl lane.
    r"\bapp\.[a-z_][a-z0-9_]*\b",
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
    # PM planning cards issue concrete-looking PM verbs (route/create/expand)
    # alongside an explicit "create one implementation child" instruction,
    # which the broad APP_IMPL verb+route regex latches onto as app work. When
    # the body pairs that with a paper-only / no-app safety clause it is a PM
    # routing/delegation instruction, not frontend implementation. Concrete
    # apps/web work still matches APP_IMPL_PATTERNS and is caught by the paired
    # negative fixture; this negation only relaxes the false positive on PM
    # planning cards.
    r"\bcreate one implementation child\b[\s\S]{0,900}\b(paper[- ]?only|no (app|product|frontend|web) code changes)\b",
    # Diagnostic/dispensation idiom: "route the narrow host-runtime fix",
    # "route a separate reviewed implementation card", "route ... disposition",
    # "route ... evidence path", "route a bounded data-quality remediation card".
    # Here "route" is a dispatch verb (direct the fix elsewhere), NOT a
    # "route component" app-surface noun. The broad APP_IMPL_PATTERNS[1]
    # matches "route ... fix" and falsely classifies postmortem/log-forensics
    # DIAGNOSE cards as frontend/web (t_2a606755, t_0430e36c). This only
    # neutralizes the verb+routed-object idiom; concrete
    # "apps/web ... route component/page" still matches APP_IMPL_PATTERNS and
    # CONCRETE_WEB_IMPL_PATTERNS and is caught by paired negative fixtures.
    r"\broute\s+(the|a|this|that|an)\s+[^.\n]{0,80}\b(narrow|host-runtime|separate|reviewed|next|bounded)\b[^.\n]{0,140}\b(fix|implementation card|disposition|evidence path|verdict|implementation)\b",
    # Article-less dispatch idiom (t_c5cfa962; repro t_431e9b88): "route
    # implementation + independent review", "route review", "route the fix",
    # "route disposition", "route a reviewed implementation/card". The
    # article-ful negation above requires an article + adjective qualifier, so
    # this bare form ("fix is needed, route implementation") slipped through:
    # APP_IMPL_PATTERNS[0] matched verb-first "fix ... route" and wedged a
    # backend/infra diagnosis+review card as web. Here "route" is a dispatch
    # verb (direct the fix/review to a builder), NOT an app-route noun. The
    # negation fires ONLY when a dispatch/review object directly follows
    # "route"; a lookahead blocks it when a surface noun follows, so
    # "route implementation page/component" and "apps/web route
    # implementation" are never neutralized. Concrete apps/web route/page work
    # still matches APP_IMPL_PATTERNS/CONCRETE_WEB_IMPL_PATTERNS and is caught
    # by the paired negative fixture and BLOCK_WEB_SURFACE (WEB_PATTERNS
    # "apps/web"/"app route").
    r"\broute\s+(implementation|review|independent\s+review|disposition|the\s+fix|a\s+reviewed\s+(?:implementation|card))\b(?!\s+(?:component|page|layout|middleware|handler|ui|frontend|app|dashboard|route)\b)",
    # Companion: "comment disposition back on <owner>" / "post ... disposition"
    # evidence-back-reference phrasing in DIAGNOSE/forensics cards routes the
    # verdict/disposition rather than implementing a browser route.
    r"\b(comment|post)\s+[^.\n]{0,120}\b(disposition|verdict|evidence path|outcome)\b[^.\n]{0,120}back\b",
    # Backend cron/report/CLI cards can mention rendered list/output text without
    # implying a browser route or page implementation.
    r"\b(cli|python|shell|command|cron|report)\b[^\n]{0,260}\brenders?\b[^\n]{0,120}\b(list|output|rows?|items?|values?|results?|metrics?|markers?|count|section)\b",
    # "No implementation/promotion/paper-sleeve route unless ... paper-only /
    # read-only" safety-clause idiom (t_90c5e1e0; recurrence of the documented
    # t_77316e9c false-positive class). APP_IMPL_PATTERNS[0] has no leading verb
    # boundary, so it matched "implement" inside "implementation" then bare
    # "route" and classed a paper-only edge-discovery/research card as web. Here
    # "route" is a dispatch/safety noun ("no ... route to promotion/paper-sleeve"),
    # NOT a browser/API route surface. The negation fires ONLY when the "no
    # <implementation|promotion|paper-sleeve> ... route(s)" construction is
    # present AND an explicit paper-only/read-only/no-implementation qualifier
    # appears within a bounded window after it, so a positive "implement ... route"
    # instruction is never neutralized. The window is multi-line bounded
    # ([\\s\\S]{0,360}) so the qualifier may sit on a LATER LINE within the same
    # card's safety framing (t_63ac495e: "SAFETY GATES: paper-only" bullet ~270
    # chars after the route clause) while still being capped against crossing
    # unrelated sections of a long card. Concrete apps/web route/page work still
    # matches APP_IMPL_PATTERNS/CONCRETE_WEB_IMPL_PATTERNS and is caught by the
    # paired negative fixture and BLOCK_WEB_SURFACE (WEB_PATTERNS "app route").
    r"\bno\s+(?:implementation|promotion|paper[- ]?sleeve)(?:[/,]\s*(?:implementation|promotion|paper[- ]?sleeve)){0,3}\s*/?\s*\broutes?\b[\s\S]{0,360}\b(?:paper[- ]?only|read[- ]?only|no[ -]implementation)\b",
]

GATE_SCOPE_NONAPP_PATTERNS: PatternList = [
    # Cards that fix/scope the verify-running-app completion gate itself (often
    # "FIX:"-titled, e.g. t_5a334580 "scope verify-running-app gate away from
    # non-UI log-forensics cards"). They quote the gate's surface vocabulary
    # (UI, frontend, route, middleware, layout, trpc, apps/web) to state what
    # the gate SHOULD apply to, and name forensics/non-UI cards as what it
    # should NOT. That describes the gate — it does not build or serve a
    # frontend surface. Concrete web implementation (apps/web, react pages/
    # routes/components) still wins via has_concrete_web_impl in the caller,
    # and app-surface changed_files is still caught by the rule-1
    # BLOCK_APP_CHANGED_FILES backstop. Paired negative fixtures prove a real
    # frontend card that also mentions the gate still blocks.
    r"\b(verify[- ]?running[- ]app|running[- ]app|verify_pass|app[- ]verification)\b[^\n.]{0,120}\b(scope|scoping|gate|classifier|false[ -]?positive|non[- ]?app)\b",
    r"\b(verify[- ]?running[- ]app|running[- ]app|app[- ]verification)\b[^\n.]{0,200}\b(forensic|non[- ]?ui|log[- ]forensic|postmortem|diagnos)\b",
    r"\b(scope|scoping)\b[^\n.]{0,120}\b(verify[- ]?running[- ]app|running[- ]app|app[- ]verification)\b[^\n.]{0,160}\bgate\b",
    r"\bnon[- ]?ui\b[^\n.]{0,120}\b(verify[- ]?running[- ]app|running[- ]app|app[- ]verification)\b",
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
    r"\breview[-_ ]verdict\b",
    r"\b(?:source task|source review|review source task|source[-_ ]?pr[-_ ]?review)\b",
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


def _profile_identifier_signal_text(task_part: str) -> str:
    """Remove only named profile compounds before implementation matching."""
    if not _any(PROFILE_ADMIN_CONTEXT_PATTERNS, task_part):
        return task_part
    return PROFILE_IDENTIFIER_WEB_TOKEN_RE.sub(" ", task_part)


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
    signal_task_part = _profile_identifier_signal_text(task_part)
    raw_app_impl = _any(APP_IMPL_PATTERNS, signal_task_part) and not _any(
        NEGATED_APP_IMPL_PATTERNS, task_part
    )
    has_concrete_web_impl = _any(CONCRETE_WEB_IMPL_PATTERNS, signal_task_part) and not _any(
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
    # verify-running-app gate-scope/classifier-fix cards (often "FIX:"-titled)
    # quote the gate's surface vocabulary to say what the gate applies to, but
    # do not build or serve a frontend surface. They are NOT app implementation.
    # Concrete web implementation (apps/web, react pages/routes) still wins via
    # has_concrete_web_impl, and app-surface changed_files is still caught by
    # the rule-1 BLOCK_APP_CHANGED_FILES backstop. This closes the gap where a
    # gate-fix card (t_5a334580) was itself blocked as `web` by APP_IMPL_PATTERNS
    # matching "implement a narrow classifier fix ... page/route/middleware".
    if _any(GATE_SCOPE_NONAPP_PATTERNS, task_part) and not has_concrete_web_impl:
        return False
    if re.search(r"^\s*review:", task_part):
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
    # Quoted source-PR surface nouns (dashboard bundle/exposure, etc.) must
    # not veto a review-verdict card that has no implementation verb of its
    # own. t_cc5a3939 / t_66c7c9a3: CONCRETE_WEB_IMPL_PATTERNS matching
    # quoted dashboard nouns was forcing web. Keep the _has_app_impl veto
    # so t_patch02-style "applies the reviewed changes" cards stay web.
    if _has_app_impl(task_part):
        return False
    # A goal-judge provider-error quarantine card is NOT a source-review card
    # just because it quotes a CHILD review's REVIEW_VERDICT=APPROVED. Bare
    # REVIEW_VERDICT text must not convert the fail-closed trap into an
    # allow-lane classification; only the strict verified-review override
    # (marker + APPROVED verdict + task-evidence) may do that.
    # REVIEW_VERDICT text alone is not enough for this allow rule, so policy
    # quotes like "do not bypass REVIEW_VERDICT" stay classified here if no
    # explicit source-PR-review context.
    if _goal_judge_provider_error(task_part, raw) and not _verified_review_with_evidence_override(
        task_part, raw
    ):
        return False
    return True


def _source_review_verdict_not_web(task_part: str, raw: str) -> bool:
    """Special-case REVIEW_VERDICT/source-PR-review cards that quote frontend
    surface nouns but do not alter those surfaces.

    These should stay non-web even when earlier NONAPP_OVERRIDE heuristics match
    review language (for example "no frontend/web route..." in review context).
    """
    if not _any(SOURCE_REVIEW_PATTERNS, task_part):
        return False
    if _has_app_impl(task_part):
        return False
    source_pr_review_context = (
        re.search(r"\bsource[-_ ]?pr[-_ ]?review\b", task_part)
        or re.search(r"\breview[-_ ]verdict\s*[:=]", task_part)
        or (
            re.search(r"\breview[-_ ]verdict\b", task_part)
            and re.search(r"\bsource\b[^\n]{0,120}\breview\b", task_part)
        )
    )
    if not source_pr_review_context:
        return False
    if _goal_judge_provider_error(task_part, raw):
        return False
    return True


def _cron_report_cli_render_not_web(task_part: str, raw: str) -> bool:
    """Cron/report/CLI cards describing rendered output/list text are report tasks,
    not frontend implementation tasks.
    """
    if _has_app_impl(task_part):
        return False
    if not re.search(r"\b(cli|python|shell|command|cron|report)\b", task_part):
        return False
    if not (
        re.search(
            r"\b(cli|python|shell|command|cron)\b[^\n]{0,220}\brenders?\b[^\n]{0,220}\b(list|output|rows?|items?|values?|count|metric|result|results|section|marker|active|line|lines|item|row|items)\b",
            task_part,
        )
        or re.search(
            r"\brenders?\b[^\n]{0,220}\b(list|output|rows?|items?|values?|count|metric|result|results|section|marker|active|line|lines|item|row|items)\b[^\n]{0,160}\b(cli|python|shell|command|cron)\b",
            task_part,
        )
        or re.search(
            r"\b(cli|python|shell|command|cron|report)\b[^\n]{0,240}\b(list|output|rows?|items?|values?|count|metric|result|results|section|marker|active|line|lines|item|row)\b[^\n]{0,240}\brenders?\b",
            task_part,
        )
    ):
        return False
    return True


GOAL_JUDGE_PROVIDER_ERROR_RE = re.compile(
    r"(goal[-_ ]?judge|goal[-_ ]?mode)[\s\S]{0,400}?(gemini|notfound|provider[-_ ]?error)",
    flags=re.I,
)


def _goal_judge_provider_error(task_part: str, raw: str) -> bool:
    """True when this card is a goal-judge/goal-mode provider-error trap card.

    Mirrors the shell hook's fail-closed trap detection so classification and
    the hook agree on which cards belong to the quarantine lane.
    """
    return bool(GOAL_JUDGE_PROVIDER_ERROR_RE.search(f"{task_part}\n{raw}"))


def _verified_review_with_evidence_override(task_part: str, raw: str) -> bool:
    """Only allow goal-judge provider-error completion traps to bypass the
    normal block path when there is explicit local evidence of terminal review
    state attached to this completion payload or task comments. Without that,
    provider-error tasks must fail closed into the operator quar override lane.
    The marker is intentionally strict to avoid accidental auto-completion.
    """
    text = f"{task_part}\n{raw}"
    if not re.search(r"GOAL_JUDGE_VERIFIED_REVIEW_OVERRIDE", text, flags=re.I):
        return False
    if not re.search(r"REVIEW_VERDICT\s*[:=]\s*APPROVED", text, flags=re.I):
        return False
    if not re.search(r"DIAGNOSTIC_VERDICT|TASK_EVIDENCE|task-evidence|task evidence", text, flags=re.I):
        return False
    # Negated/incomplete override prose ("... only; missing REVIEW_VERDICT=APPROVED
    # and reviewed task evidence markers") must NOT satisfy the override.
    if re.search(
        r"\b(missing|without|lacks?|absent|incomplete)\b[^\n]{0,80}"
        r"(GOAL_JUDGE_VERIFIED_REVIEW_OVERRIDE|REVIEW_VERDICT|task[- ]evidence)",
        text,
        flags=re.I,
    ):
        return False
    return True


def _profile_identifier_only_web_signal(task_part: str) -> bool:
    """Ignore WEB_PATTERNS hits embedded in profile names, narrowly.

    A compound profile identifier such as ``upero-ui-builder`` is not an app
    surface.  Only profile/roster administration context can activate this
    scrub, and any remaining web vocabulary or app implementation signal keeps
    the hard web classification.
    """
    if not _any(PROFILE_ADMIN_CONTEXT_PATTERNS, task_part):
        return False
    scrubbed = _profile_identifier_signal_text(task_part)
    if scrubbed == task_part:
        return False
    return not _matches_category(FRONTEND_WEB_TASK_CATEGORY, scrubbed) and not _has_app_impl(scrubbed)


def _web_surface(task_part: str, raw: str) -> bool:
    return _matches_category(FRONTEND_WEB_TASK_CATEGORY, task_part) and not _profile_identifier_only_web_signal(
        task_part
    )


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
        name="ALLOW_REVIEW_VERDICT_SOURCE_PR_REVIEW_NOT_WEB",
        decision="readonly_nonapp",
        predicate=_source_review_verdict_not_web,
        rationale="Review-verdict / source-PR-review cards that only quote app nouns must remain non-web for gate routing.",
    ),
    ContractRule(
        name="ALLOW_CRON_REPORT_CLI_RENDER_NOT_WEB",
        decision="not_web",
        predicate=_cron_report_cli_render_not_web,
        rationale="Cron/report/CLI render/list-output phrasing should not imply a browser/runtime surface by default.",
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
        name="ALLOW_VERIFIED_REVIEW_WITH_EVIDENCE_OVERRIDE",
        decision=OPERATIONAL_NONCODE_TASK_CATEGORY.decision,
        predicate=_verified_review_with_evidence_override,
        rationale="verified review with explicit evidence override marker may bypass the goal-judge provider-error quarantine lane only when local completion evidence contains REVIEW_VERDICT=APPROVED plus reviewed task evidence",
    ),    ContractRule(
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
