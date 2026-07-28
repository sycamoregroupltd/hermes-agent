#!/usr/bin/env python3
"""
Research-to-Implementation Gap Analyzer + Circuit Breaker.

Phase 1 of the automated research-to-implementation pipeline.
Reads research-actionables.md (scanner output), cross-references against:
- Existing kanban child cards (via Routed card IDs in the source)
- Existing Obsidian decision notes in Research/Reviews/
- Known false-positive patterns (template-text leakage)
- Circuit breaker state (false-positive suppression)

Output: gap-analysis.md to /home/frank/.hermes/data/research-impl-gap-analysis.md
Circuit breaker store: /home/frank/.hermes/data/research-impl-circuit-breaker.json

Usage:
    python3 /home/frank/.hermes/scripts/research-impl-gap-analyzer.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
ACTIONABLES_PATH = Path(
    "/home/frank/obsidian-fleet-vault/Research/Reviews/research-actionables.md"
)
DECISIONS_DIR = Path("/home/frank/obsidian-fleet-vault/Research/Reviews")
OUTPUT_PATH = Path("/home/frank/.hermes/data/research-impl-gap-analysis.md")
CIRCUIT_BREAKER_PATH = Path("/home/frank/.hermes/data/research-impl-circuit-breaker.json")

# ── Known false-positive patterns ─────────────────────────────────────
# Template-text leakage: generic acceptance-criteria boilerplate that is
# extracted from task bodies but doesn't represent real implementable work.
FALSE_POSITIVE_PATTERNS = [
    # Generic task boundary boilerplate
    re.compile(r"^Boundary:\s*PAPER-ONLY", re.IGNORECASE),
    re.compile(r"^Do not promote live", re.IGNORECASE),
    re.compile(r"^Do not apply DDL/DML/backfills", re.IGNORECASE),
    re.compile(r"^Hard bounds:\s*no live trading", re.IGNORECASE),
    
    # Generic acceptance criteria templates
    re.compile(r"^Acceptance criteria:\s*PM accepts/rejects/routes", re.IGNORECASE),
    re.compile(r"^Acceptance criteria:", re.IGNORECASE),
    
    # Generic process instructions
    re.compile(r"^If implementation is needed, produce source/test-only", re.IGNORECASE),
    re.compile(r"^If source fix is required, produce source/test-only", re.IGNORECASE),
    re.compile(r"^If source patch is required", re.IGNORECASE),
    re.compile(r"^Route any new evidence to review gate", re.IGNORECASE),
    re.compile(r"^Scope:\s*SOURCE/TEST ONLY", re.IGNORECASE),
    re.compile(r"^Required checks:\s*no loss of historical rows", re.IGNORECASE),
    re.compile(r"^Create review cards for trading-risk-reviewer", re.IGNORECASE),
    re.compile(r"^Do not change live trading behavior", re.IGNORECASE),
    re.compile(r"^No deploy/restart, no DB write", re.IGNORECASE),
    
    # Generic review/verification criteria
    re.compile(r"^Verify the read-only SQL packet", re.IGNORECASE),
    re.compile(r"^3\.\s*JWT token requirement", re.IGNORECASE),
    re.compile(r"^5\.\s*Are leak-free entry/exit semantics", re.IGNORECASE),
]

ACTIONABLE_VERBS = {
    "add", "adjust", "audit", "backfill", "build", "create", "dedupe",
    "document", "enforce", "fix", "group", "harden", "implement", "inspect",
    "land", "migrate", "patch", "persist", "reconcile", "refactor", "repair",
    "route", "run", "ship", "test", "update", "validate", "verify", "wire",
}
LOW_SIGNAL_PREFIX_RE = re.compile(r"(?i)^(and|or|but|then|also|where|vs\.?)\b")
MARKDOWN_FRAGMENT_RE = re.compile(r"^\s*(?:\|.*\||#{1,6}\s+.*|`{1,3}.*)$")


# ── Parsing ────────────────────────────────────────────────────────────

def parse_actionables(path: Path) -> list[dict]:
    """Parse research-actionables.md into structured item dicts.

    Each item starts with '### <board>/<task_id> — <title>' and contains
    key: value lines and bullet lists for Extracted actions / Routed card IDs.
    """
    if not path.exists():
        print(f"ERROR: Input file not found: {path}", file=sys.stderr)
        return []

    text = path.read_text()
    items = []

    # Split on item headings
    # Pattern: ### <board>/<task_id> — <title>
    heading_pattern = re.compile(r'^### (\S+)/(t_\S+)\s*[—–-]\s*(.+)$', re.MULTILINE)

    # Find all item boundaries
    positions = []
    for m in heading_pattern.finditer(text):
        positions.append({
            "start": m.start(),
            "board": m.group(1),
            "task_id": m.group(2),
            "title": m.group(3).strip(),
        })

    if not positions:
        print("WARNING: No items found in research-actionables.md", file=sys.stderr)
        return []

    # Extract each item's section (from heading start to next heading or end)
    for i, pos in enumerate(positions):
        end = positions[i + 1]["start"] if i + 1 < len(positions) else len(text)
        section = text[pos["start"]:end]

        item = {
            "board": pos["board"],
            "task_id": pos["task_id"],
            "title": pos["title"],
            "assignee": "",
            "completed": "",
            "extracted_actions": [],
            "routed_card_ids": [],
            "completion_summary": "",
            "raw_section": section,
        }

        # Extract metadata fields
        for line in section.split("\n"):
            line_stripped = line.strip()
            # Metadata key: value
            assignee_m = re.match(r'^-\s*Assignee:\s*`(.+?)`', line_stripped)
            if assignee_m:
                item["assignee"] = assignee_m.group(1)

            completed_m = re.match(r'^-\s*Completed:\s*`(.+?)`', line_stripped)
            if completed_m:
                item["completed"] = completed_m.group(1)

            # Completion summary
            summary_m = re.match(r'^-\s*Completion summary excerpt:\s*(.+?)$', line_stripped)
            if summary_m:
                item["completion_summary"] = summary_m.group(1).strip()

        # Extract Extracted actions (after "- Extracted actions:" section)
        in_actions = False
        in_routed = False
        for line in section.split("\n"):
            stripped = line.strip()

            if stripped.startswith("- Extracted actions:"):
                in_actions = True
                in_routed = False
                continue
            elif stripped.startswith("- Routed card IDs:"):
                in_actions = False
                in_routed = True
                continue
            elif stripped.startswith("- Completion summary") or stripped.startswith("- Kanban link"):
                in_actions = False
                in_routed = False
                continue

            if in_actions and stripped.startswith("- "):
                action = stripped[2:].strip()
                # Remove backtick wrapping if present on whole lines
                action = action.strip("`").strip()
                if action:
                    item["extracted_actions"].append(action)
            elif in_actions and stripped.startswith("  - ") or (in_actions and stripped.startswith("-") and not stripped.startswith("--")):
                action = stripped.lstrip("- ").strip()
                if action:
                    item["extracted_actions"].append(action)

            if in_routed and stripped.startswith("- `"):
                routed_m = re.match(r'^-\s*`(t_\S+)`', stripped)
                if routed_m:
                    item["routed_card_ids"].append(routed_m.group(1))

        items.append(item)

    return items


def normalize_action_text(text: str) -> str:
    """Normalize action text for consistent comparison and pattern matching."""
    text = text.strip()
    # Remove leading numbering like "3. **Implement pipeline:**"
    text = re.sub(r'^\d+\.\s*\*{0,2}', '', text).strip()
    text = re.sub(r'^[-*+]\s+', '', text).strip()
    text = text.strip("`*_ ")
    return text


def is_low_signal_bullet_action(action_text: str) -> bool:
    """True for markdown/table/code/fragments that should not become cards.

    Automated decomposers must not turn each copied bullet or specification line
    into its own RESEARCH-ACTIONABLE child. A standalone child requires an
    imperative verb plus a real object; otherwise the item should be suppressed
    or grouped into one digest for the source task.
    """
    normalized = normalize_action_text(action_text)
    if not normalized:
        return True
    if MARKDOWN_FRAGMENT_RE.match(normalized) or LOW_SIGNAL_PREFIX_RE.match(normalized):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", normalized.lower())
    if len(words) < 3:
        return True
    return words[0] not in ACTIONABLE_VERBS


def is_false_positive_action(action_text: str) -> bool:
    """Check if an extracted action matches known false-positive patterns."""
    normalized = normalize_action_text(action_text)
    if is_low_signal_bullet_action(normalized):
        return True
    for pattern in FALSE_POSITIVE_PATTERNS:
        if pattern.search(normalized):
            return True
    return False


def has_false_positive_actions(extracted_actions: list[str]) -> tuple[bool, list[str]]:
    """Check if extracted actions are all false positives.

    Returns (True, real_actions) if all actions are template-text → whole item is false_positive.
    Returns (False, real_actions) if some are real → partial FP, return real ones.
    Returns (True, []) if no actions at all.
    """
    if not extracted_actions:
        return True, []

    fp_actions = []
    real_actions = []
    for action in extracted_actions:
        if is_false_positive_action(action):
            fp_actions.append(action)
        else:
            real_actions.append(action)

    # If ALL extracted actions are false-positive → whole item is false_positive
    if not real_actions:
        return True, real_actions  # second element is empty: no real actions

    # Some real actions found alongside FP ones → partial false positive
    return False, real_actions


def has_pm_disposition_verdict(item: dict) -> bool:
    """Check if item has a PM disposition verdict indicating it's already handled."""
    section = item.get("raw_section", "")
    # Look for PM disposition with ACCEPTED_ALREADY_IMPLEMENTED or similar
    if re.search(r'\*\*PM disposition\b.*?\*\*', section):
        return True
    return False


def get_pm_verdict(item: dict) -> str | None:
    """Extract PM disposition verdict if present."""
    section = item.get("raw_section", "")
    pm_match = re.search(r'\*\*PM disposition \([^)]+\)\*\*:\s*(\S+)', section)
    if pm_match:
        verdict = pm_match.group(1).strip().rstrip('.')
        return verdict
    return None


def decision_note_exists(task_id: str) -> bool:
    """Check if an Obsidian decision note exists for this task."""
    if not DECISIONS_DIR.exists():
        return False
    pattern = f"*-decision-{task_id}*.md"
    # Also check patterns: *t_{task_id}*
    for f in DECISIONS_DIR.iterdir():
        if f.is_file() and f.suffix == ".md":
            if task_id in f.stem and "decision" in f.stem:
                return True
            # Also check YAML frontmatter for task reference (more expensive)
            if task_id in f.stem:
                return True
    return False


def find_decision_note_file(task_id: str) -> Path | None:
    """Find the decision note file path for a task."""
    if not DECISIONS_DIR.exists():
        return None
    for f in DECISIONS_DIR.iterdir():
        if f.is_file() and f.suffix == ".md" and task_id in f.stem:
            with open(f) as fh:
                content = fh.read(500)
                if task_id in content:
                    return f
    return None


# ── Circuit Breaker ────────────────────────────────────────────────────

def load_circuit_breaker() -> dict:
    """Load circuit breaker state from JSON file."""
    if CIRCUIT_BREAKER_PATH.exists():
        try:
            data = json.loads(CIRCUIT_BREAKER_PATH.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            print("WARNING: Circuit breaker file corrupt, reinitializing", file=sys.stderr)
    return {}


def save_circuit_breaker(state: dict) -> None:
    """Save circuit breaker state to JSON file."""
    CIRCUIT_BREAKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CIRCUIT_BREAKER_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    print(f"  Circuit breaker saved: {CIRCUIT_BREAKER_PATH}")


def get_cb_entry(state: dict, task_id: str, board: str) -> dict:
    """Get or create a circuit breaker entry for a source task."""
    key = f"{board}/{task_id}"
    if key not in state:
        state[key] = {
            "source_task_id": task_id,
            "board": board,
            "extraction_count": 0,
            "false_positive_count": 0,
            "routed_child_ids": [],
            "last_classification": None,
            "last_seen": None,
            "suppressed": False,
            "suppressed_reason": None,
        }
    return state[key]


def record_classification(
    state: dict,
    task_id: str,
    board: str,
    classification: str,
    routed_ids: list[str],
) -> None:
    """Record a classification result in the circuit breaker."""
    entry = get_cb_entry(state, task_id, board)
    entry["extraction_count"] = entry.get("extraction_count", 0) + 1
    entry["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["last_classification"] = classification

    if classification == "false_positive":
        entry["false_positive_count"] = entry.get("false_positive_count", 0) + 1
    else:
        # Non-FP resets the consecutive counter
        entry["false_positive_count"] = 0

    # Merge routed child IDs (new ones only)
    existing_routed = set(entry.get("routed_child_ids", []))
    for rid in routed_ids:
        existing_routed.add(rid)
    entry["routed_child_ids"] = sorted(existing_routed)

    # Suppression logic: ≥3 consecutive false-positives
    if entry.get("false_positive_count", 0) >= 3:
        if not entry.get("suppressed"):
            entry["suppressed"] = True
            entry["suppressed_reason"] = f"≥3 consecutive false-positives (count={entry['false_positive_count']})"
    else:
        # Clear suppression if a new real_gap or routed child was found
        if classification != "false_positive" and entry.get("suppressed"):
            # Only clear if the current classification is NOT false_positive
            entry["suppressed"] = False
            entry["suppressed_reason"] = None

    # Override: if new child was manually created, clear suppression
    if routed_ids and entry.get("suppressed"):
        new_ids = [rid for rid in routed_ids if rid not in existing_routed]
        if new_ids:
            entry["suppressed"] = False
            entry["suppressed_reason"] = f"Override: manual child creation ({','.join(new_ids)})"


def is_suppressed(state: dict, task_id: str, board: str) -> tuple[bool, str | None]:
    """Check if a source task is suppressed in the circuit breaker."""
    key = f"{board}/{task_id}"
    if key in state:
        entry = state[key]
        if entry.get("suppressed"):
            return True, entry.get("suppressed_reason")
    return False, None


# ── Classification ─────────────────────────────────────────────────────

def classify_item(item: dict, cb_state: dict) -> dict:
    """Classify a single extracted item."""
    task_id = item["task_id"]
    board = item["board"]
    extracted_actions = item["extracted_actions"]
    routed_ids = item["routed_card_ids"]
    section = item.get("raw_section", "")

    result = {
        "task_id": task_id,
        "board": board,
        "title": item["title"],
        "assignee": item["assignee"],
        "classification": None,
        "confidence": 1.0,
        "reason": "",
        "real_actions": [],
        "fp_actions": [],
        "suppressed": False,
        "suppressed_reason": None,
    }

    # 1. Check circuit breaker suppression
    suppressed, sup_reason = is_suppressed(cb_state, task_id, board)
    if suppressed:
        result["suppressed"] = True
        result["suppressed_reason"] = sup_reason
        result["classification"] = "suppressed"
        result["reason"] = f"Circuit breaker active: {sup_reason}"
        return result

    # 2. Check if already have routed children
    if routed_ids:
        result["classification"] = "already_routed"
        result["reason"] = f"Implementation children already exist: {', '.join(routed_ids)}"
        result["confidence"] = 0.95
        result["routed_ids"] = routed_ids
        return result

    # 3. Check for PM disposition verdict (already processed by PM)
    if has_pm_disposition_verdict(item):
        pm_verdict = get_pm_verdict(item)
        result["classification"] = "already_done"
        result["reason"] = f"PM disposition verdict present: {pm_verdict or 'disposition found'}"
        result["confidence"] = 0.9
        return result

    # 4. Check if decision note exists in Obsidian
    note_path = find_decision_note_file(task_id)
    if note_path:
        result["classification"] = "already_done"
        result["reason"] = f"Decision note exists: {note_path.name}"
        result["confidence"] = 0.9
        return result

    # 5. Check for false-positive (template-text leakage)
    is_fp, real_actions = has_false_positive_actions(extracted_actions)
    if is_fp and not real_actions:
        result["classification"] = "false_positive"
        result["reason"] = "All extracted actions match false-positive (template-text) patterns"
        result["confidence"] = 0.95
        result["fp_actions"] = extracted_actions
        return result

    # 6. Partial false-positive: some real, some template
    if extracted_actions and not real_actions:
        # This shouldn't happen after the check above, but safety
        result["classification"] = "false_positive"
        result["reason"] = "No real actions after template filtering"
        result["confidence"] = 0.8
        return result

    # 7. If we have real actions after filtering template text
    if real_actions:
        result["classification"] = "real_gap"
        result["reason"] = (
            f"{len(real_actions)} independently actionable item(s) found; "
            "group into one digest child for the source task"
        )
        result["confidence"] = min(0.95, 0.6 + 0.1 * len(real_actions))
        result["real_actions"] = real_actions
        return result

    # 8. No extracted actions at all → false_positive (nothing actionable)
    result["classification"] = "false_positive"
    result["reason"] = "No extracted actions found in the source item"
    result["confidence"] = 0.7
    return result


# ── Output ─────────────────────────────────────────────────────────────

def generate_report(items: list[dict], results: list[dict], cb_state: dict) -> str:
    """Generate the gap-analysis.md report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = []
    lines.append("---")
    lines.append(f"title: Research-to-Implementation Gap Analysis")
    lines.append(f"generated: {now}")
    lines.append(f"source: research-actionables.md (Research/Reviews/)")
    lines.append(f"total_items: {len(results)}")
    lines.append(f"circuit_breaker_entries: {len(cb_state)}")
    lines.append("---")
    lines.append("")
    lines.append("# Research-to-Implementation Gap Analysis")
    lines.append("")
    lines.append(f"**Generated**: {now}")
    lines.append(f"**Source**: `Research/Reviews/research-actionables.md`")
    lines.append(f"**Total items reviewed**: {len(results)}")
    lines.append("")

    # Summary counts
    classifications = {}
    for r in results:
        c = r["classification"]
        classifications[c] = classifications.get(c, 0) + 1

    lines.append("## Summary")
    lines.append("")
    lines.append("| Classification | Count |")
    lines.append("|---|---|")
    for c in ["real_gap", "already_routed", "already_done", "false_positive", "suppressed"]:
        count = classifications.get(c, 0)
        label_map = {
            "real_gap": "Real Gap 🆕",
            "already_routed": "Already Routed ✅",
            "already_done": "Already Done ✅",
            "false_positive": "False Positive ❌",
            "suppressed": "Suppressed 🔇",
        }
        label = label_map.get(c, c)
        lines.append(f"| {label} | {count} |")
    lines.append("")

    # Details per item
    lines.append("## Item Details")
    lines.append("")

    for i, (item, result) in enumerate(zip(items, results)):
        cls = result["classification"]
        cls_emoji = {
            "real_gap": "🆕",
            "already_routed": "✅",
            "already_done": "✅",
            "false_positive": "❌",
            "suppressed": "🔇",
        }.get(cls, "❓")

        skipped_marker = ""
        if result.get("suppressed"):
            skipped_marker = " 🔇 suppressed (circuit breaker)"

        lines.append(f"### {i+1}. {cls_emoji} {item['board']}/{item['task_id']} — {item['title']}{skipped_marker}")
        lines.append("")
        lines.append(f"| Field | Value |")
        lines.append("|---|---|")
        lines.append(f"| Board | `{item['board']}` |")
        lines.append(f"| Task | `{item['task_id']}` |")
        lines.append(f"| Assignee | {item['assignee']} |")
        lines.append(f"| Classification | **{cls}** |")
        lines.append(f"| Confidence | {result.get('confidence', 0):.0%} |")
        lines.append(f"| Reason | {result.get('reason', '')} |")

        if result.get("routed_ids"):
            lines.append(f"| Routed children | {', '.join(result['routed_ids'])} |")

        if result.get("real_actions"):
            lines.append("")
            lines.append("**Real actions identified:**")
            for action in result["real_actions"]:
                action_short = action[:120] + ("..." if len(action) > 120 else "")
                lines.append(f"- `{action_short}`")

        if result.get("fp_actions"):
            lines.append("")
            lines.append("**Filtered template-text actions:**")
            for action in result["fp_actions"]:
                action_short = action[:80] + ("..." if len(action) > 80 else "")
                lines.append(f"- ~~{action_short}~~")

        lines.append("")

    # Suppressed tasks section
    suppressed_entries = {k: v for k, v in cb_state.items() if v.get("suppressed")}
    if suppressed_entries:
        lines.append("## 🔇 Suppressed Source Tasks (Circuit Breaker)")
        lines.append("")
        lines.append("| Source | FP Count | Reason |")
        lines.append("|---|---|---|")
        for key, entry in sorted(suppressed_entries.items()):
            lines.append(f"| `{key}` | {entry.get('false_positive_count', 0)} | {entry.get('suppressed_reason', 'N/A')} |")
        lines.append("")

    # Circuit breaker state
    lines.append("## Circuit Breaker State")
    lines.append("")
    lines.append(f"Total tracked sources: {len(cb_state)}")
    lines.append(f"Suppressed: {len(suppressed_entries)}")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(cb_state, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("Research-to-Implementation Gap Analyzer")
    print("=" * 60)
    print()

    # 1. Parse input
    print("1. Reading research-actionables.md...")
    items = parse_actionables(ACTIONABLES_PATH)
    if not items:
        print("  No items found. Exiting.")
        return 0
    print(f"  Found {len(items)} items")

    # 2. Load circuit breaker
    print("2. Loading circuit breaker state...")
    cb_state = load_circuit_breaker()
    print(f"  {len(cb_state)} tracked sources")

    # 3. Classify each item
    print("3. Classifying items...")
    results = []
    for item in items:
        tid = item["task_id"]
        board = item["board"]
        actions = item["extracted_actions"]

        result = classify_item(item, cb_state)
        results.append(result)

        # Record in circuit breaker
        routed = item.get("routed_card_ids", [])
        record_classification(cb_state, tid, board, result["classification"], routed)

        print(f"  [{board:20s}] {tid:14s} → {result['classification']:20s} | {result.get('reason', '')[:60]}")

    # 4. Generate gap-analysis.md
    print()
    print("4. Generating gap-analysis.md...")
    report = generate_report(items, results, cb_state)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report)
    print(f"  Output: {OUTPUT_PATH} ({len(report)} chars)")

    # 5. Save circuit breaker
    print("5. Saving circuit breaker state...")
    save_circuit_breaker(cb_state)

    print()
    print("=" * 60)
    print("Gap analysis complete.")
    print(f"  Real gaps:    {sum(1 for r in results if r['classification'] == 'real_gap')}")
    print(f"  Already routed: {sum(1 for r in results if r['classification'] == 'already_routed')}")
    print(f"  Already done: {sum(1 for r in results if r['classification'] == 'already_done')}")
    print(f"  False positives: {sum(1 for r in results if r['classification'] == 'false_positive')}")
    print(f"  Suppressed:   {sum(1 for r in results if r['classification'] == 'suppressed')}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
