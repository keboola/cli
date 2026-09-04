"""``data-app update`` -- change deployment settings on an existing app.

Split out of ``data_app.py`` to respect the file-size budget
(CONTRIBUTING.md "File-size budgets"). The command attaches to the existing
``data-app`` Typer sub-app via :func:`register_settings_commands`, called at
the bottom of ``data_app.py``, so it surfaces as ``kbagent data-app update``
with the usual ``data-app.update`` permission key.

``create`` could already set Storage access and auto-suspend; nothing could
change them afterwards. The gap mattered most for ``runtime.workspace.enabled``
-- the switch that makes the platform inject ``KBC_TOKEN`` / ``WORKSPACE_ID``
/ ``QUERY_SERVICE_URL`` -- because an app missing it deploys, reports
``state=running`` and passes its health probe while every Storage call fails.
"""

from __future__ import annotations

import typer
from rich.console import Console

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code

_REF_STORAGE_ACCESS = "https://help.keboola.com/data-apps/storage-access/"


def register_settings_commands(app: typer.Typer) -> None:
    """Attach the ``data-app update`` command to the data-app sub-app."""

    @app.command("update")
    def data_app_update(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
        workspace: bool | None = typer.Option(
            None,
            "--workspace/--no-workspace",
            help=(
                "Grant/revoke Storage access (runtime.workspace.enabled). This is what "
                "makes the platform inject KBC_TOKEN, WORKSPACE_ID, QUERY_SERVICE_URL "
                "and KBC_WORKSPACE_MANIFEST_PATH. Omit to leave unchanged."
            ),
        ),
        auto_suspend: int | None = typer.Option(
            None,
            "--auto-suspend",
            help="Seconds of inactivity before the app is suspended.",
        ),
        size: str | None = typer.Option(
            None, "--size", help="Backend size: tiny|small|medium|large."
        ),
        auth: str | None = typer.Option(
            None, "--auth", help="Authentication type: password|public."
        ),
        git_branch: str | None = typer.Option(
            None,
            "--git-branch",
            help="Retarget the external git repository's branch. Managed repos have no branch to set.",
        ),
        branch: int | None = typer.Option(
            None,
            "--branch",
            help="Storage branch ID for the linked config (defaults to production).",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show the Storage PUT body without making the call."
        ),
    ) -> None:
        """Change deployment settings on an existing data app.

        Read-modify-write on the linked Storage config: only the flags you pass
        are touched, everything else (secrets, encrypted git PAT, storage
        mapping) is preserved bit-identical. A request that already matches the
        stored config writes nothing and reports deploy_required=false.

        The command never auto-deploys -- per the redeploy contract the running
        container keeps its pinned configVersion until the next
        'kbagent data-app deploy'.

        Reference: https://help.keboola.com/data-apps/storage-access/
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "data_app_service")

        # Mirror the secrets-set precedent: catch "no field given" here so it
        # exits 2 (usage) rather than falling through to the service's generic
        # error exit 1.
        if all(value is None for value in (workspace, auto_suspend, size, auth, git_branch)):
            formatter.error(
                message=(
                    "Nothing to update. Pass at least one of "
                    "--workspace/--no-workspace, --auto-suspend, --size, --auth, "
                    "--git-branch."
                ),
                error_code=ErrorCode.MISSING_PARAMETER,
            )
            raise typer.Exit(code=2) from None

        try:
            result = service.update_data_app(
                alias=project,
                app_id=app_id,
                workspace=workspace,
                auto_suspend_after_seconds=auto_suspend,
                size=size,
                auth=auth,
                git_branch=git_branch,
                branch_id=branch,
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

        def _human(c: Console, d: dict) -> None:
            c.print(f"[bold green]Success:[/bold green] {d['message']}")
            for change in d.get("changes", []):
                c.print(
                    f"  [bold]{change['field']}:[/bold] {change['before']!r} -> "
                    f"[cyan]{change['after']!r}[/cyan]"
                )
            if d.get("changed") and "workspace" in d["changed"]:
                c.print(f"  [dim]Storage access reference: {_REF_STORAGE_ACCESS}[/dim]")

        formatter.output(result, _human)
        if not formatter.json_mode and result.get("next_step"):
            formatter.console.print(f"[dim]Next: {result['next_step']}[/dim]")
