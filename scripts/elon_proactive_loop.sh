#!/bin/bash
set -euo pipefail

# This script is the core of Elon's proactive governor loop.
# It is designed to be run every 5 minutes by cron.


# 0. Research-to-Implementation: if gap analysis exists, run child creator first.
GOVERNOR_INTEGRATION="/home/frank/.hermes/scripts/research-impl-governor-integration.sh"
if [ -f "$GOVERNOR_INTEGRATION" ]; then
    bash "$GOVERNOR_INTEGRATION" || true
fi
# 1. Re-orient: Load SOUL.md and the last 24 hours of REFLECTION.md.
SOUL_PROMPT=$(cat ~/.hermes/profiles/elon/SOUL.md)
REFLECTION_CONTEXT=$(tail -n 100 ~/.hermes/profiles/elon/REFLECTION.md)

# 2. Scan all four kanban boards.
Upero_STATUS=$(hermes kanban list --board upero --limit 5 --json)
JarvisOS_STATUS=$(hermes kanban list --board jarvis-os --limit 5 --json)
SycodeAI_STATUS=$(hermes kanban list --board sycode-ai --limit 5 --json)
SycodeTrading_STATUS=$(hermes kanban list --board sycode-trading --limit 5 --json)

# 3. Formulate a high-leverage action or self-improvement goal.
PROMPT="""
$SOUL_PROMPT

My reflection context from the last 24 hours:
$REFLECTION_CONTEXT

Current Fleet Status:
Upero:
$Upero_STATUS

Jarvis-OS:
$JarvisOS_STATUS

Sycode-AI:
$SycodeAI_STATUS

Sycode-Trading:
$SycodeTrading_STATUS

Based on all of this information, and my core principles, what is the single highest-leverage action I can take *right now* to accelerate the fleet? 
- If there is a clear, urgent, or high-impact action to take on the boards, respond with a single Hermes CLI command to create, comment on, or block a kanban task.
- If the boards are stable and there are no urgent actions, respond with the phrase "PROCEED TO SELF-IMPROVEMENT".
"""

# 4. Decide on the course of action.
ACTION_OR_IMPROVEMENT=$(hermes -p elon --non-interactive --prompt "$PROMPT")

# 5. Execute the action or self-improvement.
if [[ "$ACTION_OR_IMPROVEMENT" == "PROCEED TO SELF-IMPROVEMENT" ]]; then
    # Idle path: force a concrete self-improvement.
    IMPROVEMENT_PROMPT="""
    Using the 'governor-self-improvement' and 'prompt-loop-master' skills, analyze my SOUL.md and recent REFLECTION.md file.
    Based on this analysis, generate and immediately apply one concrete improvement to my operating system.
    The improvement must be one of the following:
    1. A patch to my SOUL.md file.
    2. A new or improved skill.
    3. A new or improved cron job.

    Your final output must be the Hermes CLI command to achieve this.
    """
    ACTION_COMMAND=$(hermes -p elon --non-interactive --prompt "$IMPROVEMENT_PROMPT" --skill governor-self-improvement --skill prompt-loop-master)
    LOG_MESSAGE="SELF-IMPROVEMENT: Applied a concrete improvement to my operating system."
else
    # Active path: execute the fleet action.
    ACTION_COMMAND="$ACTION_OR_IMPROVEMENT"
    LOG_MESSAGE="FLEET_ACTION: Executed a high-leverage command."
fi

eval "$ACTION_COMMAND"

# 6. Log the action and rationale.
echo "## Cycle: $(date -u --iso-8601=seconds)" >> ~/.hermes/profiles/elon/REFLECTION.md
echo "**Action Taken: $LOG_MESSAGE**" >> ~/.hermes/profiles/elon/REFLECTION.md
echo "\`\`\`" >> ~/.hermes/profiles/elon/REFLECTION.md
echo "$ACTION_COMMAND" >> ~/.hermes/profiles/elon/REFLECTION.md
echo "\`\`\`" >> ~/.hermes/profiles/elon/REFLECTION.md
echo "" >> ~/.hermes/profiles/elon/REFLECTION.md
