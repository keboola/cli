"""CLI commands for python-js data app drafts."""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ._helpers import _confirm_or_abort, get_formatter, get_service, handle_service_call
from ._project_ref import PROJECT_HELP

_DraftId = Annotated[
    str | None,
    typer.Option("--draft-id", help="Draft UUID. Omit only when targeting a legacy draft."),
]
_AppId = Annotated[str, typer.Option("--app-id", help="Data app ID.")]


def register_draft_commands(app: typer.Typer) -> None:
    @app.command("draft-create")
    def draft_create(
        ctx: typer.Context,
        app_id: _AppId,
        project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
        git_branch: str = typer.Option(
            ..., "--git-branch", help="Existing branch on the app's Git remote."
        ),
    ) -> None:
        """Start a python-js draft on a Git branch without deploying or affecting production."""
        service = get_service(ctx, "data_app_service")
        result = handle_service_call(
            ctx, service.create_data_app_draft, project, app_id, git_branch
        )

        def render(console: Console, data: dict) -> None:
            draft = data["draft"]
            console.print(
                Text(f"Draft {draft['draft_id']} on {draft['git_branch']} ({draft['run_state']}).")
            )
            if draft["url"]:
                console.print(Text(draft["url"]))
            else:
                console.print(Text("The draft has no URL yet. Wait before minting a preview link."))

        get_formatter(ctx).output(result, render)

    @app.command("draft-list")
    def draft_list(
        ctx: typer.Context,
        app_id: _AppId,
        project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
        limit: int | None = typer.Option(
            None, "--limit", "-L", min=1, help="Maximum drafts to return."
        ),
    ) -> None:
        """List the drafts of a data app."""
        service = get_service(ctx, "data_app_service")
        result = handle_service_call(
            ctx, service.list_data_app_drafts, project, app_id, limit=limit
        )

        def render(console: Console, data: dict) -> None:
            table = Table("Draft ID", "Git Branch", "State", "URL")
            for draft in data["drafts"]:
                table.add_row(
                    *(
                        Text(draft[key] or "")
                        for key in ("draft_id", "git_branch", "run_state", "url")
                    )
                )
            console.print(table)

        get_formatter(ctx).output(result, render)

    @app.command("draft-delete")
    def draft_delete(
        ctx: typer.Context,
        app_id: _AppId,
        project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
        draft_id: _DraftId = None,
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Check the target without deleting it."
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    ) -> None:
        """Delete a draft without deleting its Git branch or production app."""
        formatter = get_formatter(ctx)
        service = get_service(ctx, "data_app_service")
        label = f"draft {draft_id} of app {app_id}" if draft_id else f"legacy draft app {app_id}"
        if not dry_run:
            _confirm_or_abort(formatter, yes, f"Delete {label}?")
        result = handle_service_call(
            ctx, service.delete_data_app_draft, project, app_id, draft_id, dry_run=dry_run
        )
        formatter.output(
            result,
            lambda console, data: console.print(
                Text(f"Would delete {label}." if data.get("dry_run") else f"Deleted {label}.")
            ),
        )

    @app.command("preview-link")
    def preview_link(
        ctx: typer.Context,
        app_id: _AppId,
        project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
        draft_id: _DraftId = None,
    ) -> None:
        """Mint a private, short-lived login link. Open it only in a browser; do not share it."""
        service = get_service(ctx, "data_app_service")
        result = handle_service_call(
            ctx, service.get_data_app_preview_link, project, app_id, draft_id
        )

        def render(console: Console, data: dict) -> None:
            console.print(
                Text(f"Open in a browser before {data['link_expires_at']}. Do not share this link.")
            )
            console.print(Text(data["url"]))

        get_formatter(ctx).output(result, render)
