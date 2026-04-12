"""Hint system — generates equivalent Python code for CLI commands.

Usage:
    from keboola_agent_cli.hints import HintRegistry, render_hint
    from keboola_agent_cli.hints.models import HintMode

    output = render_hint("config.list", HintMode.CLIENT, params, stack_url, ...)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from .models import CommandHint, HintMode


class HintRegistry:
    """Registry of hint definitions for CLI commands.

    Hints are registered at import time by definition modules.
    """

    _hints: ClassVar[dict[str, CommandHint]] = {}

    @classmethod
    def register(cls, hint: CommandHint) -> None:
        """Register a hint definition."""
        cls._hints[hint.cli_command] = hint

    @classmethod
    def get(cls, command: str) -> CommandHint | None:
        """Look up a hint by command key (e.g. 'config.list')."""
        return cls._hints.get(command)

    @classmethod
    def all_commands(cls) -> list[str]:
        """Return all registered command keys, sorted."""
        return sorted(cls._hints.keys())


def render_hint(
    cli_command: str,
    hint_mode: HintMode,
    params: dict[str, Any],
    stack_url: str | None,
    config_dir: Path | None,
    branch_id: int | None,
) -> str:
    """Render Python code for a CLI command.

    Args:
        cli_command: Dot-separated command key, e.g. 'config.list'.
        hint_mode: CLIENT or SERVICE layer.
        params: CLI parameters passed to the command.
        stack_url: Resolved stack URL (or None for placeholder).
        config_dir: Resolved config directory path (for service hints).
        branch_id: Active branch ID (or None for production).

    Returns:
        String of runnable Python code.

    Raises:
        ValueError: If no hint is registered for the given command.
    """
    # Ensure definitions are loaded
    from . import definitions as _definitions  # noqa: F401

    hint = HintRegistry.get(cli_command)
    if hint is None:
        msg = f"No hint available for command '{cli_command}'."
        raise ValueError(msg)

    from .renderer import ClientRenderer, ServiceRenderer

    if hint_mode == HintMode.SERVICE:
        return ServiceRenderer.render(hint, params, stack_url, config_dir, branch_id)
    return ClientRenderer.render(hint, params, stack_url, branch_id, config_dir=config_dir)
