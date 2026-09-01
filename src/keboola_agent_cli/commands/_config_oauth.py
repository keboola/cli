"""``kbagent config oauth-url`` -- OAuth authorization URL (issue #587 fallout).

Split out of ``commands/config.py`` purely for size: that module sits at its
grandfathered ``make loc-check`` ceiling, which may only shrink, and adding the
two-line clone registration hook pushed it over. Moving this self-contained
command out buys the file back its headroom instead of raising the recorded
limit -- CONTRIBUTING.md is explicit that the baseline is never regenerated to
silence a file you just grew.

Mounted onto ``config_app`` via :func:`register`, so the permission key stays
``config.oauth-url`` and it still shows up in ``kbagent config --help``.
"""

from __future__ import annotations

import typer
from rich.markup import escape

from ..auth.environment import open_browser
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code


def _should_open(*, no_open: bool, is_terminal: bool) -> bool:
    """Whether to hand the URL to a browser: interactive use, unless opted out."""
    return is_terminal and not no_open


def register(app: typer.Typer) -> None:
    """Mount the oauth-url command onto ``app`` (the ``config`` Typer group)."""

    @app.command(
        "oauth-url",
        rich_help_panel="OAuth",
        help=(
            "Requires master token. Generate an OAuth authorization URL for a component "
            "configuration and open it in the default browser."
        ),
    )
    def config_oauth_url(
        ctx: typer.Context,
        project: str = typer.Option(
            ...,
            "--project",
            help="Project alias",
        ),
        component_id: str = typer.Option(
            ...,
            "--component-id",
            help="Component ID (e.g. keboola.ex-google-drive)",
        ),
        config_id: str = typer.Option(
            ...,
            "--config-id",
            help="Configuration ID to authorize",
        ),
        redirect_url: str | None = typer.Option(
            None,
            "--redirect-url",
            help="Optional URL to return to after the OAuth flow completes (sets returnUrl query param)",
        ),
        no_open: bool = typer.Option(
            False,
            "--no-open",
            help="Only print the authorization URL, do not open a browser",
        ),
    ) -> None:
        """Generate an OAuth authorization URL for a component configuration.

        Mints a short-lived, component-scoped authorization link and opens it in
        the default browser, so the URL never has to be clicked or copied: it is
        ~200 characters, and a terminal or chat renderer that wraps it turns the
        visible link into its first row only, which drops the configuration id
        and makes the authorization page answer "Failed to load config data".
        The complete URL is still printed for copying elsewhere.

        The browser is left alone in `--json` mode and when stdout is not a
        terminal, so scripted and piped callers never get a stray window;
        `--no-open` suppresses it in interactive use too.

        \b
        Examples:
          kbagent config oauth-url --project P --component-id keboola.ex-google-drive --config-id ID

          # Print the URL without opening a browser (e.g. authorizing on another machine)
          kbagent config oauth-url --project P --component-id keboola.ex-google-drive --config-id ID \\
            --no-open

          # Redirect back to a custom URL after the OAuth flow completes
          kbagent config oauth-url --project P --component-id keboola.ex-google-drive --config-id ID \\
            --redirect-url https://example.com/oauth-done
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "config_service")

        try:
            result = service.get_oauth_url(
                alias=project,
                component_id=component_id,
                config_id=config_id,
                redirect_url=redirect_url,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(
                message=exc.message,
                error_code=exc.error_code,
                retryable=exc.retryable,
            )
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        url = result["url"]

        if formatter.json_mode:
            formatter.output(result)
            return

        formatter.console.print(
            f"[bold]OAuth URL for[/bold] [cyan]{escape(component_id)}[/cyan]/"
            f"[cyan]{escape(config_id)}[/cyan]:\n"
        )
        # soft_wrap keeps Rich from laying the URL out at the console width:
        # by default it inserts real newlines, so copying one visual row yields
        # a truncated URL. markup=False stops a literal "[" in the token from
        # being read as a style tag.
        formatter.console.print(url, soft_wrap=True, highlight=False, markup=False)

        should_open = _should_open(no_open=no_open, is_terminal=formatter.console.is_terminal)
        if should_open and open_browser(url):
            formatter.console.print(
                "\n[dim]Opened in your default browser. Grant access there.[/dim]"
            )
        else:
            formatter.console.print("\n[dim]Open this URL in a browser and grant access.[/dim]")
