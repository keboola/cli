"""`kbagent oauth` — OAuth broker maintenance (consumers, Entra secret rotation).

Thin CLI layer over ``services/oauth_service.py``. The safety model:

* the Manage token is an application token with ONLY the ``oauth:manage``
  scope, read once by a hidden prompt and kept in the macOS keychain (never in
  config.json, never an option or env var);
* every token read raises a macOS login-password dialog;
* every write shows its plan and needs a random code typed at a terminal --
  there is no ``--yes`` and no env bypass;
* secrets are never printed (``appSecret`` shows as ``<encrypted>``).

Terminal-only: no ``kbagent serve`` routes (the keychain dialog and the code
both need a human at this machine).
"""

from __future__ import annotations

import getpass
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape

from ..errors import ConfigError, ErrorCode, KeboolaApiError, exit_code_for
from ..output import OutputFormatter
from ..services._oauth_keychain import OAuthKeychain
from ..services.oauth_service import MAX_SECRET_DAYS, OAuthService, stack_hosts
from ._helpers import (
    assert_tty_for_write,
    check_cli_permission,
    emit_api_error,
    get_formatter,
    map_error_to_exit_code,
    require_random_code_confirmation,
)

oauth_app = typer.Typer(
    help=(
        "OAuth broker maintenance: stored oauth:manage identities, consumer reads, Entra "
        "secret rotation. Writes need a typed code at a terminal; no --yes."
    )
)
identity_app = typer.Typer(help="Manage the oauth:manage token stored in the macOS keychain.")
consumer_app = typer.Typer(help="Read OAuth broker consumers (secrets are never shown).")
oauth_app.add_typer(identity_app, name="identity")
oauth_app.add_typer(consumer_app, name="consumer")

STACK_HELP = "Stack host: connection.<stack>, oauth.<stack> or a URL of either"


@oauth_app.callback()
def _oauth_callback(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "oauth")


@identity_app.callback()
def _identity_callback(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "oauth.identity")


@consumer_app.callback()
def _consumer_callback(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "oauth.consumer")


def _service() -> OAuthService:
    return OAuthService(keychain=OAuthKeychain())


def _fail(formatter: OutputFormatter, exc: Exception) -> NoReturn:
    """Report a ConfigError / KeboolaApiError and exit with its code."""
    if isinstance(exc, KeboolaApiError):
        emit_api_error(formatter, exc)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    if isinstance(exc, ConfigError):
        code = getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)
        formatter.error(error_code=code, message=exc.message)
        raise typer.Exit(code=exit_code_for(code)) from None
    raise exc


# ----- identity -----


@identity_app.command("add")
def identity_add(
    ctx: typer.Context,
    stack: str = typer.Option(..., "--stack", help=STACK_HELP),
) -> None:
    """Store an oauth:manage application token for a stack in the macOS keychain.

    Prompts for the token without echo (a terminal is required; no pipe, option
    or env var). Refuses any token that is not an application token whose only
    scope is oauth:manage, and checks that the stack's OAuth broker accepts it.
    """
    assert_tty_for_write(f"store an OAuth token for {stack}")
    formatter = get_formatter(ctx)
    try:
        hosts = stack_hosts(stack)
    except KeboolaApiError as exc:
        _fail(formatter, exc)
    # The agent may have chosen --stack: show the human where the token will go.
    formatter.err_console.print(
        f"The token will be checked at {escape(hosts.connection_url)} and "
        f"{escape(hosts.oauth_url)}, then stored in the macOS keychain."
    )
    token = getpass.getpass(f"oauth:manage application token for {hosts.connection_url}: ").strip()
    if not token:
        raise typer.BadParameter("No token entered.", param_hint="--stack")
    try:
        result = _service().add_identity(stack, token)
    except (ConfigError, KeboolaApiError) as exc:
        _fail(formatter, exc)
    formatter.output(result, _render_identity)


@identity_app.command("verify")
def identity_verify(
    ctx: typer.Context,
    stack: str = typer.Option(..., "--stack", help=STACK_HELP),
) -> None:
    """Check the stored token again: keychain protection, scopes, broker access.

    macOS asks for the login password to release the token.
    """
    formatter = get_formatter(ctx)
    try:
        result = _service().verify_identity(stack)
    except (ConfigError, KeboolaApiError) as exc:
        _fail(formatter, exc)
    formatter.output(result, _render_identity)


def _render_identity(console: Console, data: dict[str, Any]) -> None:
    console.print(
        f"[bold green]{data['status'].capitalize()}[/bold green] {data['stack']}: token "
        f"{data.get('token_id')} ({data.get('description')}), scopes {data.get('scopes')}, "
        f"expires {data.get('expires') or 'never'}; broker lists {data.get('consumers')} consumers."
    )


# ----- consumer -----


@consumer_app.command("get")
def consumer_get(
    ctx: typer.Context,
    stack: str = typer.Option(..., "--stack", help=STACK_HELP),
    component: str = typer.Option(..., "--component", help="Component ID, e.g. keboola.ex-foo"),
) -> None:
    """Show one broker consumer. appSecret and appSecretDocker show as <encrypted>."""
    formatter = get_formatter(ctx)
    try:
        result = _service().get_consumer(stack, component)
    except (ConfigError, KeboolaApiError) as exc:
        _fail(formatter, exc)
    formatter.output(result, _render_consumer)


def _render_consumer(console: Console, data: dict[str, Any]) -> None:
    consumer = data["consumer"]
    console.print(f"[bold]{escape(str(consumer.get('componentId')))}[/bold] on {data['stack']}")
    for key, value in consumer.items():
        if key != "componentId":
            console.print(f"  {escape(str(key))}: {escape(str(value))}")


# ----- rotate-azure-secret -----


@oauth_app.command("rotate-azure-secret")
def rotate_azure_secret(
    ctx: typer.Context,
    stack: str = typer.Option(..., "--stack", help=STACK_HELP),
    component: str = typer.Option(..., "--component", help="Broker consumer (component ID)"),
    app_id: str = typer.Option(..., "--app-id", help="Entra application (client) ID"),
    op_vault: str = typer.Option(..., "--op-vault", help="1Password vault name or ID"),
    expires_in_days: int = typer.Option(
        MAX_SECRET_DAYS, "--expires-in-days", help="Lifetime of the new secret in days"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run every read-only check and show the plan; write nothing"
    ),
) -> None:
    """Rotate an Entra client secret behind one broker consumer.

    Adds a new secret to the app (named after the component), saves it in a new
    1Password item, PATCHes the broker consumer, then checks the broker and an
    Entra sign-in with the new secret. The old secret is NOT removed. One typed
    code confirms all writes. Needs `az` signed in with a role that may change
    the app's secrets, and `op` signed in.
    """
    if not dry_run:
        assert_tty_for_write(f"rotate the secret of {component}")
    formatter = get_formatter(ctx)
    service = _service()
    try:
        plan = service.prepare_rotation(stack, component, app_id, op_vault, expires_in_days)
    except (ConfigError, KeboolaApiError) as exc:
        _fail(formatter, exc)
    if dry_run:
        formatter.output({"status": "dry_run", **plan.as_dict()}, _render_plan)
        return
    _render_plan(formatter.err_console, plan.as_dict())
    require_random_code_confirmation(f"run these {len(plan.as_dict()['writes'])} writes")
    try:
        result = service.apply_rotation(plan)
    except KeboolaApiError as exc:
        _fail(formatter, exc)
    exit_code = 0 if result["status"] == "rotated" else 1
    formatter.output(result, _render_result, exit_code=exit_code)
    if exit_code:
        raise typer.Exit(code=exit_code)


def _render_plan(console: Console, data: dict[str, Any]) -> None:
    title = "DRY RUN -- planned writes" if data.get("status") == "dry_run" else "Planned writes"
    console.print(
        f"[bold yellow]{title}[/bold yellow] for {data['component_id']} on {data['stack']}"
    )
    console.print(f"  Entra app {data['app_id']} ({data['app_name']}), tenant {data['tenant_id']}")
    console.print(f"  az user {data['az_user']}")
    for number, write in enumerate(data["writes"], start=1):
        console.print(f"  {number}. [{write['target']}] {write['action']}")
    kept = ", ".join(f"{s['key_id']} (expires {s['expires']})" for s in data["old_secrets_kept"])
    console.print(f"  Old secrets for this component stay valid: {kept or 'none'}")
    if data["other_secrets_on_app"]:
        console.print(
            f"  The app has {data['other_secrets_on_app']} other secret(s) (other consumers); "
            "they are not touched."
        )


def _render_result(console: Console, data: dict[str, Any]) -> None:
    ok = data["status"] == "rotated"
    head = (
        "[bold green]Rotated[/bold green]" if ok else "[bold red]Rotated, NOT verified[/bold red]"
    )
    console.print(f"{head} {data['component_id']} on {data['stack']}")
    console.print(f"  New key ID {data['new_key_id']}, expires {data['new_secret_expires']}")
    console.print(f"  1Password item {data['op_item_id']} in vault {data['op_vault']}")
    console.print(f"  Broker secret changed: {data['broker_secret_changed']}")
    console.print(f"  Entra sign-in with the new secret: {data['entra_secret_check']['detail']}")
    old = ", ".join(data["old_secrets_still_valid"]) or "none"
    console.print(f"  Old secrets still valid (remove after a soak period): {old}")
