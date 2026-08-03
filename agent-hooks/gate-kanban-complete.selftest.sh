#!/usr/bin/env bash
# Regression coverage for gate-kanban-complete.sh classification.
# Uses an isolated temp kanban root via HERMES_HOOK_KANBAN_ROOT; does not touch live boards.
# Run standalone:         bash agent-hooks/gate-kanban-complete.selftest.sh
# Run full suite:         bash agent-hooks/run-selftests.sh
#
# For test command documentation, see: agent-hooks/verdict-router.fixture-matrix.md
set -euo pipefail
hook="${1:-/home/frank/.hermes/agent-hooks/gate-kanban-complete.sh}"
tmp=$(mktemp -d)
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

mkdir -p "$tmp/kanban/boards/test"
db="$tmp/kanban/boards/test/kanban.db"
sqlite3 "$db" <<'SQL'
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT);
CREATE TABLE task_comments (task_id TEXT, body TEXT, created_at INTEGER);
INSERT INTO tasks VALUES (
  't_pmroute',
  'REVIEW: Meilisearch product-search foundation handoff from t_552f2d3f',
  'Upero PM task completed its actual PM routing work. Scope: prevent frontend VERIFY_PASS gate on PM review-routing tasks/review-lane tasks that do not modify frontend/web/app surfaces. Evidence: verified handoff, unlinked dependency-stalled guardian child, and verified review child running on guardian. No app/product code changes are requested.'
);
INSERT INTO tasks VALUES (
  't_webwork',
  'Build marketplace frontend page',
  'Implement apps/web marketplace React page and route component.'
);
INSERT INTO tasks VALUES (
  't_webpass',
  'Build marketplace frontend page',
  'Implement apps/web marketplace React page and route component.'
);
INSERT INTO tasks VALUES (
  't_web_linked_packet',
  'Review marketplace frontend page with linked terminal packet',
  'Platform-reviewer is reviewing concrete apps/web marketplace React page and route component work. A terminal-capable RUNNING_APP_VERIFICATION packet may satisfy only the running-app evidence requirement.'
);
INSERT INTO tasks VALUES (
  't_web_negated_packet_discussion',
  'Build frontend route with delegated evidence packet',
  'Implementation worker changed concrete apps/web dashboard route component and page layout. It may mention delegated evidence, but cannot self-approve or bypass running-app evidence.'
);
INSERT INTO tasks VALUES (
  't_cliscript',
  'REPAIR: non-web kanban completion gate false positive for CLI/script tasks',
  'Investigate a Hermes CLI/Python/shell worker-visibility preflight. Acceptance requires command evidence such as py_compile, bash -n, pytest, and temp JSON; no frontend/web route, page, component, middleware, layout, tRPC path, or browser UI is touched.'
);
INSERT INTO tasks VALUES (
  't_web_cli_helper',
  'Build frontend page with CLI/script helper',
  'Implement apps/web dashboard route component. Use a CLI/script helper to seed fixture data before serving the React page.'
);
INSERT INTO tasks VALUES (
  't_web_python_helper',
  'Build frontend page with Python/shell helper',
  'Implement apps/web marketplace route component. Use Python/shell commands to generate fixture data before serving the React page.'
);
INSERT INTO tasks VALUES (
  't_frontend_nonweb_cli_helper',
  'Build frontend route with non-web CLI/script helper',
  'Implement apps/web dashboard route component. The worker may use a non-web CLI/script helper for fixture preflight, but this is still true frontend/UI work.'
);
INSERT INTO tasks VALUES (
  't_frontend_nonui_python_preflight',
  'Build frontend page with non-UI Python/shell preflight',
  'Implement apps/web marketplace page component and layout. Use a non-UI Python/shell preflight helper before checking the route.'
);
INSERT INTO tasks VALUES (
  't_frontend_completion_gate_cli',
  'Build app route with completion-gate CLI/script support',
  'Implement apps/web route component. A CLI/script completion-gate support helper is part of the workflow, but VERIFY_PASS is still required.'
);
INSERT INTO tasks VALUES (
  't_frontend_false_positive_python',
  'Fix frontend component with false-positive Python/shell classification note',
  'Update apps/web React component and page layout. The handoff mentions Python/shell false-positive classification support, but the app surface changed.'
);
INSERT INTO tasks VALUES (
  't_pkg_types_review',
  'REVIEW: delivery Zod cleanup reconciliation gate failure',
  'Review a TypeScript gate/policy failure for packages/types/src/delivery.ts. The diff under review is only packages/types/src/delivery.ts with z.record cleanup; apps/api tsc output mentions src/trpc/routers but this review task does not touch or approve a running app route.'
);
INSERT INTO tasks VALUES (
  't_frontend_pkg_types_route',
  'Build frontend route using packages/types delivery schema',
  'Implement apps/web delivery dashboard route component using packages/types/src/delivery.ts schema output. This is concrete frontend UI work and must still prove the running route.'
);
INSERT INTO tasks VALUES (
  't_repo_hygiene',
  'UNBLOCK: reconcile dirty Upero checkout for Yorkstone CMS block wave',
  'Repo-hygiene checkout-reconciliation task: classify dirty/untracked paths including apps/web/src/lib/payload-client.test.ts, update .git/info/exclude for AGENTS.md and wt/, create a reversible stash, verify git status clean, and update Obsidian note/log. No app/runtime route, page, component, middleware, API, tRPC, auth/tenant, layout, or frontend runtime surface was modified.'
);
INSERT INTO tasks VALUES (
  't_frontend_repo_hygiene_route',
  'Fix frontend route after repo-hygiene cleanup',
  'Implement apps/web marketplace route component and page layout after repo-hygiene checkout cleanup. The worker may use git status and a reversible stash, but this is concrete frontend UI work and must still prove the running route.'
);
INSERT INTO tasks VALUES (
  't_static_obsidian_qa_packet',
  'Assemble QA packet and comment evidence path',
  'Goal: turn completed visual/content evidence and static-only boundary audit into the final lightweight visual/content QA packet. Create a clear Markdown packet under /home/frank/obsidian-fleet-vault/Operations/ with screenshots/render evidence links, blog-category notes, job-tile notes, explicit static-only/forbidden-capability confirmations, and no app/runtime route, page, component, middleware, API, tRPC, auth/tenant, layout, or frontend runtime surface modified.'
);
INSERT INTO tasks VALUES (
  't_frontend_static_packet_route',
  'Build frontend route with static QA packet output',
  'Implement apps/web marketplace route component and page layout, then produce a static visual/content QA packet. This changed a concrete page-dependent API path and frontend route, so it must still prove the running route.'
);
INSERT INTO task_comments VALUES (
  't_web_linked_packet',
  'RUNNING_APP_VERIFICATION evidence_packet=t_terminal_packet_001 producer=platform-reviewer terminal_capable=true command="bash /home/frank/.hermes/scripts/verify-running-app.sh http://127.0.0.1:4300 /marketplace upero.localhost"\nVERIFY_PASS /marketplace :: HTTP 200, 39647b, real content\nNote: this packet satisfies only the running-app evidence requirement; reviewer must still provide a separate REVIEW_VERDICT.',
  1783072500
);
INSERT INTO task_comments VALUES (
  't_web_negated_packet_discussion',
  'implementation-worker delegated evidence discussion: bare VERIFY_PASS quoted as an example, but no RUNNING_APP_VERIFICATION packet from a terminal-capable reviewer and no independent reviewer verdict.',
  1783072501
);
SQL

fixtures="$(dirname "$hook")/gate-kanban-complete.fixtures.json"
matrix="$(dirname "$hook")/gate-kanban-complete.fixture-matrix.md"
classifier="$(dirname "$hook")/gate-kanban-complete-classifier.py"

python3 - "$fixtures" "$matrix" "$classifier" <<'PY'
import json, re, sys

fixtures_path, matrix_path, classifier_path = sys.argv[1:]
fixtures = json.load(open(fixtures_path, encoding="utf-8"))
matrix = open(matrix_path, encoding="utf-8").read().lower()
classifier = open(classifier_path, encoding="utf-8").read().lower()

required_doc_patterns = {
    "future non-app allow override": r"future\s+non-app\s+allow\s+override",
    "paired frontend/app negative fixtures": r"paired\s+frontend/app\s+negative\s+fixtures",
    "changed-files-aware tests": r"changed-files-aware\s+tests",
}
for label, pattern in required_doc_patterns.items():
    if not re.search(pattern, classifier):
        raise SystemExit(f"maintenance contract missing from classifier docstring: {label}")

for marker in ("paired frontend/app negative fixtures", "changed-files-aware coverage", "changed_files"):
    if marker not in matrix:
        raise SystemExit(f"maintenance contract missing from fixture matrix: {marker}")

has_nonapp_allow = any(
    fixture.get("expected") == "allow" and fixture.get("expected_class") == "readonly_nonapp"
    for fixture in fixtures
)
has_frontend_negative = any(
    fixture.get("expected") == "block" and fixture.get("expected_class") == "web" and re.search(
        r"paired|negative|frontend|apps/web",
        " ".join(str(fixture.get(key, "")) for key in ("name", "title", "body", "description")).lower(),
    )
    for fixture in fixtures
)
has_changed_files_web_negative = any(
    fixture.get("expected") == "block"
    and fixture.get("expected_class") == "web"
    and any(str(path).startswith("apps/web/") for path in fixture.get("metadata", {}).get("changed_files", []))
    for fixture in fixtures
)
if not has_nonapp_allow:
    raise SystemExit("maintenance contract missing readonly_nonapp allow fixture")
if not has_frontend_negative:
    raise SystemExit("maintenance contract missing paired frontend/app negative fixture")
if not has_changed_files_web_negative:
    raise SystemExit("maintenance contract missing changed_files-aware app-surface negative fixture")

print("PASS maintenance-contract: paired negatives and changed_files-aware guardrails present")
PY

python3 - "$db" "$fixtures" <<'PY'
import json, sqlite3, sys

db_path, fixtures_path = sys.argv[1:3]
fixtures = json.load(open(fixtures_path, encoding="utf-8"))
with sqlite3.connect(db_path) as conn:
    for fixture in fixtures:
        conn.execute(
            "INSERT OR REPLACE INTO tasks (id, title, body) VALUES (?, ?, ?)",
            (fixture["task_id"], fixture["title"], fixture["body"]),
        )
PY

payload() {
  local tid="$1"
  local summary="${2:-done}"
  local metadata_json="${3:-}"
  python3 - "$tid" "$summary" "$metadata_json" <<'PY'
import json, sys
metadata = None
if len(sys.argv) > 3 and sys.argv[3]:
    metadata = json.loads(sys.argv[3])
tool_input = {"task_id": sys.argv[1], "board": "test", "summary": sys.argv[2]}
if metadata is not None:
    tool_input["metadata"] = metadata
print(json.dumps({
    "tool_name": "kanban_complete",
    "tool_input": tool_input,
}))
PY
}

run_case() {
  local name="$1" tid="$2" expected="$3" summary="${4:-done}"
  local metadata_json="${5:-}"
  local out decision
  out=$(payload "$tid" "$summary" "$metadata_json" | HERMES_HOOK_KANBAN_ROOT="$tmp" bash "$hook")
  decision=$(python3 - <<'PY' "$out"
import json, sys
try:
    print((json.loads(sys.argv[1]) or {}).get("decision", "allow"))
except Exception:
    print("parse_error")
PY
)
  if [ "$decision" != "$expected" ]; then
    printf 'FAIL %s: expected %s got %s output=%s\n' "$name" "$expected" "$decision" "$out" >&2
    exit 1
  fi
  printf 'PASS %s: %s\n' "$name" "$decision"
}

run_classifier_case() {
  local name="$1" tid="$2" expected_class="$3" summary="${4:-done}"
  local metadata_json="${5:-}"
  local class
  class=$(python3 - "$db" "$tid" "$summary" "$metadata_json" "$hook" <<'PY'
import json, sqlite3, subprocess, sys

db_path, task_id, summary, metadata_json, hook_path = sys.argv[1:]
with sqlite3.connect(db_path) as conn:
    row = conn.execute(
        "SELECT title, COALESCE(body, '') FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
if row is None:
    raise SystemExit(f"missing fixture task {task_id}")
title, body = row
parts = [summary] if summary else []
if metadata_json:
    parts.append(json.dumps(json.loads(metadata_json), sort_keys=True))
raw = f"{title}\n---BODY---\n{body}\n---COMMENTS---\n\n---INPUT---\n" + "\n".join(parts)
classifier = hook_path.rsplit("/", 1)[0] + "/gate-kanban-complete-classifier.py"
result = subprocess.run(
    ["python3", classifier],
    input=raw,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=True,
)
print(result.stdout.strip())
PY
)
  if [ "$class" != "$expected_class" ]; then
    printf 'FAIL %s: expected classifier %s got %s\n' "$name" "$expected_class" "$class" >&2
    exit 1
  fi
  printf 'PASS %s classifier: %s\n' "$name" "$class"
}

run_case "pm-review-routing-nonapp-allows" t_pmroute allow
run_case "cli-script-nonweb-command-evidence-allows" t_cliscript allow 'py_compile rc=0; bash -n rc=0; pytest 6 passed; temp JSON read back'
run_case "frontend-without-verify-blocks" t_webwork block
run_case "frontend-cli-script-helper-without-verify-blocks" t_web_cli_helper block
run_case "frontend-python-shell-helper-without-verify-blocks" t_web_python_helper block
run_case "frontend-nonweb-cli-helper-without-verify-blocks" t_frontend_nonweb_cli_helper block
run_case "frontend-nonui-python-preflight-without-verify-blocks" t_frontend_nonui_python_preflight block
run_case "frontend-completion-gate-cli-without-verify-blocks" t_frontend_completion_gate_cli block
run_case "frontend-false-positive-python-without-verify-blocks" t_frontend_false_positive_python block
run_case "packages-types-review-nonapp-allows" t_pkg_types_review allow 'REVIEW_VERDICT: BLOCKED for packages/types/src/delivery.ts TypeScript gate; no running app route touched'
run_case "frontend-packages-types-route-without-verify-blocks" t_frontend_pkg_types_route block
run_case "repo-hygiene-checkout-reconciliation-allows" t_repo_hygiene allow 'git status clean; .git/info/exclude updated; reversible stash created; Obsidian note/log updated; no app/runtime route surface changed'
run_case "frontend-repo-hygiene-route-without-verify-blocks" t_frontend_repo_hygiene_route block
run_case "static-obsidian-qa-packet-allows" t_static_obsidian_qa_packet allow 'AD_HOC_VERIFY_PASS packet/log markers; artifact exists; forbidden marker scan PASS; no live route exists'
run_case "frontend-static-packet-route-without-verify-blocks" t_frontend_static_packet_route block 'AD_HOC_VERIFY_PASS packet markers only; no running app VERIFY_PASS supplied'
run_case "frontend-with-verify-allows" t_webpass allow 'VERIFY_PASS /marketplace :: HTTP 200, real content'
run_case "frontend-linked-running-app-packet-comment-allows" t_web_linked_packet allow 'REVIEW_VERDICT: APPROVE_WITH_NOTES; running-app evidence is explicitly linked via terminal-capable packet t_terminal_packet_001 in task comments.'
run_case "frontend-negated-running-app-packet-discussion-blocks" t_web_negated_packet_discussion block 'Implementation worker claims DELEGATED_EVIDENCE_PACKET=self and REVIEW_VERDICT: APPROVE by self; no RUNNING_APP_VERIFICATION packet and no actual VERIFY_PASS evidence supplied.'
run_case "goal-judge-provider-error-no-override-blocks" t_goal_judge_provider_error_fixture block 'GeminiAPIError/NotFoundError preserved; no override marker; needs terminal evidence/review handoff'
run_case "goal-judge-provider-error-incomplete-override-blocks" t_goal_judge_incomplete_override_fixture block 'Incomplete override marker; failure lane preserved'
run_case "goal-judge-provider-error-verified-review-override-allows" t_goal_judge_verified_override_fixture allow 'REVIEW_VERDICT=APPROVED with reviewed task evidence path in metadata; GOAL_JUDGE_VERIFIED_REVIEW_OVERRIDE present'
run_case "goal-judge-provider-error-frontend-negative-blocks" t_goal_judge_frontend_negative_fixture block 'apps/web frontend task; GOAL_JUDGE_VERIFIED_REVIEW_OVERRIDE wording is present but no running-app VERIFY_PASS supplied'

while IFS=$'\t' read -r name tid expected summary metadata_json expected_class; do
  run_case "fixture-corpus:${name}" "$tid" "$expected" "$summary" "$metadata_json"
  if [ -n "${expected_class:-}" ]; then
    run_classifier_case "fixture-corpus:${name}" "$tid" "$expected_class" "$summary" "$metadata_json"
  fi
done < <(python3 - "$fixtures" <<'PY'
import json, sys

for fixture in json.load(open(sys.argv[1], encoding="utf-8")):
    print("\t".join([
        fixture["name"],
        fixture["task_id"],
        fixture["expected"],
        fixture.get("summary", "done").replace("\t", " ").replace("\n", " "),
        json.dumps(fixture.get("metadata", {}), sort_keys=True) if fixture.get("expected_class") else (json.dumps(fixture.get("metadata", None), sort_keys=True) if "metadata" in fixture else ""),
        fixture.get("expected_class", ""),
    ]))
PY
)

echo "gate-kanban-complete self-test PASS"
