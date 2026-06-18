#!/usr/bin/env python3
"""CI guard: verify every CLI command is registered and documented.

The kbagent CLI surface is mirrored across several agent-facing files that have
NO other CI freshness check (see CONTRIBUTING.md "Plugin synchronization map"
and CLAUDE.md convention #17). Forgetting to update one ships an AI agent that
recommends commands the installed kbagent version does not have, or a
permission engine that silently mis-categorises a new command. This script
makes the live command tree the single source of truth and fails if any
surface has drifted from it.

Checks (all blocking):
  1. permissions.py OPERATION_REGISTRY has an entry for every leaf command.
  2. OPERATION_REGISTRY has NO dead entries (keys matching no command path).
  3. CLAUDE.md "## All CLI Commands" mentions every leaf command.
  4. commands/context.py AGENT_CONTEXT mentions every command group/subcommand.
  5. commands-reference.md mentions every command group/subcommand.

Granularity (calibrated against the live tree for zero false positives):
  - Registry + CLAUDE.md are exhaustive surfaces, matched at FULL-leaf
    granularity (e.g. "semantic-layer add constraint").
  - AGENT_CONTEXT + commands-reference.md document deep (3-level) groups
    compactly (e.g. "semantic-layer add metric|dataset|..."), so they are
    matched at 2-SEGMENT granularity (e.g. "semantic-layer add").

Hidden commands/aliases (e.g. the `sl` alias for `semantic-layer`) are skipped
along with their subtree: they are not part of the public documented surface.

NOT checked here (both need judgement, so they are left to /kbagent:review,
not gated deterministically):
  - gotchas.md `(since vX.Y.Z)` tags. Whether a heading is a new
    version-specific gotcha (needs the tag) or a version-independent
    structural section (must not have it) requires judgement.
  - server/routers/<group>.py REST mirror. CONTRIBUTING.md requires a 1:1
    route for every non-terminal command, but "is this command terminal-only?"
    (interactive prompt / Rich-only output / kbagent-infra) is a judgement
    call, so it is not gated here.

Usage (run from repo root):
    python scripts/check_command_sync.py          # exit 1 if any drift found
    python scripts/check_command_sync.py --list    # print the command tree
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
import typer.main

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands.context import AGENT_CONTEXT
from keboola_agent_cli.commands.repl import _is_group
from keboola_agent_cli.permissions import OPERATION_REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
COMMANDS_REFERENCE_MD = (
    REPO_ROOT
    / "plugins"
    / "kbagent"
    / "skills"
    / "kbagent"
    / "references"
    / "commands-reference.md"
)

CommandPath = tuple[str, ...]


def _walk(
    group: click.Group, ctx: click.Context, prefix: CommandPath = ()
) -> tuple[list[CommandPath], list[CommandPath]]:
    """Walk the Click command tree; return (leaf_paths, group_paths).

    Hidden commands and groups (e.g. the `sl` alias) are skipped together with
    their entire subtree -- they are not part of the documented surface.
    """
    leaves: list[CommandPath] = []
    groups: list[CommandPath] = []
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or getattr(cmd, "hidden", False):
            continue
        path = (*prefix, name)
        if _is_group(cmd):
            groups.append(path)
            with click.Context(cmd, parent=ctx) as sub_ctx:
                sub_leaves, sub_groups = _walk(cmd, sub_ctx, path)
                leaves.extend(sub_leaves)
                groups.extend(sub_groups)
        else:
            leaves.append(path)
    return leaves, groups


def collect_commands() -> tuple[list[CommandPath], list[CommandPath]]:
    """Return (leaf_paths, group_paths) for the live CLI command tree."""
    click_app = typer.main.get_command(app)
    assert _is_group(click_app)
    with click.Context(click_app) as ctx:
        return _walk(click_app, ctx)


def find_drift(
    leaves: list[CommandPath],
    groups: list[CommandPath],
    *,
    registry_keys: set[str],
    claude_text: str,
    context_text: str,
    reference_text: str,
) -> list[str]:
    """Return a human-readable block per drifted surface (empty list == clean)."""
    leaf_keys = {".".join(p) for p in leaves}
    all_keys = leaf_keys | {".".join(p) for p in groups}
    two_segment = {" ".join(p[:2]) for p in leaves}

    problems: list[str] = []

    missing_registry = sorted(leaf_keys - registry_keys)
    if missing_registry:
        entries = "\n".join(
            f'    "{k}": "<read|write|destructive|admin>",' for k in missing_registry
        )
        problems.append(
            "Commands missing from permissions.py OPERATION_REGISTRY (the "
            "fail-closed default 'write' hides their true risk category):\n" + entries
        )

    dead_registry = sorted(registry_keys - all_keys)
    if dead_registry:
        keys = "\n".join(f"    {k}" for k in dead_registry)
        problems.append(
            "OPERATION_REGISTRY keys matching no live command (a renamed or "
            "removed command -- delete the key or fix the rename):\n" + keys
        )

    missing_claude = sorted(
        p.replace(".", " ") for p in leaf_keys if p.replace(".", " ") not in claude_text
    )
    if missing_claude:
        cmds = "\n".join(f"    kbagent {c} ..." for c in missing_claude)
        problems.append("Commands not documented in CLAUDE.md '## All CLI Commands':\n" + cmds)

    missing_context = sorted(s for s in two_segment if s not in context_text)
    if missing_context:
        cmds = "\n".join(f"    {s}" for s in missing_context)
        problems.append(
            "Command groups not documented in commands/context.py AGENT_CONTEXT:\n" + cmds
        )

    missing_reference = sorted(s for s in two_segment if s not in reference_text)
    if missing_reference:
        cmds = "\n".join(f"    {s}" for s in missing_reference)
        problems.append("Command groups not documented in commands-reference.md:\n" + cmds)

    return problems


def main() -> int:
    leaves, groups = collect_commands()

    if "--list" in sys.argv:
        for path in sorted(leaves):
            print(" ".join(path))
        print(f"\n{len(leaves)} leaf commands, {len(groups)} groups", file=sys.stderr)
        return 0

    problems = find_drift(
        leaves,
        groups,
        registry_keys=set(OPERATION_REGISTRY),
        claude_text=CLAUDE_MD.read_text(encoding="utf-8"),
        context_text=AGENT_CONTEXT,
        reference_text=COMMANDS_REFERENCE_MD.read_text(encoding="utf-8"),
    )

    if problems:
        print("FAIL: the CLI command surface has drifted from its mirrors.\n")
        for block in problems:
            print(block)
            print()
        print(
            "Fix every block above, then re-run. See CONTRIBUTING.md "
            "'Plugin synchronization map' and CLAUDE.md convention #17."
        )
        return 1

    print(
        f"OK: all {len(leaves)} CLI commands are registered (OPERATION_REGISTRY) and "
        "documented (CLAUDE.md, context.py, commands-reference.md)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
