"""`kbagent dev-portal` — Developer Portal command surface.

Identity management mirrors `kbagent project`; portal writes are gated by
`require_random_code_confirmation()` from _helpers — there is no `--yes`
bypass and no env-var override.
"""

from __future__ import annotations

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import DeveloperPortalIdentity
from ._helpers import (
    get_dev_portal_service,
    get_formatter,
    map_error_to_exit_code,
    resolve_identity_alias,
)

dev_portal_app = typer.Typer(
    help="Keboola Developer Portal — multi-identity, production-safe writes.",
    no_args_is_help=True,
)

identity_app = typer.Typer(help="Manage Developer Portal identities (login credentials).")
dev_portal_app.add_typer(identity_app, name="identity")


def _split_app(app: str) -> tuple[str, str]:
    """Split `VENDOR.APP_ID` into (vendor, app_id)."""
    if "." not in app:
        raise typer.BadParameter(
            f"--app must be in VENDOR.APP_ID form (e.g. keboola.ex-foo), got: {app!r}"
        )
    vendor, _ = app.split(".", 1)
    return vendor, app


# ----- Identity subcommands -----


@identity_app.command("add")
def identity_add(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--alias"),
    username: str = typer.Option(..., "--username"),
    password: str | None = typer.Option(None, "--password"),
    password_stdin: bool = typer.Option(
        False,
        "--password-stdin",
        help="Read password from stdin (paste from a secrets manager).",
    ),
    role_hint: str = typer.Option("vendor", "--role-hint"),
    vendor: str | None = typer.Option(None, "--vendor"),
    portal_url: str = typer.Option(
        "https://apps-api.keboola.com",
        "--portal-url",
    ),
) -> None:
    formatter = get_formatter(ctx)
    if password_stdin:
        import sys as _sys

        password = _sys.stdin.read().strip()
    if not password:
        raise typer.BadParameter("Pass --password or --password-stdin.")
    identity = DeveloperPortalIdentity(
        username=username,
        password=password,
        role_hint=role_hint,
        vendor=vendor,
        portal_url=portal_url,
    )
    svc = get_dev_portal_service(ctx)
    try:
        svc.add_identity(alias, identity)
    except (ConfigError, KeboolaApiError) as exc:
        formatter.error(
            message=str(exc),
            error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR),
        )
        raise typer.Exit(
            code=map_error_to_exit_code(exc) if isinstance(exc, KeboolaApiError) else 5
        ) from None
    formatter.output({"status": "ok", "alias": alias, "username": username})


@identity_app.command("list")
def identity_list(ctx: typer.Context) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    identities = svc.list_identities()
    default = svc.current_identity()
    rows = [
        {
            "alias": alias,
            "username": ident.username,
            "vendor": ident.vendor or "",
            "role_hint": ident.role_hint,
            "portal_url": ident.portal_url,
            "default": alias == default,
        }
        for alias, ident in identities.items()
    ]
    formatter.output(rows)


@identity_app.command("remove")
def identity_remove(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--alias"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    try:
        svc.remove_identity(alias)
    except ConfigError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output({"status": "ok", "removed": alias})


@identity_app.command("edit")
def identity_edit(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--alias"),
    username: str | None = typer.Option(None, "--username"),
    password: str | None = typer.Option(None, "--password"),
    password_stdin: bool = typer.Option(False, "--password-stdin"),
    role_hint: str | None = typer.Option(None, "--role-hint"),
    vendor: str | None = typer.Option(None, "--vendor"),
    new_alias: str | None = typer.Option(None, "--new-alias"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    if password_stdin:
        import sys as _sys

        password = _sys.stdin.read().strip()
    try:
        if new_alias:
            svc.rename_identity(alias, new_alias)
            alias = new_alias
        svc.edit_identity(
            alias,
            username=username,
            password=password,
            role_hint=role_hint,
            vendor=vendor,
        )
    except ConfigError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output({"status": "ok", "alias": alias})


@identity_app.command("use")
def identity_use(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Identity alias to set as default"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    try:
        svc.use_identity(alias)
    except ConfigError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output({"status": "ok", "default": alias})


@identity_app.command("current")
def identity_current(ctx: typer.Context) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    formatter.output({"default": svc.current_identity()})


@identity_app.command("verify")
def identity_verify(
    ctx: typer.Context,
    identity: str | None = typer.Option(None, "--identity"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    try:
        info = svc.verify_identity(alias)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", **info})


# ----- Read commands -----


@dev_portal_app.command("list")
def list_apps(
    ctx: typer.Context,
    vendor: str = typer.Option(..., "--vendor"),
    identity: str | None = typer.Option(None, "--identity"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    try:
        apps = svc.list_apps(alias, vendor)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(apps)


@dev_portal_app.command("get")
def get_app_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app", help="VENDOR.APP_ID, e.g. keboola.ex-foo"),
    identity: str | None = typer.Option(None, "--identity"),
) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        result = svc.get_app(alias, vendor, app_id)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(result)
