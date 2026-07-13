#!/usr/bin/env python3
"""
Test circuit breaker suppression: create sample entry with 3 false-positives,
confirm suppression activates after the 3rd consecutive FP.

This exercises the actual circuit breaker functions from the gap analyzer,
not a mock/replica.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Monkey-patch paths BEFORE importing the gap analyzer functions
# so it uses temporary paths, not the real circuit breaker.
TEST_DIR = Path(tempfile.mkdtemp(prefix="cb_test_"))
TEST_CB_PATH = TEST_DIR / "test-circuit-breaker.json"
TEST_OUTPUT_PATH = TEST_DIR / "test-gap-analysis.md"

# Patch module-level globals
import research_impl_gap_analyzer as ga

# Save originals
_orig_cb_path = ga.CIRCUIT_BREAKER_PATH
_orig_out_path = ga.OUTPUT_PATH

# Patch to use test paths
ga.CIRCUIT_BREAKER_PATH = TEST_CB_PATH
ga.OUTPUT_PATH = TEST_OUTPUT_PATH

from ga import (
    load_circuit_breaker,
    save_circuit_breaker,
    record_classification,
    is_suppressed,
    get_cb_entry,
)

print("=" * 60)
print("Circuit Breaker Suppression Test")
print("=" * 60)

test_board = "upero"
test_task_id = "t_test_suppression"
test_key = f"{test_board}/{test_task_id}"
pas = 0
fails = 0

def check(condition: bool, msg: str):
    global pas, fails
    if condition:
        print(f"  ✅ PASS: {msg}")
        pas += 1
    else:
        print(f"  ❌ FAIL: {msg}")
        fails += 1

# ── Phase 1: Fresh circuit breaker ──────────────────────────────────
print("\n1. Fresh circuit breaker — no entries yet")
state = load_circuit_breaker()
assert isinstance(state, dict), "State should be a dict"
assert len(state) == 0, f"Fresh state should be empty, got {len(state)} entries"
check(len(state) == 0, "Fresh circuit breaker is empty")

# ── Phase 2: Record 3 consecutive false-positives ──────────────────
print("\n2. Recording 3 consecutive false-positives...")

# FP #1
record_classification(state, test_task_id, test_board, "false_positive", [])
entry = get_cb_entry(state, test_task_id, test_board)
check(entry["false_positive_count"] == 1, f"FP count = 1 (got {entry['false_positive_count']})")
check(entry["suppressed"] == False, "Suppressed = False after 1 FP")
print(f"   Entry after 1 FP: FP={entry['false_positive_count']}, suppressed={entry['suppressed']}")

# FP #2
record_classification(state, test_task_id, test_board, "false_positive", [])
entry = get_cb_entry(state, test_task_id, test_board)
check(entry["false_positive_count"] == 2, f"FP count = 2 (got {entry['false_positive_count']})")
check(entry["suppressed"] == False, "Suppressed = False after 2 FP")
print(f"   Entry after 2 FP: FP={entry['false_positive_count']}, suppressed={entry['suppressed']}")

# FP #3 — suppression should activate
record_classification(state, test_task_id, test_board, "false_positive", [])
entry = get_cb_entry(state, test_task_id, test_board)
check(entry["false_positive_count"] == 3, f"FP count = 3 (got {entry['false_positive_count']})")
check(entry["suppressed"] == True, "Suppressed = True after 3 FP")
check(entry["suppressed_reason"] is not None, "Suppressed reason is set")
check(
    "≥3 consecutive false-positives" in entry["suppressed_reason"],
    f"Suppressed reason mentions ≥3 consecutive (got: {entry['suppressed_reason']})"
)
print(f"   Entry after 3 FP: FP={entry['false_positive_count']}, suppressed={entry['suppressed']}")
print(f"   Reason: {entry['suppressed_reason']}")

# ── Phase 3: Verify is_suppressed returns True ─────────────────────
print("\n3. Verifying is_suppressed() detects the entry...")
suppressed, reason = is_suppressed(state, test_task_id, test_board)
check(suppressed == True, "is_suppressed returns True")
check(reason == entry["suppressed_reason"], "is_suppressed returns correct reason")

# ── Phase 4: Verify suppression persists across save/load ──────────
print("\n4. Verify suppression persists across save/load...")
save_circuit_breaker(state)
loaded_state = load_circuit_breaker()
suppressed, reason = is_suppressed(loaded_state, test_task_id, test_board)
check(suppressed == True, "Suppression survives save/load cycle")
check(
    loaded_state[test_key]["false_positive_count"] == 3,
    f"FP count=3 after reload (got {loaded_state[test_key]['false_positive_count']})"
)

# ── Phase 5: Override — non-FP classification resets counter ────────
print("\n5. Non-FP classification resets false positive counter...")
record_classification(state, test_task_id, test_board, "already_routed", [])
entry = get_cb_entry(state, test_task_id, test_board)
# The counter resets (but suppression already triggered). With non-FP,
# the counter should reset to 0. But suppression won't auto-clear
# unless the classification is non-FP AND suppressed was True.
# Let's check the counter reset behavior:
check(
    entry["false_positive_count"] == 0,
    f"FP count reset to 0 after non-FP (got {entry['false_positive_count']})"
)
# Suppression should clear when a non-FP classification comes in
# (see line 336-339 of the gap analyzer: "if classification != 'false_positive' and entry.get('suppressed')")
check(
    entry["suppressed"] == False,
    "Suppression cleared after non-FP classification"
)
print(f"   After non-FP: FP={entry['false_positive_count']}, suppressed={entry['suppressed']}")

# ── Phase 6: Re-trigger suppression (3 more FPs) ──────────────────
print("\n6. Re-trigger suppression (3 more FPs)...")
for i in range(3):
    record_classification(state, test_task_id, test_board, "false_positive", [])
entry = get_cb_entry(state, test_task_id, test_board)
check(entry["suppressed"] == True, "Suppression re-activated after 3 new FPs")
check(entry["false_positive_count"] == 3, f"FP count = 3 (got {entry['false_positive_count']})")

# ── Phase 7: Manual child creation clears suppression ──────────────
print("\n7. Manual child creation clears suppression...")
record_classification(state, test_task_id, test_board, "real_gap", ["t_child_new_001"])
entry = get_cb_entry(state, test_task_id, test_board)
check(entry["suppressed"] == False, "Suppression cleared by non-FP + new child")
check(
    "manual child creation" in (entry.get("suppressed_reason") or ""),
    "suppressed_reason mentions manual child creation"
)
check(
    "t_child_new_001" in entry.get("routed_child_ids", []),
    "New child id recorded in routed_child_ids"
)
print(f"   After manual child: suppressed={entry['suppressed']}")
print(f"   Routed children: {entry.get('routed_child_ids')}")

# ── Summary ────────────────────────────────────────────────────────
print()
print("=" * 60)
total = pas + fails
print(f"Results: {pas}/{total} passed" + ("" if fails == 0 else f", {fails} FAILED"))
print("=" * 60)

# Cleanup test files
import shutil
shutil.rmtree(TEST_DIR, ignore_errors=True)

sys.exit(0 if fails == 0 else 1)
