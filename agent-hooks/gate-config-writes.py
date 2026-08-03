#!/usr/bin/env python3
# Logic for gate-config-writes.sh (kept as a file so the hook can pipe the JSON payload to
# stdin while this program is passed as an argument). Reads the pre_tool_call payload on stdin;
# prints {} to allow or a block JSON to veto. FAIL-OPEN on any error.
#
# Blocks an agent from writing/editing a Hermes config.yaml (profile or global ~/.hermes/config.yaml).
# Three detection cases:
#   A) a write/edit tool with a `path` arg pointing at a .hermes config.yaml
#   B) a shell command (terminal/bash) that edits a .hermes config.yaml (sed -i, tee, > , hermes config set)
#   C) an apply_patch/diff whose file-target header points at a .hermes config.yaml (edit of an existing config)
# App/project config.yaml files, plain reads, and docs that merely *mention* a config path are NOT blocked.
import json, sys, re, os, datetime
LOG = "/home/frank/.hermes/cron/state/config-write-gate.log"
CFG = re.compile(r"\.hermes/(?:profiles/[^/\s\"]+/)?config\.yaml")
WRITE_TOOLS = {"create_file", "apply_patch", "str_replace", "str_replace_editor",
               "write_file", "edit_file", "file_write", "fs_write", "patch_file"}

def emit(obj):
    sys.stdout.write(json.dumps(obj)); sys.exit(0)
def allow():
    emit({})
def block(reason, profile, tool, tgt):
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} BLOCK profile={profile} tool={tool} tgt={tgt}\n")
    except Exception:
        pass
    emit({"decision": "block", "action": "block", "reason": reason, "message": reason})

def collect(o):
    acc = []
    if isinstance(o, str): acc.append(o)
    elif isinstance(o, dict):
        for v in o.values(): acc += collect(v)
    elif isinstance(o, list):
        for v in o: acc += collect(v)
    return acc

try:
    d = json.load(sys.stdin)
except Exception:
    allow()
if not isinstance(d, dict):
    allow()

tool = (d.get("tool_name") or "").strip()
ti = d.get("tool_input") or d.get("args") or {}
if not isinstance(ti, dict):
    allow()
ex = d.get("extra") if isinstance(d.get("extra"), dict) else {}
profile = ex.get("profile") or d.get("profile") or os.environ.get("HERMES_PROFILE") or "?"

path = str(ti.get("path") or ti.get("file_path") or ti.get("filename") or "")
has_content = any(k in ti and ti.get(k) is not None
                  for k in ("content", "new_str", "new_string", "text", "file_text", "old_str", "patch"))
cmd = ti.get("command") or ti.get("cmd") or ti.get("script") or ""
if isinstance(cmd, list): cmd = " ".join(map(str, cmd))
cmd = str(cmd)

REASON = ("Config-write gate: editing a Hermes config ({tgt}) is blocked — provider/model/toolset "
          "config is human-managed (Frank). Surface drift to Frank; never edit or propagate. "
          "Set ALLOW_CONFIG_WRITE=1 for an approved repair.")

# Case A: file write/edit tool with a path arg targeting a .hermes config.yaml
if path and CFG.search(path):
    if has_content or re.search(r"write|edit|patch|replace|create|append|insert|str_replace|apply|save", tool, re.I):
        block(REASON.format(tgt=path), profile, tool or "file", path)

# Case B: shell command that edits a .hermes config.yaml
if cmd:
    m = CFG.search(cmd)
    if m and re.search(r"(sed\s+-i|tee\b|cat\s*>|>>?\s*[^|;&]*\.hermes|hermes\s+config\s+set|hermes\s+fallback\b|perl\s+-i|truncate\b)", cmd):
        block(REASON.format(tgt=m.group(0)), profile, tool or "terminal", m.group(0))

# Case C: apply_patch / diff whose file-target header points at a .hermes config.yaml
if tool in WRITE_TOOLS or has_content:
    for line in "\n".join(collect(ti)).splitlines():
        m = CFG.search(line)
        if m and re.search(r"(\*\*\*|\+\+\+|^---|\bFile:|Update File|Add File|Move (to|from)|Delete File)", line):
            block(REASON.format(tgt=m.group(0)), profile, tool or "patch", m.group(0))

allow()

