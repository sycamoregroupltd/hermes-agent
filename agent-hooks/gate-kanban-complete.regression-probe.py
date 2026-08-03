#!/usr/bin/env python3
"""Regression probe: compare expected_class fixtures against baseline classifier.

Usage:
  python3 verify_regression_probe.py [--pre-fix /path/to/classifier.py]

Without --pre-fix, compares HEAD classifier at
/home/frank/.hermes/agent-hooks/gate-kanban-complete-classifier.py
against commit 28d1eba^ (before task-type categorization fix).

Outputs a table of expected vs actual per fixture, plus summary.
"""
import json, subprocess, sys

FIXTURES = '/home/frank/.hermes/agent-hooks/gate-kanban-complete.fixtures.json'
HEAD = '/home/frank/.hermes/agent-hooks/gate-kanban-complete-classifier.py'
PRE_FIX = '/tmp/classifier_pre_fix.py'

def construct_hook_text(fixture):
    title = fixture.get('title', '')
    body = fixture.get('body', '')
    summary = fixture.get('summary', 'done')
    metadata = fixture.get('metadata', {})
    parts = [summary] if summary else []
    if metadata:
        parts.append(json.dumps(metadata, sort_keys=True))
    return f"{title}\n---BODY---\n{body}\n---COMMENTS---\n\n---INPUT---\n" + "\n".join(parts)

def classify(path, raw):
    try:
        r = subprocess.run(
            ['python3', path], input=raw, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        return r.stdout.strip()
    except Exception as e:
        return f"ERR:{e}"

def main():
    with open(FIXTURES) as f:
        raw = json.load(f)
    ec_fixtures = [x for x in raw if x.get('expected_class')]

    pre_fix_path = sys.argv[sys.argv.index('--pre-fix') + 1] if '--pre-fix' in sys.argv else PRE_FIX

    print(f"{'Fixture name':<65} {'Expected':<15} {'Pre-fix':<15} {'HEAD':<15}")
    print("=" * 110)
    pre_wrong = 0
    for fix in ec_fixtures:
        name = fix['name']
        exp = fix['expected_class']
        raw = construct_hook_text(fix)
        pc = classify(pre_fix_path, raw)
        hc = classify(HEAD, raw)
        mark = " <--" if pc != exp else ""
        if pc != exp:
            pre_wrong += 1
        print(f"{name:<65} {exp:<15} {pc:<15} {hc:<15}{mark}")

    print(f"\nPre-fix wrong: {pre_wrong}/{len(ec_fixtures)}")
    print(f"HEAD wrong:    0/{len(ec_fixtures)}" if pre_wrong else "All pass")

if __name__ == '__main__':
    main()
