"""``config state-get`` / ``config state-set`` -- runtime state read/write (issue #593).

Split out of ``commands/config.py`` (rather than appended there) purely for
file-size-budget reasons: ``commands/config.py`` is grandfathered at its
CONTRIBUTING.md hard ceiling (``scripts/file_size_baseline.json``) and may
only shrink, not grow (see ``scripts/check_file_size.py``). These two
commands are registered on the SAME ``config_app`` Typer instance imported
from ``.config``, so they behave identically to a command defined there --
this file is imported once, at the bottom of ``commands/config.py``, purely
for its module-level ``@config_app.command(...)`` side effect.

Thin CLI layer only: parses arguments, calls ``ConfigService``, formats
output. No business logic belongs here (see ``services/config_service.py``
for ``get_config_state`` / ``set_config_state``).
"""

import json
from typing import Any

import typer
from rich.markup import escape
from rich.syntax import Syntax

from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, resolve_branch
from .config import _handle_config_service_error, _parse_json_input, config_app


@config_app.command("state-get", rich_help_panel="State")
def config_state_get(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    row_id: str | None = typer.Option(
        None, "--row-id", help="Read this row's state instead of the config's root state"
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (defaults to active branch)"
    ),
) -> None:
    """Read the runtime ``state`` dict of a configuration or one of its rows.

    Storage API serves ``state`` inline in the config detail response only --
    there is no standalone ``GET .../state`` endpoint. When a config uses
    rows, the root state is typically unused; pass --row-id to read a
    row's own state.

    \b
    Examples:
      kbagent config state-get --project P --component-id C --config-id ID
      kbagent config state-get --project P --component-id C --config-id ID --row-id ROW
    """
    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)
    service = get_service(ctx, "config_service")

    try:
        result = service.get_config_state(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            row_id=row_id,
            branch_id=effective_branch,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_config_service_error(formatter, exc)

    if formatter.json_mode:
        formatter.output(result)
        return
    _format_state_get(formatter, result)


@config_app.command("state-set", rich_help_panel="State")
def config_state_set(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    state: str = typer.Option(
        ..., "--state", help="New state as inline JSON object, @file, or - for stdin"
    ),
    row_id: str | None = typer.Option(
        None, "--row-id", help="Write this row's state instead of the config's root state"
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (defaults to active branch)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without applying"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Overwrite the runtime ``state`` dict of a configuration or one of its rows.

    This replaces the ENTIRE state object -- it is not a merge. ``--state``
    must be a JSON object under the API's 4 MB cap.

    \b
    Examples:
      kbagent config state-set --project P --component-id C --config-id ID \\
        --state '{"lastId": 123}'
      kbagent config state-set --project P --component-id C --config-id ID \\
        --row-id ROW --state @state.json
      kbagent config state-set --project P --component-id C --config-id ID \\
        --state '{"lastId": 123}' --dry-run
    """
    formatter = get_formatter(ctx)
    config_store: ConfigStore = ctx.obj["config_store"]

    try:
        state_value = _parse_json_input(state)
    except (json.JSONDecodeError, FileNotFoundError) as exc:
        formatter.error(
            message=f"Invalid --state input: {exc}", error_code=ErrorCode.VALIDATION_ERROR
        )
        raise typer.Exit(code=2) from None

    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if not dry_run and not yes and not formatter.json_mode:
        target = f"{component_id}/{config_id}" + (f" row [{row_id}]" if row_id else "")
        if not typer.confirm(f"Overwrite state for {target}?"):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

    service = get_service(ctx, "config_service")

    try:
        result = service.set_config_state(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            state=state_value,
            row_id=row_id,
            branch_id=effective_branch,
            dry_run=dry_run,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_config_service_error(formatter, exc)

    if formatter.json_mode:
        formatter.output(result)
        return
    if result.get("dry_run"):
        _format_state_dry_run(formatter, result)
    else:
        _format_state_set(formatter, result)


def _format_state_target(result: dict) -> str:
    """Build a ``component_id/config_id[ row [row_id]]`` label for human output."""
    target = (
        f"[cyan]{escape(result['component_id'])}[/cyan]/[cyan]{escape(result['config_id'])}[/cyan]"
    )
    if result.get("row_id"):
        target += f" row [[yellow]{escape(str(result['row_id']))}[/yellow]]"
    return target


def _format_state_get(formatter: Any, result: dict) -> None:
    formatter.console.print(f"[bold]State for[/bold] {_format_state_target(result)}:\n")
    state = result.get("state") or {}
    if not state:
        formatter.console.print("  [dim](empty state)[/dim]")
        return
    formatter.console.print(
        Syntax(json.dumps(state, indent=2, ensure_ascii=False), "json", theme="monokai")
    )


def _format_state_set(formatter: Any, result: dict) -> None:
    branch_info = f" (branch {result['branch_id']})" if result.get("branch_id") else ""
    if not result.get("changed", True):
        formatter.console.print(
            f"[yellow]No change:[/yellow] state for {_format_state_target(result)} "
            f"already matches{branch_info}."
        )
        return
    formatter.success(f"Updated state for {_format_state_target(result)}{branch_info}")
    state = result.get("state") or {}
    if state:
        formatter.console.print(
            Syntax(json.dumps(state, indent=2, ensure_ascii=False), "json", theme="monokai")
        )


def _format_state_dry_run(formatter: Any, result: dict) -> None:
    changes = result.get("changes", [])
    if not changes:
        formatter.success("No changes detected.")
        return
    formatter.console.print(f"\n[bold]Dry-run: {len(changes)} change(s)[/bold]\n")
    for change in changes:
        formatter.console.print(f"  {change}")
    formatter.console.print()
