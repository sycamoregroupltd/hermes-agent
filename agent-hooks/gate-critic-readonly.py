#!/usr/bin/env python3
# Logic for gate-critic-readonly.sh — pre_tool_call hook for CRITIC/REVIEWER profiles.
# Enforces "a critic never mutates the artifact it judges" as a HARD runtime guarantee.
# Reviewers stay able to READ everything, RUN read-only verification (tests/typecheck/build),
# post kanban verdicts, and write review NOTES to the Obsidian vault — only artifact MUTATION
# is blocked. Contract: read pre_tool_call JSON on stdin; print {} to allow or
# {"decision":"block","reason":...} to veto. FAIL-OPEN only on genuine parse ambiguity.
#
# 2026-06-29 hardening (post re-score finding): the gate keyed on a fixed WRITE_TOOLS name set,
# so MCP/dgx_write_file/unknown write tools could slip through. Now it ALSO blocks by:
#   - broadened content/path key detection (covers data/body/contents/file/target/dest...),
#   - a tool-NAME write-pattern catch (write|edit|create|patch|put|upload|replace|apply|delete|
#     str_replace), including namespaced MCP tools (mcp__*, dgx_write_file, ...),
# so a write-shaped call to a non-vault path is blocked regardless of which tool issued it.
import json, sys, re, os, datetime

LOG = "/home/frank/.hermes/cron/state/critic-readonly-gate.log"
# Known explicit write/edit tools (still useful for content-less write tools).
WRITE_TOOLS = {"create_file", "apply_patch", "str_replace", "str_replace_editor",
               "write_file", "edit_file", "file_write", "fs_write", "patch_file",
               "edit", "write", "dgx_write_file"}
# Tool-NAME pattern: any tool (incl namespaced MCP like mcp__dgx__dgx_write_file) that writes.
WRITE_NAME = re.compile(r"(write|edit|create|patch|put|upload|replace|apply_?patch|delete|truncate|mkdir|move|rename|chmod)", re.I)
# Tool names that are read/searchy even though they may match WRITE_NAME loosely — never block these.
READ_NAME = re.compile(r"(read|get|list|search|find|show|view|cat|grep|status|diff|log|fetch|describe|query)", re.I)
# Exact control-plane routing tools that create/advance kanban work without mutating
# the artifact under review. Keep this exact-name only: namespaced/deceptive tool
# names still flow through WRITE_NAME and fail closed, while gate-kanban-dupe-create
# remains the separate hard gate for kanban_create payload safety.
CONTROL_PLANE_ROUTE_TOOLS = {"kanban_create"}
# Paths a critic MAY write to (review notes, scratch).
ALLOW_WRITE = re.compile(r"(obsidian-fleet-vault|obsidian/quant-team|/tmp/|/scratch/|REFLECTION\.md|\.hermes/cron/state/)")
# VCS state-mutation + in-place source edits via shell.
MUTATE_CMD = re.compile(
    r"(git\s+(commit|add|push|reset|checkout|switch|merge|rebase|stash|cherry-pick|apply|revert)\b"
    r"|sed\s+-i|perl\s+-i\b|patch\s+<|apply_patch|tee\s+[^|&]*\.)", re.I)
CONTENT_KEYS = ("content", "new_str", "new_string", "text", "file_text", "patch",
                "data", "body", "contents", "value", "new_text", "source")
PATH_KEYS = ("path", "file_path", "filename", "file", "target", "dest", "destination", "filepath", "uri")

def emit(o): sys.stdout.write(json.dumps(o)); sys.exit(0)
def allow(): emit({})
def block(reason, profile, tool, tgt):
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} BLOCK profile={profile} tool={tool} tgt={tgt}\n")
    except Exception:
        pass
    emit({"decision": "block", "action": "block", "reason": reason, "message": reason})

try:
    d = json.load(sys.stdin)
    assert isinstance(d, dict)
except Exception:
    allow()  # genuine parse ambiguity -> fail-open (never wedge the fleet)

tool = (d.get("tool_name") or "").strip()
ti = d.get("tool_input") or d.get("args") or {}
if not isinstance(ti, dict): allow()
ex = d.get("extra") if isinstance(d.get("extra"), dict) else {}
profile = ex.get("profile") or d.get("profile") or os.environ.get("HERMES_PROFILE") or "?"

if tool in CONTROL_PLANE_ROUTE_TOOLS:
    allow()

path = ""
for k in PATH_KEYS:
    if ti.get(k):
        path = str(ti.get(k)); break
has_content = any(ti.get(k) is not None for k in CONTENT_KEYS)
cmd = ti.get("command") or ti.get("cmd") or ti.get("script") or ""
if isinstance(cmd, list): cmd = " ".join(map(str, cmd))
cmd = str(cmd)

# short tool basename for name matching (mcp__dgx__dgx_write_file -> dgx_write_file)
tool_base = tool.split("__")[-1] if tool else ""
is_write_tool = (tool_base in WRITE_TOOLS) or (
    bool(WRITE_NAME.search(tool_base)) and not READ_NAME.search(tool_base))

REASON = ("Critic read-only gate: a reviewer must not MUTATE the artifact it judges ({tgt}). "
          "Read, run read-only verification (tests/typecheck/build-check), and post your verdict via "
          "kanban_comment/kanban_block — do not edit code, commit, push, or change the work under review. "
          "Route fixes back to the builder; independence is the point.")

# A) any write-shaped tool call (explicit set, content+path, or write-name pattern) to a non-allowed path
if (is_write_tool or has_content) and path:
    if not ALLOW_WRITE.search(path):
        block(REASON.format(tgt=path), profile, tool or "file", path)
# A2) write-name tool with content but NO recognized path key -> still a mutation attempt; fail-closed
if is_write_tool and has_content and not path:
    block(REASON.format(tgt=f"{tool} (no path arg)"), profile, tool or "file", "<no-path>")

# B) shell command that mutates VCS/source in place and is not confined to an allowed zone
if cmd and MUTATE_CMD.search(cmd):
    if not ALLOW_WRITE.search(cmd):
        block(REASON.format(tgt=cmd[:80]), profile, tool or "terminal", cmd[:80])

allow()

