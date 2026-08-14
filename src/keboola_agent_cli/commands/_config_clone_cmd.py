"""``kbagent config clone`` -- whole-configuration duplicate (issue #587).

Thin CLI layer over :meth:`services.config_service.ConfigService.clone_config`.

Lives in a private module because ``commands/config.py`` is already past its
grandfathered size ceiling (``make loc-check``: "shrink it, do not extend
it"). Mounted onto ``config_app`` via :func:`register`, so the permission key
stays ``config.clone`` and the command shows up in ``kbagent config --help``
alongside the other lifecycle commands.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code


def _parse_pair_options(items: list[str] | None, flag: str, formatter: Any) -> dict[str, str]:
    """Parse repeatable ``--flag PATH=VALUE`` options into a dict.

    Splits on the FIRST ``=`` only, so a value may itself contain ``=``
    (base64 padding and connection strings routinely do).
    """
    parsed: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            formatter.error(
                message=f"Invalid {flag} format: '{item}'. Expected PATH=VALUE.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2) from None
        path, _, value = item.partition("=")
        parsed[path.strip()] = value
    return parsed


def _format_clone_result(console: Console, data: dict) -> None:
    """Render the clone result for humans."""
    mode = data.get("mode", "same-project")
    if data.get("dry_run"):
        console.print(
            f"[bold yellow]DRY RUN[/bold yellow] -- {escape(mode)} clone of "
            f"[cyan]{escape(str(data.get('source_config_id')))}[/cyan] "
            f"(version {data.get('source_version')})"
        )
        console.print(f"  New name : {escape(str(data.get('name')))}")
        console.print(f"  Target   : {escape(str(data.get('target_project')))}")
        console.print(f"  Rows     : {data.get('row_count', 0)}")
        missing = data.get("missing_secrets") or []
        if missing:
            console.print(
                f"\n[bold red]{len(missing)} encrypted value(s) must be re-supplied[/bold red] "
                "-- no other project can decrypt them:"
            )
            for path in missing:
                console.print(f"  [red]-[/red] {escape(path)}")
            console.print("\n[dim]Pass each one as --secret 'PATH=VALUE'.[/dim]")
        return

    console.print(
        f"[bold green]Cloned[/bold green] -> config id [cyan]{escape(str(data.get('id')))}[/cyan] "
        f"({escape(mode)}, project {escape(str(data.get('target_project')))})"
    )
    rows = data.get("copied_rows") or []
    if rows:
        console.print(f"  Copied {len(rows)} row(s)")
    if mode == "cross-project":
        console.print(
            "\n[dim]Note: storage input/output mappings were copied verbatim. "
            "Bucket and table IDs are NOT remapped -- check they exist in the "
            "target project (`kbagent sync clone` handles remapping).[/dim]"
        )


def register(app: typer.Typer) -> None:
    """Mount the clone command onto ``app`` (the ``config`` Typer group)."""

    @app.command("clone", rich_help_panel="Lifecycle")
    def config_clone(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Source project alias"),
        component_id: str = typer.Option(
            ..., "--component-id", help="Component ID (e.g. keboola.wr-db-snowflake)"
        ),
        config_id: str = typer.Option(..., "--config-id", help="Configuration ID to clone"),
        name: str = typer.Option(..., "--name", help="Name for the new configuration"),
        target_project: str | None = typer.Option(
            None,
            "--target-project",
            help="Clone into a different project (default: same project)",
        ),
        description: str = typer.Option(
            "", "--description", help="Description for the clone (default: inherit the source's)"
        ),
        set_values: list[str] | None = typer.Option(
            None,
            "--set",
            help="Override a value in the clone: PATH=VALUE (repeatable)",
        ),
        secret_values: list[str] | None = typer.Option(
            None,
            "--secret",
            help=(
                "Re-supply an encrypted value for a cross-project clone: PATH=VALUE "
                "(repeatable). Encrypted in the TARGET project on write."
            ),
        ),
        branch: int | None = typer.Option(
            None, "--branch", help="Source dev branch (defaults to the active branch)"
        ),
        target_branch: int | None = typer.Option(
            None,
            "--target-branch",
            help="Target dev branch (defaults to the target's active branch)",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show the plan (and any missing secrets) without writing"
        ),
        allow_plaintext: bool = typer.Option(
            False,
            "--allow-plaintext-on-encrypt-failure",
            help="Allow the clone even if secret encryption fails (DANGEROUS: plaintext secrets)",
        ),
    ) -> None:
        """Duplicate a configuration, whole -- including runtime, storage and authorization.

        \b
        Rebuilding a configuration body by hand drops sibling keys of `parameters`
        silently. A lost `runtime.parallelism` makes Keboola fall back to
        parallelism 1, which is invisible until you compare job timestamps.
        Cloning copies everything instead.

        \b
        Within one project the Storage API copies server-side, so rows and
        encrypted values come along untouched. Across projects the configuration
        is reassembled here, and any encrypted (`KBC::`) value must be re-supplied
        with --secret: ciphertext belongs to the project it was encrypted in and
        no other project can decrypt it. Run with --dry-run first to list exactly
        which paths need one.

        \b
        Examples:
          # Duplicate inside a project and point the copy at new tables
          kbagent config clone --project prod --component-id keboola.wr-db-snowflake \\
            --config-id 123 --name "Writer (staging tables)" \\
            --set 'parameters.db.schema=STAGING'

          # See what a cross-project clone would need, without writing
          kbagent config clone --project prod --component-id keboola.ex-db-mysql \\
            --config-id 123 --name "Copy" --target-project dev --dry-run

          # Cross-project clone, re-supplying the credential
          kbagent config clone --project prod --component-id keboola.ex-db-mysql \\
            --config-id 123 --name "Copy" --target-project dev \\
            --secret 'parameters.db.#password=hunter2'
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "config_service")

        set_overrides = _parse_pair_options(set_values, "--set", formatter)
        secret_overrides = _parse_pair_options(secret_values, "--secret", formatter)

        try:
            result = service.clone_config(
                alias=project,
                component_id=component_id,
                config_id=config_id,
                name=name,
                description=description,
                target_alias=target_project,
                set_overrides=set_overrides,
                secret_overrides=secret_overrides,
                branch_id=branch,
                target_branch_id=target_branch,
                dry_run=dry_run,
                allow_plaintext_fallback=allow_plaintext,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(
                message=exc.message,
                error_code=exc.error_code,
                project=project,
                retryable=exc.retryable,
            )
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        formatter.output(result, _format_clone_result)
