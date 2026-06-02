"""`kbagent project login` -- browser OAuth (PKCE) project authorization.

Lives in its own module because commands/project.py is over its file-size
budget; the command is registered onto ``project_app`` from there.
Thin CLI layer per conventions: argument parsing, formatter, error mapping.
"""

import typer

from ..constants import DEFAULT_STACK_URL, ENV_KBC_STORAGE_API_URL
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code


def project_login(
    ctx: typer.Context,
    url: str = typer.Option(
        DEFAULT_STACK_URL,
        help="Keboola stack URL to log into (e.g. connection.keboola.com)",
        envvar=ENV_KBC_STORAGE_API_URL,
    ),
    alias: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Alias to register the project under. Defaults to the slugified "
            "project name. Re-login into an already-registered project "
            "updates it in place."
        ),
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        min=1,
        max=65535,
        help=(
            "Explicit loopback callback port. Default: first free port from "
            "the whitelisted set (8765-8769)."
        ),
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help=(
            "Do not launch a browser; print the login URL to open manually "
            "(e.g. when kbagent runs on a remote host)."
        ),
    ),
    timeout: float = typer.Option(
        300.0,
        "--timeout",
        min=10,
        help="Seconds to wait for the browser login to complete.",
    ),
) -> None:
    """Log into a Keboola project via the browser (OAuth + PKCE).

    Opens the stack's login page; you authenticate and pick the project, and
    kbagent receives the credentials on a localhost callback -- no manual
    token copying. The session auto-renews in the background; when it
    eventually expires, re-run this command.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "oauth_login_service")

    def show_authorize_url(authorize_url: str) -> None:
        """Print the login URL before blocking on the callback (stderr,
        so --json stdout stays a single parseable document)."""
        if no_browser:
            formatter.err_console.print(
                f"Open this URL in your browser to log in:\n  [bold]{authorize_url}[/bold]"
            )
        else:
            formatter.err_console.print(
                "Opening your browser to complete the Keboola login... "
                f"(or open manually: {authorize_url})"
            )
        formatter.err_console.print("Waiting for the browser login to finish...")

    try:
        result = service.login(
            url,
            alias=alias,
            port=port,
            open_browser=not no_browser,
            timeout=timeout,
            on_authorize_url=show_authorize_url,
        )
        formatter.output(
            result,
            lambda c, d: c.print(
                f"[bold green]Success:[/bold green] Logged into project "
                f"[bold]{d['project_name']}[/bold] (id: {d['project_id']}) "
                f"as [bold]{d['alias']}[/bold]"
                + (" [dim](re-authenticated)[/dim]" if d.get("re_authenticated") else "")
                + "\nThe session renews automatically; re-run "
                "[bold]kbagent project login[/bold] if it ever expires."
            ),
        )
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
