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
    check_cli_permission,
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


@dev_portal_app.callback()
def _dev_portal_callback(ctx: typer.Context) -> None:
    """Permission gate for `kbagent dev-portal …`."""
    check_cli_permission(ctx, "dev-portal")


@identity_app.callback()
def _identity_callback(ctx: typer.Context) -> None:
    """Permission gate for `kbagent dev-portal identity …`."""
    check_cli_permission(ctx, "dev-portal.identity")


def _split_app(app: str) -> tuple[str, str]:
    """Split `VENDOR.APP_ID` into (vendor, app_id)."""
    if "." not in app:
        raise typer.BadParameter(
            f"--app must be in VENDOR.APP_ID form (e.g. keboola.ex-foo), got: {app!r}"
        )
    vendor, _ = app.split(".", 1)
    return vendor, app


# ----- Identity subcommands -----


@identity_app.command(
    "add", help="Add a Developer Portal identity (verifies creds before persisting)."
)
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


@identity_app.command("list", help="List configured Developer Portal identities.")
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


@identity_app.command("remove", help="Remove a Developer Portal identity.")
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


@identity_app.command("edit", help="Edit fields on a Developer Portal identity (or rename it).")
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


@identity_app.command("use", help="Set the default Developer Portal identity.")
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


@identity_app.command("current", help="Show the alias of the default Developer Portal identity.")
def identity_current(ctx: typer.Context) -> None:
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    formatter.output({"default": svc.current_identity()})


@identity_app.command("verify", help="Probe a Developer Portal identity by logging in.")
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


@dev_portal_app.command("list", help="List Developer Portal apps for a vendor.")
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


@dev_portal_app.command("get", help="Show the full Developer Portal entry for one app.")
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


# ----- Write commands -----

import json  # noqa: E402
import sys as _sys  # noqa: E402
from dataclasses import asdict  # noqa: E402
from pathlib import Path  # noqa: E402

from ..constants import EXIT_PERMISSION_DENIED  # noqa: E402
from ._helpers import require_random_code_confirmation  # noqa: E402


def _assert_tty(action_description: str) -> None:
    """Refuse immediately on non-TTY; called before any payload I/O.

    This is the *first* guard in every write command so that CI/CD shells
    and AI agents are rejected before any file or stdin access happens.
    The full random-code prompt fires later (after the preview) on TTY.
    """
    is_tty = hasattr(_sys.stdin, "isatty") and _sys.stdin.isatty()
    if not is_tty:
        _sys.stderr.write(
            f"\nRefusing to {action_description}: this action requires a "
            "real terminal so a human can type the confirmation code. "
            "There is no --yes bypass by design.\n"
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED)


def _load_payload(data: str | None) -> dict:
    if data is None:
        raise typer.BadParameter("--data is required")
    if data == "-":
        import sys as _sys

        return json.loads(_sys.stdin.read())
    return json.loads(Path(data).read_text())


def _render_pending(formatter, pending) -> None:  # type: ignore[type-arg]
    """Write a stderr-only preview of the pending write."""
    from ..services.dev_portal_service import (
        PendingCreate,
        PendingDeprecate,
        PendingIconUpload,
        PendingPatch,
        PendingPublish,
    )

    err = formatter.err_console
    if isinstance(pending, PendingPatch):
        err.print(f"[bold]PATCH[/bold] /vendors/{pending.vendor}/apps/{pending.app_id}")
        for d in pending.diff:
            err.print(f"  [yellow]{d.key}[/yellow]: {d.current!r} -> {d.new!r}")
        if not pending.diff:
            err.print("  [dim]no field-level changes (payload matches current state)[/dim]")
    elif isinstance(pending, PendingCreate):
        err.print(f"[bold]POST[/bold] /vendors/{pending.vendor}/apps")
        err.print_json(json.dumps(pending.payload))
    elif isinstance(pending, PendingIconUpload):
        err.print(
            f"[bold]UPLOAD ICON[/bold] {pending.png_path} -> "
            f"{pending.vendor}/{pending.app_id} ({len(pending.png_bytes)} bytes)"
        )
    elif isinstance(pending, PendingPublish):
        err.print(
            f"[bold red]PUBLISH[/bold red] /vendors/{pending.vendor}/apps/"
            f"{pending.app_id}/publish (requests Keboola review)"
        )
    elif isinstance(pending, PendingDeprecate):
        err.print(
            f"[bold red]DEPRECATE[/bold red] /vendors/{pending.vendor}/apps/"
            f"{pending.app_id}/deprecate (hides app, blocks new configs)"
        )


def _pending_as_json(pending) -> dict:  # type: ignore[type-arg]
    """Serialise a pending write for --json --dry-run output."""
    raw = asdict(pending)
    if "png_bytes" in raw:
        raw["png_bytes"] = f"<{len(raw['png_bytes'])} bytes>"
    if "png_path" in raw:
        raw["png_path"] = str(raw["png_path"])
    return {"status": "dry-run", "pending": raw}


@dev_portal_app.command(
    "create",
    help="Create (register) a new app in the Developer Portal. Requires TTY confirm; --dry-run for preview.",
)
def create_cmd(
    ctx: typer.Context,
    vendor: str = typer.Option(..., "--vendor"),
    data: str = typer.Option(..., "--data", help="Path to JSON payload, or '-' for stdin"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    if not dry_run:
        _assert_tty(f"create app in vendor '{vendor}'")
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    try:
        pending = svc.prepare_create(alias, vendor, _load_payload(data))
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"create app in vendor '{vendor}'")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", "created": result})


@dev_portal_app.command(
    "patch",
    help="Patch one or more properties of an existing Developer Portal app. Requires TTY confirm; --dry-run for preview.",
)
def patch_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    data: str | None = typer.Option(None, "--data"),
    property_: str | None = typer.Option(None, "--property"),
    value: str | None = typer.Option(None, "--value"),
    value_file: str | None = typer.Option(None, "--value-file"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    if not dry_run:
        _assert_tty(f"patch {app}")
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)

    if data:
        payload = _load_payload(data)
    elif property_:
        if value_file:
            raw = Path(value_file).read_text()
        elif value is not None:
            raw = value
        else:
            raise typer.BadParameter("--property requires --value or --value-file")
        try:
            parsed = json.loads(raw) if raw.strip()[:1] in "[{" else raw
        except json.JSONDecodeError:
            parsed = raw
        payload = {property_: parsed}
    else:
        raise typer.BadParameter("Provide --data, or --property with --value/--value-file")

    try:
        pending = svc.prepare_patch(alias, vendor, app_id, payload)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"patch {app}")
    try:
        svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(
        {
            "status": "ok",
            "app": app,
            "patched_keys": [d.key for d in pending.diff],
        }
    )


@dev_portal_app.command(
    "upload-icon",
    help="Upload a 128x128 PNG icon for a Developer Portal app. Requires TTY confirm; --dry-run for preview.",
)
def upload_icon_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    file: str = typer.Option(..., "--file"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    if not dry_run:
        _assert_tty(f"upload icon for {app}")
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        pending = svc.prepare_upload_icon(alias, vendor, app_id, file)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"upload icon for {app}")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(result)


@dev_portal_app.command(
    "publish",
    help="Publish an app in the Developer Portal (requests Keboola review). Requires TTY confirm; --dry-run for preview.",
)
def publish_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    if not dry_run:
        _assert_tty(f"publish {app}")
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        pending = svc.prepare_publish(alias, vendor, app_id)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"publish {app}")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", "published": result})


@dev_portal_app.command(
    "deprecate",
    help="Deprecate an app in the Developer Portal (hides it, blocks new configs). Requires TTY confirm; --dry-run for preview.",
)
def deprecate_cmd(
    ctx: typer.Context,
    app: str = typer.Option(..., "--app"),
    identity: str | None = typer.Option(None, "--identity"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    if not dry_run:
        _assert_tty(f"deprecate {app}")
    formatter = get_formatter(ctx)
    svc = get_dev_portal_service(ctx)
    alias = resolve_identity_alias(ctx, identity)
    vendor, app_id = _split_app(app)
    try:
        pending = svc.prepare_deprecate(alias, vendor, app_id)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    _render_pending(formatter, pending)
    if dry_run:
        formatter.output(_pending_as_json(pending))
        return
    require_random_code_confirmation(f"deprecate {app}")
    try:
        result = svc.apply(pending)
    except KeboolaApiError as exc:
        formatter.error(message=str(exc), error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output({"status": "ok", "deprecated": result})
