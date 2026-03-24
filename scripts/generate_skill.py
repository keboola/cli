#!/usr/bin/env python3
"""Auto-generate the command decision table in the plugin's SKILL.md.

Introspects the Typer/Click command tree from keboola_agent_cli.cli.app
and generates a markdown table mapping goals to commands. The table is
injected between HTML comment markers in SKILL.md, making this script
safe to run repeatedly (idempotent).

Usage:
    python scripts/generate_skill.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click
import typer.main

from keboola_agent_cli.cli import app

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "plugins" / "kbagent" / "skills" / "kbagent" / "SKILL.md"

BEGIN_MARKER = "<!-- BEGIN AUTO-GENERATED COMMANDS -->"
END_MARKER = "<!-- END AUTO-GENERATED COMMANDS -->"

# Commands to skip in the table (utility / meta commands that the AI agent
# does not typically need to choose between in a decision table)
SKIP_COMMANDS = {"context", "version", "init", "doctor", "repl"}


# ---------------------------------------------------------------------------
# Click introspection
# ---------------------------------------------------------------------------


def _first_sentence(text: str) -> str:
    """Extract the first sentence from a help string.

    Strips leading/trailing whitespace, takes the first paragraph line,
    and truncates at the first period if present.
    """
    if not text:
        return ""
    # Take only the first paragraph (up to a blank line)
    first_para = text.strip().split("\n\n")[0]
    # Collapse whitespace / newlines within the paragraph
    first_para = " ".join(first_para.split())
    # Truncate at the first period that is followed by a space or end-of-string
    match = re.match(r"^(.*?\.)\s", first_para)
    if match:
        return match.group(1)
    # If no mid-sentence period, return the whole paragraph
    return first_para.rstrip(".")


def _format_param(param: click.Parameter) -> str:
    """Format a single Click parameter for the command column.

    Required params get a placeholder value (e.g. --alias NAME).
    Optional params are omitted from the compact command string.
    """
    if isinstance(param, click.Argument):
        human_name = param.name.upper().replace("_", "-")
        return f"<{human_name}>" if param.required else f"[{human_name}]"

    if isinstance(param, click.Option):
        if not param.required:
            return ""
        # Use the longest option string (e.g. --alias over -a)
        opt_str = max(param.opts, key=len)
        # Convert underscores to hyphens for display
        opt_str = opt_str.replace("_", "-")
        human_name = param.human_readable_name.upper().replace("_", "-")
        if param.is_flag:
            return opt_str
        return f"{opt_str} {human_name}"

    return ""


def _collect_commands(
    group: click.Group,
    ctx: click.Context,
    prefix: str = "",
) -> list[dict[str, str]]:
    """Recursively walk the Click command tree and collect command metadata."""
    commands: list[dict[str, str]] = []

    for name in group.list_commands(ctx):
        if name in SKIP_COMMANDS and not prefix:
            continue

        cmd = group.get_command(ctx, cmd_name=name)
        if cmd is None:
            continue

        full_name = f"{prefix} {name}".strip() if prefix else name

        if isinstance(cmd, click.Group):
            # If the group has invoke_without_command and its own non-trivial
            # params, it acts as a standalone command too (e.g. `explorer`
            # has --project, --output-dir, etc.). Groups whose callback is
            # just a pass-through to a subcommand (e.g. `lineage` -> `show`)
            # have no meaningful params and should be skipped to avoid
            # duplicate rows.
            if getattr(cmd, "invoke_without_command", False) and cmd.callback:
                own_params = [
                    p
                    for p in cmd.params
                    if p.name not in ("help", "ctx")
                    and not (isinstance(p, click.Option) and p.name == "help")
                ]
                if own_params:
                    help_text = cmd.help or ""
                    param_parts = [_format_param(p) for p in own_params]
                    param_str = " ".join(p for p in param_parts if p)
                    cmd_str = f"kbagent {full_name}"
                    if param_str:
                        cmd_str += f" {param_str}"
                    goal = _first_sentence(help_text)
                    if goal:
                        commands.append({"goal": goal, "command": cmd_str})

            # Recurse into subcommands
            with click.Context(cmd, parent=ctx) as sub_ctx:
                commands.extend(_collect_commands(cmd, sub_ctx, prefix=full_name))
        else:
            help_text = cmd.help or cmd.short_help or ""
            # Build param string from required params
            param_parts = [_format_param(p) for p in cmd.params if p.name not in ("help", "ctx")]
            param_str = " ".join(p for p in param_parts if p)
            cmd_str = f"kbagent {full_name}"
            if param_str:
                cmd_str += f" {param_str}"

            goal = _first_sentence(help_text)
            if goal:
                commands.append({"goal": goal, "command": cmd_str})

    return commands


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------


def generate_table(commands: list[dict[str, str]]) -> str:
    """Generate a markdown table from the collected commands."""
    lines = [
        "| Goal | Command |",
        "|------|---------|",
    ]
    for entry in commands:
        goal = entry["goal"]
        cmd = entry["command"]
        lines.append(f"| {goal} | `{cmd}` |")

    return "\n".join(lines)


def inject_into_skill_md(table: str, skill_path: Path) -> None:
    """Replace content between markers in SKILL.md with the generated table."""
    content = skill_path.read_text(encoding="utf-8")

    if BEGIN_MARKER not in content or END_MARKER not in content:
        print(
            f"ERROR: Markers not found in {skill_path}.\n"
            f"  Expected: {BEGIN_MARKER}\n"
            f"  And:      {END_MARKER}",
            file=sys.stderr,
        )
        sys.exit(1)

    pattern = re.compile(
        re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    replacement = f"{BEGIN_MARKER}\n{table}\n{END_MARKER}"
    new_content = pattern.sub(replacement, content)

    skill_path.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: introspect CLI, generate table, inject into SKILL.md."""
    click_app = typer.main.get_command(app)

    with click.Context(click_app) as ctx:
        commands = _collect_commands(click_app, ctx)

    table = generate_table(commands)

    print(f"Generated table with {len(commands)} commands:")
    print(table)
    print()

    inject_into_skill_md(table, SKILL_MD)
    print(f"Updated {SKILL_MD}")


if __name__ == "__main__":
    main()
