"""``data-app git-*`` commands (sandboxes-service ``/apps/{id}/git-repo/*``).

Split out of ``data_app.py`` to respect the file-size budget
(CONTRIBUTING.md "File-size budgets"). The commands attach to the existing
``data-app`` Typer sub-app via :func:`register_git_commands`, called at the
bottom of ``data_app.py`` -- so they still surface as
``kbagent data-app git-*`` and the permission gate
(``data-app.<subcommand>``) and CLI command-sync checks see them unchanged.

``git-repo`` needs only the project storage token; the credential pair
(git-credentials / git-credentials-create) needs an admin storage token and
targets *managed* git repos only. See ``references/gotchas.md`` for the
deploy-once precondition.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code

# sandboxes-service git-repo credential enums (API contract literals).
_GIT_CRED_TYPES = ("ssh_key", "http_token")
_GIT_CRED_PERMISSIONS = ("readOnly", "readWrite")


def register_git_commands(app: typer.Typer) -> None:
    """Attach the ``data-app git-*`` commands to the data-app sub-app."""

    @app.command("git-repo")
    def data_app_git_repo(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    ) -> None:
        """Show the clone URLs of a data app's configured git repository.

        Returns sshUrl / httpsUrl and whether the repo is *managed* by Keboola.
        Apps created with `data-app create --git-repo <url>` are *external*
        (not managed), so the git-credentials commands do not apply to them.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "data_app_git_service")
        try:
            result = service.get_data_app_git_repo(alias=project, app_id=app_id)
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
                c.print(
                    f"\n[bold]Git repository[/bold] (app {d['app_id']} in {d['project_alias']})"
                ),
                c.print(f"  [bold]Managed:[/bold] {d['is_managed_git_repo']}"),
                c.print(f"  [bold]SSH URL:[/bold] {d.get('ssh_url') or '-'}"),
                c.print(f"  [bold]HTTPS URL:[/bold] {d.get('https_url') or '-'}"),
            ),
        )

    @app.command("git-credentials")
    def data_app_git_credentials(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    ) -> None:
        """List the credentials of a data app's MANAGED git repository.

        Only applies to managed repos (the server returns 409 for external
        repos). The credential secret is never returned by this endpoint --
        it is shown once at create time only.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "data_app_git_service")
        try:
            result = service.list_data_app_git_credentials(alias=project, app_id=app_id)
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
            credentials = d.get("credentials", [])
            if not credentials:
                c.print("[dim]No git credentials configured.[/dim]")
                return
            c.print(
                f"\n[bold]{d['count']} credential(s)[/bold] "
                f"(app {d['app_id']} in {d['project_alias']})"
            )
            for cred in credentials:
                c.print(
                    f"  [bold]{cred['id']}[/bold] "
                    f"[cyan]{cred.get('type', '')}[/cyan] "
                    f"[magenta]{cred.get('permissions', '')}[/magenta] "
                    f"{cred.get('name', '') or '(no name)'} "
                    f"[dim]owner={cred.get('owner_admin_id', '')} · "
                    f"{cred.get('created_at', '')}[/dim]"
                )

        formatter.output(result, _human)

    @app.command("git-credentials-create")
    def data_app_git_credentials_create(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
        cred_type: str = typer.Option(
            ...,
            "--type",
            help="Credential type: ssh_key | http_token.",
        ),
        permissions: str = typer.Option(
            ...,
            "--permissions",
            help="Access level: readOnly | readWrite.",
        ),
        public_key: str | None = typer.Option(
            None,
            "--public-key",
            help=(
                "SSH public key inline (required for --type ssh_key; "
                "mutually exclusive with --public-key-file)."
            ),
        ),
        public_key_file: Path | None = typer.Option(
            None,
            "--public-key-file",
            help="Read the SSH public key from this file (required for --type ssh_key).",
        ),
        name: str | None = typer.Option(
            None,
            "--name",
            help="Optional display label for the credential.",
        ),
        yes: bool = typer.Option(
            False,
            "--yes",
            "-y",
            help="Skip the confirmation prompt.",
        ),
    ) -> None:
        """Create a git credential (SSH key or HTTP token) for a MANAGED repo.

        Only managed repos accept credentials (external repos return 409, and a
        non-admin storage token returns 403). For --type http_token the response
        includes a ONE-TIME secret that is shown once and cannot be retrieved
        again. For --type ssh_key you must supply a public key.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "data_app_git_service")

        if cred_type not in _GIT_CRED_TYPES:
            raise typer.BadParameter(
                f"--type must be one of {', '.join(_GIT_CRED_TYPES)} (got {cred_type!r}).",
                param_hint="--type",
            )
        if permissions not in _GIT_CRED_PERMISSIONS:
            raise typer.BadParameter(
                f"--permissions must be one of {', '.join(_GIT_CRED_PERMISSIONS)} "
                f"(got {permissions!r}).",
                param_hint="--permissions",
            )
        if public_key and public_key_file:
            raise typer.BadParameter(
                "--public-key and --public-key-file are mutually exclusive; pick one.",
                param_hint="--public-key",
            )

        resolved_public_key = public_key
        if public_key_file is not None:
            try:
                resolved_public_key = public_key_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise typer.BadParameter(
                    f"Cannot read public-key file {public_key_file}: {exc}",
                    param_hint="--public-key-file",
                ) from exc

        if cred_type == "ssh_key" and not resolved_public_key:
            raise typer.BadParameter(
                "--type ssh_key requires --public-key or --public-key-file.",
                param_hint="--public-key",
            )
        if cred_type == "http_token" and resolved_public_key:
            raise typer.BadParameter(
                "--type http_token must not be given a public key.",
                param_hint="--public-key",
            )

        if (
            not yes
            and not formatter.json_mode
            and not typer.confirm(
                f"Create a {cred_type} credential ({permissions}) for data app "
                f"{app_id} in '{project}'?"
            )
        ):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

        try:
            result = service.create_data_app_git_credential(
                alias=project,
                app_id=app_id,
                type_=cred_type,
                permissions=permissions,
                public_key=resolved_public_key,
                name=name,
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
            cred = d.get("credential", {})
            c.print(f"[bold green]Success:[/bold green] {d['message']}")
            c.print(f"  [bold]ID:[/bold] {cred.get('id', '')}")
            c.print(f"  [bold]Type:[/bold] {cred.get('type', '')}")
            c.print(f"  [bold]Permissions:[/bold] {cred.get('permissions', '')}")
            if cred.get("name"):
                c.print(f"  [bold]Name:[/bold] {cred['name']}")
            secret = cred.get("secret")
            if secret:
                c.print(
                    f"\n[bold yellow]One-time secret (won't be shown again):[/bold yellow] {secret}"
                )

        formatter.output(result, _human)
