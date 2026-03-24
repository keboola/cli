"""Interactive REPL mode for kbagent.

Provides a prompt-toolkit based interactive shell where users and agents
can run kbagent commands without restarting the process. Supports tab
completion, persistent history, and colored output.
"""

import shlex
import sys
from pathlib import Path

import click
import platformdirs
import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory

from .. import __version__
from ._helpers import get_formatter


def _build_command_tree(click_group: click.Group, prefix: str = "") -> dict:
    """Walk Typer/Click command tree and return a dict of command paths to help strings."""
    tree: dict[str, str] = {}
    try:
        with click.Context(click_group) as ctx:
            for name in click_group.list_commands(ctx):
                cmd = click_group.get_command(ctx, name)
                if cmd is None:
                    continue
                full_name = f"{prefix}{name}"
                help_text = cmd.get_short_help_str(limit=80)
                tree[full_name] = help_text
                if isinstance(cmd, click.Group):
                    tree.update(_build_command_tree(cmd, f"{full_name} "))
    except Exception:
        pass
    return tree


class KbagentCompleter(Completer):
    """Tab-completion for kbagent commands inside the REPL."""

    def __init__(self, command_tree: dict[str, str]) -> None:
        self._commands = command_tree

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.strip()
        for cmd, help_text in sorted(self._commands.items()):
            if cmd.startswith(text):
                # Yield the remaining part after what's already typed
                suffix = cmd[len(text) :]
                yield Completion(
                    suffix,
                    start_position=0,
                    display=cmd,
                    display_meta=help_text,
                )


def _get_history_path() -> Path:
    """Return path for persistent REPL history file."""
    config_dir = Path(platformdirs.user_config_dir("keboola-agent-cli"))
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "repl_history"


def _run_repl(json_mode: bool, verbose: bool, no_color: bool, config_dir: str | None) -> None:
    """Main REPL loop."""
    from ..cli import app as typer_app

    # Build command tree for completion
    click_app = typer.main.get_command(typer_app)
    command_tree = _build_command_tree(click_app)

    # Add REPL-specific commands
    command_tree["help"] = "Show available commands"
    command_tree["exit"] = "Exit the REPL"
    command_tree["quit"] = "Exit the REPL"

    completer = KbagentCompleter(command_tree)
    history = FileHistory(str(_get_history_path()))

    session: PromptSession = PromptSession(
        history=history,
        completer=completer,
        complete_while_typing=False,
    )

    prompt_text = HTML("<ansiblue><b>kbagent</b></ansiblue><ansigray> &gt; </ansigray>")

    # Show banner
    sys.stderr.write(f"\nkbagent v{__version__} -- interactive mode\n")
    sys.stderr.write("Type 'help' for commands, 'exit' to quit.\n")
    sys.stderr.write(f"Global flags: --json={json_mode}, --verbose={verbose}\n\n")

    while True:
        try:
            line = session.prompt(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\n")
            break

        if not line:
            continue

        if line in ("exit", "quit", "q"):
            break

        if line == "help":
            # Print command tree
            sys.stderr.write("\nAvailable commands:\n")
            # Show only top-level and second-level commands
            shown = set()
            for cmd, help_text in sorted(command_tree.items()):
                parts = cmd.split()
                if len(parts) <= 2 and cmd not in ("help", "exit", "quit"):
                    sys.stderr.write(f"  {cmd:<35} {help_text}\n")
                    shown.add(cmd)
            sys.stderr.write(f"\n  {'help':<35} Show this help\n")
            sys.stderr.write(f"  {'exit':<35} Exit the REPL\n")
            sys.stderr.write("\n")
            continue

        # Parse the input line into argv
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            sys.stderr.write(f"Parse error: {exc}\n")
            continue

        # Build full argv with global flags
        full_argv = []
        if json_mode and "--json" not in argv and "-j" not in argv:
            full_argv.append("--json")
        if verbose and "--verbose" not in argv and "-v" not in argv:
            full_argv.append("--verbose")
        if no_color and "--no-color" not in argv:
            full_argv.append("--no-color")
        if config_dir and "--config-dir" not in argv:
            full_argv.extend(["--config-dir", config_dir])
        full_argv.extend(argv)

        # Prevent recursive REPL
        if full_argv and full_argv[-1] == "repl":
            sys.stderr.write("Already in REPL mode.\n")
            continue

        # Execute the command via Typer
        try:
            click_app = typer.main.get_command(typer_app)
            click_app(full_argv, standalone_mode=False)
        except SystemExit:
            pass  # Typer/Click raises SystemExit on --help or errors
        except Exception as exc:
            sys.stderr.write(f"Error: {exc}\n")


def repl_command(ctx: typer.Context) -> None:
    """Start interactive REPL mode for kbagent."""
    formatter = get_formatter(ctx)

    if formatter.json_mode:
        # In JSON mode, REPL doesn't make sense -- output instructions
        formatter.output(
            {
                "message": "REPL mode is for interactive use. Run 'kbagent repl' without --json.",
            }
        )
        return

    _run_repl(
        json_mode=ctx.obj.get("json_output", False),
        verbose=ctx.obj.get("verbose", False),
        no_color=ctx.obj.get("no_color", False),
        config_dir=None,  # Already resolved in ctx
    )
