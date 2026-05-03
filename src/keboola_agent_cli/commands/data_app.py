"""Data-app commands -- create, list, detail, deploy, start, stop, delete, password.

Thin CLI layer that delegates to :class:`DataAppService`. The underlying
Keboola Data Science API is not idempotent and has several footguns
(redeploy contract, cross-project KMS, transient stopped during initial
deploy); the service encodes them. The command layer's job is argument
parsing, mutual-exclusion validation, dual JSON / human output, and
exit-code mapping.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from ..constants import DEFAULT_JOB_RUN_TIMEOUT
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    emit_hint,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_manage_token,
    should_hint,
)

data_app_app = typer.Typer(help="Keboola data-app lifecycle (create, deploy, manage)")


@data_app_app.callback(invoke_without_command=True)
def _data_app_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "data-app")


def _print_data_app_table(formatter, result: dict) -> None:
    """Compact human-readable list of data apps across projects."""
    apps = result.get("apps", [])
    if not apps:
        formatter.console.print("[dim]No data apps found.[/dim]")
        return
    for app in apps:
        formatter.console.print(
            f"  [bold]{app['id']}[/bold] "
            f"[cyan]{app.get('name', '')}[/cyan] "
            f"({app.get('type', '?')}) "
            f"state=[yellow]{app.get('state', '?')}[/yellow] "
            f"desired={app.get('desired_state', '?')} "
            f"v{app.get('config_version', '?')} "
            f"in [magenta]{app['project_alias']}[/magenta]"
        )
        if app.get("url"):
            formatter.console.print(f"      [dim]{app['url']}[/dim]")


def _read_pat_from_env(env_var: str) -> str:
    value = os.environ.get(env_var, "")
    if not value:
        raise typer.BadParameter(
            f"Environment variable {env_var} is unset or empty.",
            param_hint="--git-pat-env",
        )
    return value


def _read_pat_from_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise typer.BadParameter(
            f"Cannot read PAT file {path}: {exc}",
            param_hint="--git-pat-file",
        ) from exc


# ---------------------------------------------------------------------------
# data-app list
# ---------------------------------------------------------------------------


@data_app_app.command("list")
def data_app_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (repeatable). None = all projects.",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Storage branch ID for the config-name lookup (defaults to production).",
    ),
) -> None:
    """List data apps across one or more registered projects."""
    if should_hint(ctx):
        emit_hint(ctx, "data-app.list", project=project, branch=branch)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    try:
        result = service.list_data_apps(aliases=project, branch_id=branch)
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _print_data_app_table(formatter, result)
        emit_project_warnings(formatter, result)


# ---------------------------------------------------------------------------
# data-app detail
# ---------------------------------------------------------------------------


@data_app_app.command("detail")
def data_app_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Storage branch ID for the linked config (defaults to production).",
    ),
) -> None:
    """Show merged Data Science + Storage detail for one data app."""
    if should_hint(ctx):
        emit_hint(ctx, "data-app.detail", project=project, app_id=app_id, branch=branch)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    try:
        result = service.get_data_app(alias=project, app_id=app_id, branch_id=branch)
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    formatter.output(
        result,
        lambda c, d: (
            c.print(f"\n[bold]Data app:[/bold] {d.get('name', '')} ({d['id']})"),
            c.print(f"  [bold]Project:[/bold] {d['project_alias']}"),
            c.print(f"  [bold]Slug:[/bold] {d.get('slug', '')}"),
            c.print(f"  [bold]Type:[/bold] {d.get('type', '')}"),
            c.print(
                f"  [bold]State:[/bold] [yellow]{d.get('state', '?')}[/yellow] "
                f"(desired={d.get('desired_state', '?')})"
            ),
            c.print(
                f"  [bold]Config version:[/bold] storage="
                f"{d.get('config_version_storage', '?')}, "
                f"deployed={d.get('config_version_deployed', '?')}"
            ),
            c.print(f"  [bold]Size:[/bold] {d.get('size', '')}"),
            c.print(f"  [bold]Auto-suspend:[/bold] {d.get('auto_suspend_after_seconds', '?')}s"),
            c.print(f"  [bold]URL:[/bold] {d.get('url', '')}"),
            c.print(f"  [bold]Last started:[/bold] {d.get('last_start_timestamp', '')}"),
            c.print(f"  [bold]Git:[/bold] {d.get('git', {})}"),
        ),
    )


# ---------------------------------------------------------------------------
# data-app create
# ---------------------------------------------------------------------------


@data_app_app.command("create")
def data_app_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    name: str = typer.Option(..., "--name", help="Display name shown in the Keboola UI"),
    description: str = typer.Option(
        "",
        "--description",
        help="Long-form description (markdown). Mutually exclusive with --description-file.",
    ),
    description_file: Path | None = typer.Option(
        None,
        "--description-file",
        help="Read description from a file. Mutually exclusive with --description.",
        exists=True,
        readable=True,
    ),
    slug: str = typer.Option(
        ..., "--slug", help="URL slug (lowercase alphanumeric, hyphens; 2-64 chars)"
    ),
    git_repo: str = typer.Option(..., "--git-repo", help="GitHub repository URL"),
    git_branch: str = typer.Option("main", "--git-branch", help="Git branch to clone"),
    git_public: bool = typer.Option(
        False,
        "--git-public/--no-git-public",
        help="Mark the repository as public (no credentials needed).",
    ),
    git_username: str | None = typer.Option(
        None, "--git-username", help="GitHub username (required for private repos)"
    ),
    git_pat_env: str | None = typer.Option(
        None,
        "--git-pat-env",
        help="Environment variable containing the plaintext PAT (recommended).",
    ),
    git_pat_file: Path | None = typer.Option(
        None,
        "--git-pat-file",
        help="File containing the plaintext PAT.",
        exists=True,
        readable=True,
    ),
    git_pat_encrypted: str | None = typer.Option(
        None,
        "--git-pat-encrypted",
        help=(
            "Pre-encrypted PAT (KBC::Project... ciphertext). Must be encrypted "
            "against THIS project's KMS -- ciphertext does not cross projects."
        ),
    ),
    auth: str = typer.Option(
        "password",
        "--auth",
        help="Authentication mode: 'password' (simpleAuth) or 'public' (no auth gate).",
    ),
    size: str = typer.Option("tiny", "--size", help="Runtime size: tiny, small, medium, or large."),
    auto_suspend: int = typer.Option(
        900,
        "--auto-suspend",
        help="Auto-suspend after N seconds idle (0 disables).",
    ),
    type_: str = typer.Option(
        "python-js",
        "--type",
        help="Runtime type. Default 'python-js' covers Python AND Node apps.",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Keboola dev branch ID (defaults to production).",
    ),
    no_deploy: bool = typer.Option(
        False,
        "--no-deploy",
        help="Skip the deploy step; create the shell + Storage config only.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Block until state == running (or error). Respects pitfall #1: stopped is not terminal.",
    ),
    timeout: float = typer.Option(
        DEFAULT_JOB_RUN_TIMEOUT,
        "--timeout",
        help="Maximum seconds to wait for state == running (default 300).",
    ),
    keep_on_failure: bool = typer.Option(
        False,
        "--keep-on-failure",
        help="Keep the orphan deployment shell if PUT or initial deploy fails (forensics).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the three request bodies without making any API call.",
    ),
) -> None:
    """Create a Keboola data app end-to-end (POST + encrypt + PUT + deploy)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "data-app.create",
            project=project,
            name=name,
            description=description,
            slug=slug,
            git_repo=git_repo,
            git_branch=git_branch,
            git_public=git_public,
            git_username=git_username,
            git_pat_env=git_pat_env,
            git_pat_file=str(git_pat_file) if git_pat_file else None,
            git_pat_encrypted=git_pat_encrypted,
            auth=auth,
            size=size,
            auto_suspend=auto_suspend,
            type_=type_,
            branch=branch,
            no_deploy=no_deploy,
            wait=wait,
            timeout=timeout,
            keep_on_failure=keep_on_failure,
            dry_run=dry_run,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")

    # Mutual exclusion: --description vs --description-file
    if description and description_file:
        formatter.error(
            message="Specify either --description or --description-file, not both.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    effective_description = description
    if description_file is not None:
        try:
            effective_description = description_file.read_text(encoding="utf-8")
        except OSError as exc:
            formatter.error(
                message=f"Cannot read --description-file {description_file}: {exc}",
                error_code=ErrorCode.READ_ERROR,
            )
            raise typer.Exit(code=2) from None

    # Mutual exclusion of git PAT input modes (CLI layer).
    pat_inputs_set = sum(1 for v in (git_pat_env, git_pat_file, git_pat_encrypted) if v is not None)
    if pat_inputs_set > 1:
        formatter.error(
            message=(
                "Specify exactly one of --git-pat-env / --git-pat-file / "
                "--git-pat-encrypted; they are mutually exclusive."
            ),
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    # Resolve PAT plaintext if needed (env / file). Encrypted form passes
    # through; service validates the prefix.
    pat_plaintext: str | None = None
    if git_pat_env is not None:
        pat_plaintext = _read_pat_from_env(git_pat_env)
    elif git_pat_file is not None:
        pat_plaintext = _read_pat_from_file(git_pat_file)

    try:
        result = service.create_data_app(
            alias=project,
            name=name,
            description=effective_description,
            slug=slug,
            git_repo=git_repo,
            git_branch=git_branch,
            git_public=git_public,
            git_username=git_username,
            git_pat_plaintext=pat_plaintext,
            git_pat_encrypted=git_pat_encrypted,
            auth=auth,
            size=size,
            auto_suspend_after_seconds=auto_suspend,
            type_=type_,
            branch_id=branch,
            deploy=not no_deploy,
            wait=wait,
            timeout_seconds=timeout,
            keep_on_failure=keep_on_failure,
            dry_run=dry_run,
        )
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        if result.get("dry_run"):
            formatter.console.print("[bold]DRY RUN -- no API calls were made.[/bold]")
            formatter.console.print(result["requests"])
        else:
            formatter.console.print(
                f"[bold green]Success:[/bold green] {result.get('message', '')}"
            )
            formatter.console.print(f"  [bold]App ID:[/bold] {result['id']}")
            formatter.console.print(f"  [bold]Config ID:[/bold] {result['config_id']}")
            if result.get("url"):
                formatter.console.print(f"  [bold]URL:[/bold] {result['url']}")
            formatter.console.print(
                f"  [bold]State:[/bold] {result.get('state', '?')} "
                f"(desired={result.get('desired_state', '?')})"
            )


# ---------------------------------------------------------------------------
# data-app deploy / start / stop
# ---------------------------------------------------------------------------


def _run_lifecycle(
    ctx: typer.Context,
    service_method: str,
    *,
    project: str,
    app_id: str,
    wait: bool,
    timeout: float,
    extra: dict | None = None,
) -> None:
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    method = getattr(service, service_method)
    kwargs = {"alias": project, "app_id": app_id, "wait": wait, "timeout_seconds": timeout}
    if extra:
        kwargs.update(extra)
    try:
        result = method(**kwargs)
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output(
        result,
        lambda c, d: c.print(f"[bold green]Success:[/bold green] {d.get('message', '')}"),
    )


@data_app_app.command("deploy")
def data_app_deploy(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    config_version: str | None = typer.Option(
        None,
        "--config-version",
        help="Pin a specific Storage config version (defaults to latest).",
    ),
    wait: bool = typer.Option(False, "--wait", help="Block until running or error."),
    timeout: float = typer.Option(
        DEFAULT_JOB_RUN_TIMEOUT, "--timeout", help="Max seconds to wait."
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Storage branch for reading the latest version (defaults to production).",
    ),
) -> None:
    """Deploy the latest Storage config (the §9 redeploy contract)."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "data-app.deploy",
            project=project,
            app_id=app_id,
            config_version=config_version,
            wait=wait,
            branch=branch,
        )
        return
    _run_lifecycle(
        ctx,
        "deploy_data_app",
        project=project,
        app_id=app_id,
        wait=wait,
        timeout=timeout,
        extra={"config_version": config_version, "branch_id": branch},
    )


@data_app_app.command("start")
def data_app_start(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    wait: bool = typer.Option(False, "--wait", help="Block until running or error."),
    timeout: float = typer.Option(
        DEFAULT_JOB_RUN_TIMEOUT, "--timeout", help="Max seconds to wait."
    ),
) -> None:
    """Wake an auto-suspended data app at its currently-pinned configVersion."""
    if should_hint(ctx):
        emit_hint(ctx, "data-app.start", project=project, app_id=app_id, wait=wait)
        return
    _run_lifecycle(
        ctx,
        "start_data_app",
        project=project,
        app_id=app_id,
        wait=wait,
        timeout=timeout,
    )


@data_app_app.command("stop")
def data_app_stop(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    wait: bool = typer.Option(False, "--wait", help="Block until stopped."),
    timeout: float = typer.Option(
        DEFAULT_JOB_RUN_TIMEOUT, "--timeout", help="Max seconds to wait."
    ),
) -> None:
    """Stop a running data app (preserves the URL and Storage config)."""
    if should_hint(ctx):
        emit_hint(ctx, "data-app.stop", project=project, app_id=app_id, wait=wait)
        return
    _run_lifecycle(
        ctx,
        "stop_data_app",
        project=project,
        app_id=app_id,
        wait=wait,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# data-app delete
# ---------------------------------------------------------------------------


@data_app_app.command("delete")
def data_app_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Delete the deployment AND the Storage config (cascade, irreversible)."""
    if should_hint(ctx):
        emit_hint(ctx, "data-app.delete", project=project, app_id=app_id)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")

    if (
        not yes
        and not formatter.json_mode
        and not typer.confirm(
            f"Delete data app {app_id} in '{project}'? "
            "This deletes the deployment AND the Storage config (irreversible)."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.delete_data_app(alias=project, app_id=app_id)
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    formatter.output(
        result,
        lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
    )


# ---------------------------------------------------------------------------
# data-app password (requires Manage token)
# ---------------------------------------------------------------------------


@data_app_app.command("password")
def data_app_password(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
) -> None:
    """Retrieve the simpleAuth password for a password-gated data app.

    Requires the Manage API token in addition to the project's Storage
    token. Default-deny since 0.28.0: read from an interactive hidden
    prompt; pass top-level --allow-env-manage-token to read
    KBC_MANAGE_API_TOKEN from env (CI/CD). Never persisted, never logged.
    """
    if should_hint(ctx):
        emit_hint(ctx, "data-app.password", project=project, app_id=app_id)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])

    try:
        result = service.get_data_app_password(
            alias=project, app_id=app_id, manage_token=manage_token
        )
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    formatter.output(
        result,
        lambda c, d: (
            c.print(f"[bold green]Success:[/bold green] {d['message']}"),
            c.print(f"\n[bold yellow]Password:[/bold yellow] {d['password']}"),
        ),
    )
