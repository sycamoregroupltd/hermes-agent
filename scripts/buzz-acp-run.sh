#!/usr/bin/env bash
# buzz-acp-run.sh <grok|hermes|claude|codex>
# Native Buzz peer: harness listens on the relay and the ACP agent replies
# on the same channel. No kanban hop.
# Never echo the private key. Never create profiles/grok or profiles/fable.
set -euo pipefail
SEAT="${1:?usage: buzz-acp-run.sh grok|hermes|claude|codex}"
IDENT=/home/frank/buzz-bridge-pilot/state/identities
ACP="${BUZZ_ACP_BIN:-/home/frank/buzz-lab/target/ci/buzz-acp}"
export PATH="/home/frank/.local/bin:/home/frank/.npm-global/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/local/bin:/usr/bin:/bin"

# Admitted private-channel peers (hex pubs). Owner (if known) is extra via --agent-owner.
ALLOW="b4da325bd611f1e80ebe10481a4f9d8b7b39a0d6ba71a0ea21a245ace9c8069d,8cb7ee76dd3c84aee1908452b99b2d0e2b891ec459eae2b2a4655b1bbafd7325,908802a4cd4944211c4be873648c2e7851df2d36b693133850073dacf67dccc1,1dda2df938f98f541885683df97b1a9c1139bc194da308b7c4866c703695375f,79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"

# Optional human owner pubkey (64-hex). Do not invent one.
OWNER_FILE="$IDENT/owner.pub"
if [[ -f "$OWNER_FILE" ]]; then
  export BUZZ_ACP_AGENT_OWNER
  BUZZ_ACP_AGENT_OWNER="$(tr -d '[:space:]' < "$OWNER_FILE")"
fi

case "$SEAT" in
  grok)
    KEY_FILE="$IDENT/grok.key"
    export BUZZ_ACP_AGENT_COMMAND="${GROK_BIN:-$(command -v grok)}"
    export BUZZ_ACP_AGENT_ARGS='agent,--always-approve,--no-leader,--model,grok-4.6,stdio'
    # Max-plan OIDC via ~/.grok/auth.json (auth.x.ai). Never fall through to XAI_API_KEY / api.x.ai.
    unset XAI_API_KEY
    ;;
  hermes)
    KEY_FILE="$IDENT/hermes.key"
    export BUZZ_ACP_AGENT_COMMAND="${HERMES_BIN:-$(command -v hermes)}"
    export BUZZ_ACP_AGENT_ARGS='acp,--accept-hooks'
    # Slim home: no n8n/git/linear MCP, no Telegram/WhatsApp. Auth/env symlinked from jarvis.
    export HERMES_HOME=/home/frank/.hermes/seats/hermes-acp
    unset HERMES_PROFILE
    unset XAI_API_KEY
    ;;
  claude)
    KEY_FILE="$IDENT/claude.key"
    # Claude Max / Claude Code credentials (~/.claude). Do not set ANTHROPIC_API_KEY.
    unset ANTHROPIC_API_KEY
    CLAUDE_ACP="$(command -v claude-agent-acp || true)"
    [[ -n "$CLAUDE_ACP" ]] || { echo "claude-agent-acp missing — npm i -g @agentclientprotocol/claude-agent-acp" >&2; exit 2; }
    export BUZZ_ACP_AGENT_COMMAND="$CLAUDE_ACP"
    export BUZZ_ACP_AGENT_ARGS=''
    ;;
  codex)
    KEY_FILE="$IDENT/codex.key"
    # Codex subscription (~/.codex/auth.json). Do not set OPENAI_API_KEY.
    unset OPENAI_API_KEY
    CODEX_ACP="$(command -v codex-acp || true)"
    [[ -n "$CODEX_ACP" ]] || { echo "codex-acp missing — npm i -g @agentclientprotocol/codex-acp" >&2; exit 2; }
    export BUZZ_ACP_AGENT_COMMAND="$CODEX_ACP"
    export BUZZ_ACP_AGENT_ARGS=''
    ;;
  *)
    echo "unknown seat: $SEAT" >&2
    exit 2
    ;;
esac

[[ -x "$ACP" ]] || { echo "buzz-acp missing at $ACP — build: cargo build --profile ci -p buzz-acp" >&2; exit 2; }
[[ -f "$KEY_FILE" ]] || { echo "identity missing: $KEY_FILE" >&2; exit 2; }

export BUZZ_PRIVATE_KEY
BUZZ_PRIVATE_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"
export BUZZ_RELAY_URL="ws://localhost:3030"
export BUZZ_ACP_RESPOND_TO=allowlist
export BUZZ_ACP_RESPOND_TO_ALLOWLIST="$ALLOW"

OWNER_ARGS=()
if [[ -n "${BUZZ_ACP_AGENT_OWNER:-}" ]]; then
  OWNER_ARGS+=(--agent-owner "$BUZZ_ACP_AGENT_OWNER")
fi

exec "$ACP" --respond-to allowlist --respond-to-allowlist "$ALLOW" "${OWNER_ARGS[@]}"
