"""Data-app plain env-var commands -- env-set / env-list / env-get / env-remove.

A data app keeps its env vars in ``parameters.dataApp.secrets`` of its
``keboola.data-apps`` Storage config. A key with ``#`` is an encrypted secret
(``data-app secrets-*``); a key without ``#`` is a plain env var, stored in
clear. These commands manage the plain keys only and never encrypt. The logic
is in ``services/_data_app_env.py``. They attach to the ``data-app`` sub-app
via :func:`register_env_commands`, called at the bottom of ``data_app.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from ..effective_branch import resolve_branch
from ..errors import ConfigError, ErrorCode, KeboolaApiError, exit_code_for
from ..output import OutputFormatter
from ..services._data_app_env import DEFAULT_ENV_LIST_LIMIT
from ._data_app_runtime import _parse_secret_arg, _read_secrets_file
from ._helpers import (
    _confirm_or_abort,
    emit_api_error,
    existing_file_or_dash,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)
from ._project_ref import PROJECT_HELP

_BRANCH_HELP = "Storage branch ID for the linked config (defaults to production)."
_APP_ID_HELP = "Data Science numeric app id"


def _call(
    formatter: OutputFormatter, method: Callable[..., dict[str, Any]], **kwargs: Any
) -> dict[str, Any]:
    """Run a service method; report an API or config error and exit."""
    try:
        return method(**kwargs)
    except KeboolaApiError as exc:
        emit_api_error(formatter, exc)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        error_code = getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)
        formatter.error(message=exc.message, error_code=error_code)
        raise typer.Exit(code=exit_code_for(error_code)) from None


def data_app_env_set(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
    app_id: str = typer.Option(..., "--app-id", help=_APP_ID_HELP),
    var: list[str] | None = typer.Option(
        None,
        "--var",
        help=(
            "Plain env var as 'KEY=VALUE' (no '#'; stored in clear). Repeatable. "
            "Mutually exclusive with --var-file."
        ),
    ),
    var_file: Path | None = typer.Option(
        None,
        "--var-file",
        help="Read a JSON object mapping 'KEY' -> 'value' from a file (- for stdin).",
        readable=True,
        dir_okay=False,
        allow_dash=True,
        callback=existing_file_or_dash,
    ),
    branch: int | None = typer.Option(None, "--branch", help=_BRANCH_HELP),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the Storage PUT body without making the call."
    ),
    no_hint_next: bool = typer.Option(
        False,
        "--no-hint-next",
        help="Suppress the 'now run kbagent data-app deploy' hint in the output.",
    ),
) -> None:
    """Write plain (unencrypted) env vars to the linked Storage config.

    The value is stored in clear under the key without '#'. Use it for values
    that are not secret; use 'data-app secrets-set' for secrets. A key with
    '#' is refused, and so is a key whose runtime env var name an encrypted
    secret already has (remove the secret first with 'data-app secrets-remove').
    The runtime env var name is the key uppercased with '-' replaced by '_'.

    The command never deploys: the running container keeps the old config
    until the next 'kbagent data-app deploy'.

    Example: kbagent data-app env-set --project prod --app-id 123 --var LOG_LEVEL=debug
    """
    formatter = get_formatter(ctx)
    branch = resolve_branch(ctx.obj["config_store"], project, branch, ignore_active_branch=True)
    service = get_service(ctx, "data_app_service")

    if var and var_file:
        formatter.error(
            message="--var and --var-file are mutually exclusive; pick one input mode.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2) from None
    if not var and not var_file:
        formatter.error(
            message="Provide at least one --var 'KEY=VALUE' or --var-file PATH.",
            error_code=ErrorCode.MISSING_PARAMETER,
        )
        raise typer.Exit(code=2) from None

    env: dict[str, str] = {}
    try:
        for entry in var or []:
            key, value = _parse_secret_arg(entry, "--var")
            env[key] = value
        if var_file:
            env.update(_read_secrets_file(var_file, "--var-file"))
    except typer.BadParameter as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None

    result = _call(
        formatter,
        service.set_data_app_env,
        alias=project,
        app_id=app_id,
        env=env,
        branch_id=branch,
        dry_run=dry_run,
    )
    if no_hint_next:
        result.pop("next_step", None)
    formatter.output(
        result,
        lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
    )
    if not no_hint_next and not formatter.json_mode and result.get("next_step"):
        formatter.console.print(f"[dim]Next: {result['next_step']}[/dim]")


def data_app_env_list(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
    app_id: str = typer.Option(..., "--app-id", help=_APP_ID_HELP),
    branch: int | None = typer.Option(None, "--branch", help=_BRANCH_HELP),
    limit: int = typer.Option(
        DEFAULT_ENV_LIST_LIMIT,
        "--limit",
        "-L",
        min=1,
        help=f"Max env vars to return, sorted by key (default {DEFAULT_ENV_LIST_LIMIT}).",
    ),
) -> None:
    """List the plain (unencrypted) env vars of a data app, with their values.

    Lists the keys without '#' in parameters.dataApp.secrets. Encrypted
    secrets are listed by 'data-app secrets-list'.
    """
    formatter = get_formatter(ctx)
    branch = resolve_branch(ctx.obj["config_store"], project, branch, ignore_active_branch=True)
    service = get_service(ctx, "data_app_service")
    result = _call(
        formatter,
        service.list_data_app_env,
        alias=project,
        app_id=app_id,
        branch_id=branch,
        limit=limit,
    )
    if formatter.json_mode:
        formatter.output(result)
        return
    if not result["env"]:
        formatter.console.print("[dim]No plain env vars set on this data app.[/dim]")
        return
    formatter.console.print(
        f"\n[bold]{result['count']} env var(s)[/bold] on data app "
        f"[cyan]{result['app_id']}[/cyan] in [magenta]{result['project_alias']}[/magenta]:"
    )
    for entry in result["env"]:
        marker = (
            " [yellow](shadowed by runtime)[/yellow]" if entry.get("shadowed_by_runtime") else ""
        )
        formatter.console.print(
            f"  [bold]{entry['key']}[/bold] -> env [cyan]{entry['env_var']}[/cyan]"
            f" = {escape(str(entry['value']))}{marker}",
            highlight=False,
        )
    if result["has_more"]:
        formatter.err_console.print(
            f"[dim]More env vars exist; raise --limit (now {result['limit']}).[/dim]"
        )


def data_app_env_get(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
    app_id: str = typer.Option(..., "--app-id", help=_APP_ID_HELP),
    key: str = typer.Option(..., "--key", help="Plain env var key (no '#')."),
    branch: int | None = typer.Option(None, "--branch", help=_BRANCH_HELP),
) -> None:
    """Show ONE plain (unencrypted) env var of a data app, with its value.

    A key with '#' is refused: use 'data-app secrets-get' for secrets.
    """
    formatter = get_formatter(ctx)
    branch = resolve_branch(ctx.obj["config_store"], project, branch, ignore_active_branch=True)
    service = get_service(ctx, "data_app_service")
    result = _call(
        formatter,
        service.get_data_app_env,
        alias=project,
        app_id=app_id,
        key=key,
        branch_id=branch,
    )
    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold]{d['key']}[/bold] -> env [cyan]{d['env_var']}[/cyan] = "
            f"{escape(str(d['value']))}",
            highlight=False,
        ),
    )


def data_app_env_remove(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
    app_id: str = typer.Option(..., "--app-id", help=_APP_ID_HELP),
    key: list[str] = typer.Option(
        ..., "--key", help="Plain env var key to remove (no '#'). Repeatable."
    ),
    branch: int | None = typer.Option(None, "--branch", help=_BRANCH_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the Storage PUT body without making the call."
    ),
) -> None:
    """Remove plain (unencrypted) env vars from a data app. A missing key is exit 0.

    The config version history keeps the old values. The app can break at the
    next deploy if it needs the variable; the command never deploys. A key
    with '#' is refused: use 'data-app secrets-remove' for secrets.
    """
    formatter = get_formatter(ctx)
    branch = resolve_branch(ctx.obj["config_store"], project, branch, ignore_active_branch=True)
    service = get_service(ctx, "data_app_service")
    if not dry_run:
        _confirm_or_abort(
            formatter,
            yes,
            f"Remove {len(key)} env var(s) from data app {app_id} in '{project}'? "
            "This may break the app at next deploy if it depends on these values.",
        )
    result = _call(
        formatter,
        service.remove_data_app_env,
        alias=project,
        app_id=app_id,
        keys=key,
        branch_id=branch,
        dry_run=dry_run,
    )
    formatter.output(
        result,
        lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
    )


def register_env_commands(app: typer.Typer) -> None:
    """Attach the data-app env-* commands to the data-app sub-app."""
    app.command("env-set")(data_app_env_set)
    app.command("env-list")(data_app_env_list)
    app.command("env-get")(data_app_env_get)
    app.command("env-remove")(data_app_env_remove)
