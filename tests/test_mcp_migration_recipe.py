"""The documented migration recipe must name flags the CLI actually has.

#581 shipped a four-step recipe whose last step told the reader to run
`kbagent agent update --type cli_command --argv ...`. That command patches
name/cron/enabled/trigger only -- it explicitly cannot change a task's action --
so the documented path exited 2. A recipe that fails at its last step is worse
than no recipe, and nothing was checking that the docs matched the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer.main

from keboola_agent_cli.cli import app

GOTCHAS = Path(__file__).parent.parent / "plugins/kbagent/skills/kbagent/references/gotchas.md"


def _options_of(*command: str) -> set[str]:
    """Every option name a command declares, read from the command object.

    Deliberately NOT scraped from ``--help``: rendered help is a presentation
    concern -- Rich wraps and truncates it by terminal width and TTY detection,
    so an option can be present in the interface and absent from the text. The
    parameter list is the actual contract. An earlier version of this helper
    scraped the help, passed locally, and failed on all three CI runners.
    """
    node: Any = typer.main.get_command(app)
    for name in command:
        # Duck-typed rather than isinstance(click.Group): Typer builds its own
        # Command/Group subclasses, so a click.Group check is False at runtime.
        subcommands = getattr(node, "commands", None)
        assert isinstance(subcommands, dict), f"{name!r} is not under a command group"
        assert name in subcommands, f"no such command: {' '.join(command)}"
        node = subcommands[name]
    return {opt for param in node.params for opt in param.opts if opt.startswith("--")}


def test_agent_update_still_cannot_change_the_action() -> None:
    """Anchor for the recipe below; if this ever changes, simplify the docs."""
    options = _options_of("agent", "update")
    assert "--type" not in options
    assert "--argv" not in options


def test_recipe_does_not_tell_agent_update_to_change_the_action() -> None:
    text = GOTCHAS.read_text(encoding="utf-8")
    assert "agent update <id> --type cli_command" not in text


def test_recipe_uses_flags_agent_create_really_has() -> None:
    options = _options_of("agent", "create")
    for flag in ("--type", "--argv", "--name", "--cron"):
        assert flag in options, f"the migration recipe names {flag}, which agent create lacks"


def test_recipe_warns_about_the_changing_task_id() -> None:
    """Recreating breaks any task chained to the old id via Trigger.task_id."""
    text = GOTCHAS.read_text(encoding="utf-8")
    section = text.split("Recipe for migrating one task", 1)
    assert len(section) == 2, "migration recipe section is gone"
    body = section[1][:1600]
    assert "--trigger-task-id" in body
    assert "the task id changes" in body.lower()
