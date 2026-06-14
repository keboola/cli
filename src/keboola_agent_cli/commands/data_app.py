"""Data-app commands -- create, list, detail, deploy, start, stop, delete, password, logs.

Thin CLI layer that delegates to :class:`DataAppService`. The underlying
Keboola Data Science API is not idempotent and has several footguns
(redeploy contract, cross-project KMS, transient stopped during initial
deploy); the service encodes them. The command layer's job is argument
parsing, mutual-exclusion validation, dual JSON / human output, and
exit-code mapping.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from ..constants import DEFAULT_JOB_RUN_TIMEOUT
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._data_app_git import register_git_commands
from ._data_app_runtime import register_secrets_commands
from ._helpers import (
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_manage_token,
)

# Canonical Keboola help-doc references appended to each --help epilog so
# operators have a one-click path to the rule a flag enforces.
_REF_PYTHON_JS = "https://help.keboola.com/data-apps/python-js/"
_REF_STORAGE_ACCESS = "https://help.keboola.com/data-apps/storage-access/"


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
            f"  [bold]{app['app_id']}[/bold] "
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
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    try:
        result = service.list_data_apps(aliases=project, branch_id=branch)
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
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
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    try:
        result = service.get_data_app(alias=project, app_id=app_id, branch_id=branch)
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    formatter.output(
        result,
        lambda c, d: (
            c.print(f"\n[bold]Data app:[/bold] {d.get('name', '')} ({d['app_id']})"),
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
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
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
            formatter.console.print(f"  [bold]App ID:[/bold] {result['app_id']}")
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
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
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
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
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
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])

    try:
        result = service.get_data_app_password(
            alias=project, app_id=app_id, manage_token=manage_token
        )
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
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


# ---------------------------------------------------------------------------
# data-app logs (Data Science /apps/{id}/logs/tail)
# ---------------------------------------------------------------------------


@data_app_app.command("logs")
def data_app_logs(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    lines: int | None = typer.Option(
        None,
        "--lines",
        help=(
            "Tail the last N lines (default 500 when neither --lines nor "
            "--since is set). Pass 0 to fetch the full current container "
            "buffer (no server-side cap). Mutually exclusive with --since."
        ),
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help=(
            "Fetch lines since this ISO 8601 timestamp WITH timezone "
            "(e.g. '2026-05-21T13:00:00Z' or '2026-05-21T13:00:00+00:00'). "
            "Mutually exclusive with --lines."
        ),
    ),
) -> None:
    """Tail the container logs for a deployed data app.

    Returns the full container stdout/stderr buffer including the spin-up
    trace ([TIMING] git_clone, uv install, supervisord, runtime stack
    traces). App must be running or recently-stopped -- never-started
    apps return HTTP 400 "App X is not running"; recover with
    ``kbagent data-app start`` or ``data-app deploy``.

    Auth: project Storage token only. No Manage token required.

    Note: the log buffer can echo runtime secrets the app printed to
    stdout/stderr (tracebacks, debug os.environ dumps). Consider secret
    hygiene before piping --json output into AI agent context.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")

    # Command-layer validations (UX-level usage errors -> exit 2). The
    # service has its own mutex guard for `kbagent serve` / programmatic
    # callers; see DataAppService.get_app_logs.
    if lines is not None and since is not None:
        formatter.error(
            message="--lines and --since are mutually exclusive; pass exactly one.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2) from None

    if lines is not None and lines < 0:
        formatter.error(
            message="--lines must be 0 (full buffer) or a positive integer.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2) from None

    if since is not None:
        try:
            parsed = datetime.fromisoformat(since)
        except ValueError as exc:
            formatter.error(
                message=(f"--since must be ISO 8601 (e.g. '2026-05-21T13:00:00Z'): {exc}"),
                error_code=ErrorCode.USAGE_ERROR,
            )
            raise typer.Exit(code=2) from None
        if parsed.tzinfo is None:
            formatter.error(
                message=(
                    "--since must include a timezone (e.g. 'Z' or '+00:00'); "
                    "the server rejects naive datetimes."
                ),
                error_code=ErrorCode.USAGE_ERROR,
            )
            raise typer.Exit(code=2) from None

    # Translate the CLI sentinels into the service kwargs:
    #   - neither set        -> apply default lines=500
    #   - lines=0            -> opt-in to full buffer (no params sent)
    #   - lines>0 or since=Y -> pass through verbatim
    if lines is None and since is None:
        lines_arg: int | None = 500
    elif lines == 0:
        lines_arg = None
    else:
        lines_arg = lines

    try:
        result = service.get_app_logs(
            alias=project,
            app_id=app_id,
            lines=lines_arg,
            since=since,
        )
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    def _print_logs(c: Console, d: dict) -> None:
        c.print(
            f"\n[bold]Logs[/bold] for data app [cyan]{escape(str(d['app_id']))}[/cyan] "
            f"in [magenta]{escape(d['project_alias'])}[/magenta] "
            f"([dim]{d['lines_returned']} lines[/dim])"
        )
        # ``markup=False`` so literal [TIMING], [INFO], etc. in the log
        # payload are not interpreted as Rich tags. ``highlight=False``
        # disables Rich's auto-highlighter for URLs / IPs / timestamps
        # in log lines (false positives that just add visual noise).
        # ``end=""`` because the server includes a trailing newline.
        c.print(d["text"], markup=False, highlight=False, end="")

    formatter.output(result, _print_logs)


# ---------------------------------------------------------------------------
# data-app validate-repo
# ---------------------------------------------------------------------------


@data_app_app.command("validate-repo")
def data_app_validate_repo(
    ctx: typer.Context,
    git_repo: str = typer.Option(
        ..., "--git-repo", help="GitHub repo URL (https://github.com/owner/repo)."
    ),
    git_branch: str = typer.Option(
        "main", "--git-branch", help="Git ref to validate (default: main)."
    ),
    git_public: bool = typer.Option(
        True,
        "--git-public/--no-git-public",
        help="Public repo (no PAT). Use --no-git-public for private repos and pass --git-pat-env / --git-pat-file.",
    ),
    git_pat_env: str | None = typer.Option(
        None,
        "--git-pat-env",
        help="Read GitHub PAT from this env var (recommended; no argv leak).",
    ),
    git_pat_file: Path | None = typer.Option(
        None,
        "--git-pat-file",
        help="Read GitHub PAT from this file.",
        exists=True,
        readable=True,
        dir_okay=False,
    ),
    type_: str = typer.Option(
        "python-js",
        "--type",
        help="Repo layout to validate against. Currently only 'python-js' is supported; other types tracked as follow-up.",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Treat WARN findings as failures (exit 1)."
    ),
) -> None:
    """Pre-flight check that a git repo follows the Keboola data-app Golden Rule.

    Walks the repo via GitHub Contents + Trees API and validates the
    documented structure (keboola-config/ tree, pyproject.toml, no
    'pip install' in setup.sh, requires-python at-or-below the runtime
    pin, etc.). Each check emits BLOCKING / WARN / OK with a citation
    to the help-doc anchor that defines the rule.

    Reference: https://help.keboola.com/data-apps/python-js/
    """

    formatter = get_formatter(ctx)
    service = get_service(ctx, "repo_validate_service")

    if git_pat_env and git_pat_file:
        formatter.error(
            message="--git-pat-env and --git-pat-file are mutually exclusive.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2) from None

    pat_supplied = git_pat_env is not None or git_pat_file is not None
    if pat_supplied and git_public:
        # The default --git-public means "anonymous fetch"; sending a PAT
        # with it is a contradiction (the resulting 404 would lead to a
        # 'private repo -- pass --git-pat-env' message recommending the
        # flag the user already passed). Fail loud instead.
        formatter.error(
            message=(
                "--git-pat-env / --git-pat-file requires --no-git-public; the "
                "default --git-public flag opts into an anonymous fetch and "
                "would silently drop the PAT."
            ),
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2) from None

    pat: str | None = None
    if git_pat_env is not None:
        pat = _read_pat_from_env(git_pat_env)
    elif git_pat_file is not None:
        pat = _read_pat_from_file(git_pat_file)

    try:
        result = service.validate_repo(
            git_repo=git_repo,
            git_branch=git_branch,
            git_public=git_public,
            git_pat=pat,
            type_=type_,
            strict=strict,
        )
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        verdict_colour = (
            "red"
            if result["verdict"] == "BLOCKING"
            else "yellow"
            if result["verdict"] == "WARN"
            else "green"
        )
        formatter.console.print(
            f"\n[bold {verdict_colour}]{result['verdict']}[/bold {verdict_colour}] "
            f"-- {result['blocking_count']} BLOCKING, "
            f"{result['warn_count']} WARN, {result['ok_count']} OK"
        )
        for check in result["checks"]:
            sev = check["severity"]
            colour = "red" if sev == "BLOCKING" else "yellow" if sev == "WARN" else "green"
            line = f"  [{colour}]{sev:<8}[/{colour}] {check['name']}"
            if check.get("message"):
                line += f" -- {check['message']}"
            formatter.console.print(line)
        formatter.console.print(f"\n[dim]{result['message']}[/dim]")

    if result.get("is_failure"):
        # validate-repo's own exit code: BLOCKING (or strict-WARN) -> 1.
        # We bypass the structured-error formatter because validate-repo
        # output is itself the structured error envelope.
        if formatter.json_mode:
            # JSON envelope is already printed; just exit non-zero.
            raise typer.Exit(code=1)
        # Human mode: the verdict line above conveyed the failure.
        raise typer.Exit(code=1)


# Attach the data-app git-* and secrets-* commands. They live in
# _data_app_git.py / _data_app_runtime.py to keep this module under the
# file-size budget (CONTRIBUTING.md "File-size budgets"); they still register
# as `kbagent data-app git-*` / `secrets-*` on this sub-app.
register_git_commands(data_app_app)
register_secrets_commands(data_app_app)
