"""``hermes agent`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_agent_parser(subparsers, *, cmd_agent: Callable) -> None:
    """Attach the ``agent`` command family to the top-level parser."""
    agent_parser = subparsers.add_parser(
        "agent",
        help="Spawn and manage independent Hermes agent runs",
        description="Spawn and manage independent Hermes agent runs.",
    )
    agent_subparsers = agent_parser.add_subparsers(dest="agent_action")

    spawn = agent_subparsers.add_parser(
        "spawn",
        help="Start an independent Hermes agent run and return a run_id",
        description=(
            "Start an independent Hermes agent run. By default the run starts "
            "in the background and prints its run_id; pass --block to wait for "
            "completion and record stdout/stderr."
        ),
    )
    spawn.add_argument("--profile", "-p", default="default", help="Profile to run (default: default)")
    prompt_group = spawn.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="Prompt to send to the spawned agent")
    prompt_group.add_argument("--goal", help="Goal text to send to the spawned agent")
    spawn.add_argument("--block", action="store_true", help="Wait for completion and print the final output")
    spawn.add_argument("--background", action="store_true", help="Explicit background mode (default)")
    spawn.add_argument("--toolsets", help="Comma-separated toolsets for the spawned oneshot run")
    spawn.add_argument(
        "--context-file",
        action="append",
        help="Path to a context file exposed via HERMES_AGENT_CONTEXT_FILES. Repeatable.",
    )
    spawn.add_argument(
        "--env",
        action="append",
        help="Environment override for the child process, NAME=VALUE. Repeatable.",
    )
    spawn.add_argument("--cwd", help="Working directory for the spawned run")
    spawn.add_argument("--timeout", type=float, help="Timeout in seconds for --block mode")
    spawn.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    spawn.set_defaults(func=cmd_agent)

    agent_parser.set_defaults(func=cmd_agent)
