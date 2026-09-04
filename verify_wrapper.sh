#!/usr/bin/env bash
# Source-only harness for the exact wrapper in this branch.
set -euo pipefail
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
cp profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh "$root/wrapper.sh"
cat >"$root/kanban_classify_failure_recent.py" <<'EOF'
#!/usr/bin/env python3
import os
raise SystemExit(int(os.environ.get("DIAG_RC", "0")))
EOF
cat >"$root/reaper.py" <<'EOF'
#!/usr/bin/env python3
import os
raise SystemExit(int(os.environ.get("REAPER_RC", "0")))
EOF
chmod +x "$root/kanban_classify_failure_recent.py" "$root/wrapper.sh"
# Redirect both stage paths to harmless stubs; no live board or cron state is touched.
sed -i "s#\${SCRIPT_DIR}/kanban_classify_failure_recent.py#$root/kanban_classify_failure_recent.py#" "$root/wrapper.sh"
sed -i "s#/home/frank/.hermes/scripts/dead_pid_blocked_reaper.py#$root/reaper.py#" "$root/wrapper.sh"

run_case() {
  local diag=$1 reaper=$2 expected=$3 label=$4
  local stderr rc
  stderr=$(mktemp -p "$root")
  set +e
  DIAG_RC=$diag REAPER_RC=$reaper "$root/wrapper.sh" 2>"$stderr"
  rc=$?
  set -e
  [[ "$rc" == "$expected" ]] || { printf 'FAIL %s: rc=%s expected=%s\n' "$label" "$rc" "$expected"; return 1; }
  if [[ "$expected" == 1 ]]; then
    grep -F "stage failure diag rc=$diag reaper rc=$reaper" "$stderr" >/dev/null || { printf 'FAIL %s: missing marker\n' "$label"; return 1; }
  else
    [[ ! -s "$stderr" ]] || { printf 'FAIL %s: unexpected stderr\n' "$label"; return 1; }
  fi
  printf 'PASS %s: diag=%s reaper=%s wrapper=%s\n' "$label" "$diag" "$reaper" "$rc"
  rm -f "$stderr"
}

run_case 0 0 0 clean-no-op
run_case 1 0 1 diagnostics-only-failure
run_case 0 1 1 reaper-only-failure
run_case 1 1 1 both-stages-fail
for attempt in 1 2 3; do
  run_case 1 0 1 "repeated-diagnostics-failure-$attempt"
  run_case 0 1 1 "repeated-reaper-failure-$attempt"
done
bash -n profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh
printf 'PASS shell-syntax\n'
printf 'wrapper-sha256 '
sha256sum profiles/jarvis/scripts/kanban_classify_failure_and_reaper.sh
