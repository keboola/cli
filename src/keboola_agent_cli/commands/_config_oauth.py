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

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code


def register(app: typer.Typer) -> None:
    """Mount the oauth-url command onto ``app`` (the ``config`` Typer group)."""

    @app.command(
        "oauth-url",
        rich_help_panel="OAuth",
        help=(
            "Requires master token. Generate an OAuth authorization URL for a component configuration."
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
    ) -> None:
        """Generate an OAuth authorization URL for a component configuration.

        Opens a short-lived, component-scoped authorization link.
        The user must open this URL in a browser and grant access.

        \b
        Examples:
          kbagent config oauth-url --project P --component-id keboola.ex-google-drive --config-id ID

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

        if formatter.json_mode:
            formatter.output(result)
        else:
            formatter.console.print(
                f"[bold]OAuth URL for[/bold] [cyan]{escape(component_id)}[/cyan]/"
                f"[cyan]{escape(config_id)}[/cyan]:\n"
            )
            formatter.console.print(f"  [link]{result['url']}[/link]")
            formatter.console.print("\n[dim]Open this URL in a browser and grant access.[/dim]")
