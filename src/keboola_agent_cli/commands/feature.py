"""Feature-flag management commands (super-admin Manage API).

Thin CLI layer: parses arguments, calls FeatureService, formats output.
No business logic belongs here.

All operations require a super-admin Manage API token. It is read from an
interactive hidden prompt by default (never persisted, never a CLI argument);
pass the top-level --allow-env-manage-token to read KBC_MANAGE_API_TOKEN from
env (CI/CD). See ``resolve_manage_token`` for the full default-deny policy.
"""

from __future__ import annotations

from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_manage_token,
)

feature_app = typer.Typer(help="Feature flag management (requires super-admin Manage API token)")


@feature_app.callback(invoke_without_command=True)
def _feature_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "feature")


def _handle_errors(formatter: Any, exc: Exception) -> NoReturn:
    """Map a ConfigError / KeboolaApiError to a structured error + Exit."""
    if isinstance(exc, ConfigError):
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    if isinstance(exc, KeboolaApiError):
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    raise exc


def _format_feature_catalogue(console: Console, data: dict[str, Any]) -> None:
    """Render the stack feature catalogue as a Rich table."""
    features = data.get("features") or []
    title = f"Features on {data.get('stack_url', '')} ({len(features)} total)"
    table = Table(title=title)
    table.add_column("Name", style="bold cyan")
    table.add_column("Title")
    table.add_column("Type", style="dim")
    table.add_column("Description", style="dim")
    for feat in features:
        table.add_row(
            feat.get("name", ""),
            feat.get("title", ""),
            feat.get("type", ""),
            feat.get("description", ""),
        )
    console.print(table)


def _format_assigned_features(console: Console, data: dict[str, Any]) -> None:
    """Render features assigned to a project or user as a Rich table."""
    features = data.get("features") or []
    owner = (
        f"project [cyan]{data.get('alias')}[/cyan] (id={data.get('project_id')})"
        if "project_id" in data
        else f"user [cyan]{data.get('email')}[/cyan]"
    )
    if not features:
        console.print(f"No features assigned to {owner}.")
        return
    table = Table(title=f"Features assigned to {owner} ({len(features)} total)")
    table.add_column("Name", style="bold cyan")
    table.add_column("Title")
    table.add_column("Description", style="dim")
    for feat in features:
        table.add_row(feat.get("name", ""), feat.get("title", ""), feat.get("description", ""))
    console.print(table)


def _format_write_result(console: Console, data: dict[str, Any]) -> None:
    """Render the outcome of an add/remove operation."""
    status = data.get("status", "")
    feature = data.get("feature", "")
    target = (
        f"project [cyan]{data.get('alias')}[/cyan] (id={data.get('project_id')})"
        if "project_id" in data
        else f"user [cyan]{data.get('email')}[/cyan]"
    )
    if status == "dry_run":
        verb = "add to" if data.get("action") == "add" else "remove from"
        console.print(
            f"[bold yellow]DRY RUN[/bold yellow] would {verb} {target}: feature [bold]{feature}[/bold]"
        )
    elif status == "added":
        console.print(f"[bold green]Added[/bold green] feature [bold]{feature}[/bold] to {target}.")
    elif status == "removed":
        console.print(f"[bold red]Removed[/bold red] feature [bold]{feature}[/bold] from {target}.")


# ── Stack catalogue ───────────────────────────────────────────────────


@feature_app.command("list")
def feature_list(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", "-p", help="Project alias (used to resolve the stack URL)"
    ),
) -> None:
    """List all feature flags defined on the stack."""
    formatter = get_formatter(ctx)
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "feature_service")
    try:
        result = service.list_stack_features(manage_token=manage_token, alias=project)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_feature_catalogue)


# ── Project features ──────────────────────────────────────────────────


@feature_app.command("project-show")
def feature_project_show(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
) -> None:
    """Show feature flags assigned to a project."""
    formatter = get_formatter(ctx)
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "feature_service")
    try:
        result = service.list_project_features(manage_token=manage_token, alias=project)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_assigned_features)


@feature_app.command("project-add")
def feature_project_add(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    feature: str = typer.Option(..., "--feature", "-f", help="Feature name to enable"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without making changes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Enable a feature flag on a project."""
    formatter = get_formatter(ctx)
    if (
        not dry_run
        and not formatter.json_mode
        and not yes
        and not typer.confirm(f"Add feature '{feature}' to project {project}?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "feature_service")
    try:
        result = service.add_project_feature(
            manage_token=manage_token, alias=project, feature=feature, dry_run=dry_run
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_write_result)


@feature_app.command("project-remove")
def feature_project_remove(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    feature: str = typer.Option(..., "--feature", "-f", help="Feature name to disable"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without making changes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Disable a feature flag on a project (destructive)."""
    formatter = get_formatter(ctx)
    if (
        not dry_run
        and not formatter.json_mode
        and not yes
        and not typer.confirm(
            f"Remove feature '{feature}' from project {project}? This is destructive."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "feature_service")
    try:
        result = service.remove_project_feature(
            manage_token=manage_token, alias=project, feature=feature, dry_run=dry_run
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_write_result)


# ── User features ─────────────────────────────────────────────────────


@feature_app.command("user-show")
def feature_user_show(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", "-p", help="Project alias (used to resolve the stack URL)"
    ),
    email: str = typer.Option(..., "--email", "-e", help="User email address"),
) -> None:
    """Show feature flags assigned to a user."""
    formatter = get_formatter(ctx)
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "feature_service")
    try:
        result = service.list_user_features(manage_token=manage_token, alias=project, email=email)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_assigned_features)


@feature_app.command("user-add")
def feature_user_add(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", "-p", help="Project alias (used to resolve the stack URL)"
    ),
    email: str = typer.Option(..., "--email", "-e", help="User email address"),
    feature: str = typer.Option(..., "--feature", "-f", help="Feature name to enable"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without making changes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Enable a feature flag on a user."""
    formatter = get_formatter(ctx)
    if (
        not dry_run
        and not formatter.json_mode
        and not yes
        and not typer.confirm(f"Add feature '{feature}' to user {email}?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "feature_service")
    try:
        result = service.add_user_feature(
            manage_token=manage_token, alias=project, email=email, feature=feature, dry_run=dry_run
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_write_result)


@feature_app.command("user-remove")
def feature_user_remove(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", "-p", help="Project alias (used to resolve the stack URL)"
    ),
    email: str = typer.Option(..., "--email", "-e", help="User email address"),
    feature: str = typer.Option(..., "--feature", "-f", help="Feature name to disable"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without making changes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Disable a feature flag on a user (destructive)."""
    formatter = get_formatter(ctx)
    if (
        not dry_run
        and not formatter.json_mode
        and not yes
        and not typer.confirm(f"Remove feature '{feature}' from user {email}? This is destructive.")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "feature_service")
    try:
        result = service.remove_user_feature(
            manage_token=manage_token, alias=project, email=email, feature=feature, dry_run=dry_run
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_write_result)
