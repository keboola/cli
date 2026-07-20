"""Data-app runtime-secrets commands -- secrets-set / -list / -get / -remove.

Split out of ``data_app.py`` to keep that module under the CONTRIBUTING.md
file-size budget (see "File-size budgets"). These commands manage the
``parameters.dataApp.secrets`` block of a data app's Storage config (the
app-runtime env-var secrets); the business logic stays on ``DataAppService``.
They attach to the existing ``data-app`` Typer sub-app via
:func:`register_secrets_commands`, called at the bottom of ``data_app.py``, so
they still surface as ``kbagent data-app secrets-*`` with identical names,
permission keys, and serve REST routes.

The module is deliberately NOT named ``_data_app_secrets.py``: the repo's
permission config denies Read/Write/Edit on any path matching ``*secrets*``.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code

# Canonical Keboola help-doc reference (mirrors data_app.py).
_REF_STORAGE_ACCESS = "https://help.keboola.com/data-apps/storage-access/"


def _parse_secret_arg(arg: str) -> tuple[str, str]:
    """Split ``#KEY=VALUE`` into ``(key, value)``.

    The value may contain ``=``; only the FIRST ``=`` is the separator.
    """
    if "=" not in arg:
        raise typer.BadParameter(
            f"Expected '#KEY=VALUE'; got {arg!r} (no '=' separator).",
            param_hint="--secret",
        )
    key, _, value = arg.partition("=")
    if not key:
        raise typer.BadParameter(
            f"Empty secret key in {arg!r}; expected '#KEY=VALUE'.",
            param_hint="--secret",
        )
    return key, value


def _read_secrets_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(
            f"Cannot read secrets file {path}: {exc}",
            param_hint="--secrets-file",
        ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"Secrets file {path} is not valid JSON: {exc}",
            param_hint="--secrets-file",
        ) from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(
            f"Secrets file {path} must be a JSON object mapping #KEY -> value.",
            param_hint="--secrets-file",
        )
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise typer.BadParameter(
                f"Secrets file {path} contains non-string entry for {key!r}.",
                param_hint="--secrets-file",
            )
        out[key] = value
    if not out:
        raise typer.BadParameter(
            f"Secrets file {path} is empty.",
            param_hint="--secrets-file",
        )
    return out


def data_app_secrets_set(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    secret: list[str] | None = typer.Option(
        None,
        "--secret",
        help=(
            "One or more '#KEY=VALUE' plaintext entries. Repeatable. "
            "Mutually exclusive with --secrets-file."
        ),
    ),
    secrets_file: Path | None = typer.Option(
        None,
        "--secrets-file",
        help="Path to a JSON file mapping '#KEY' -> 'plaintext value'.",
        exists=True,
        readable=True,
        dir_okay=False,
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Storage branch ID for the linked config (defaults to production).",
    ),
    allow_plaintext_on_encrypt_failure: bool = typer.Option(
        False,
        "--allow-plaintext-on-encrypt-failure",
        help=(
            "Bootstrap/debug only: write the value as-is if the Encryption API "
            "did not return a project-scoped ciphertext. NEVER use in production."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the encryption request and Storage PUT body without making either call.",
    ),
    no_hint_next: bool = typer.Option(
        False,
        "--no-hint-next",
        help="Suppress the 'now run kbagent data-app deploy' hint in the output.",
    ),
) -> None:
    """Encrypt and write app-runtime secrets to the linked Storage config.

    The '#'-prefix is required on every key (Keboola encryption convention).
    The runtime exposes each secret as an env var with '#' stripped, '-'
    replaced with '_', and uppercased ('#my-api-key' -> 'MY_API_KEY').

    The command never auto-deploys; the running container keeps the old
    config until the next 'kbagent data-app deploy' call.

    Reference: https://help.keboola.com/data-apps/python-js/
    """

    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")

    if secret and secrets_file:
        formatter.error(
            message=("--secret and --secrets-file are mutually exclusive; pick one input mode."),
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2) from None

    if not secret and not secrets_file:
        formatter.error(
            message=("Provide at least one --secret '#KEY=VALUE' or --secrets-file PATH."),
            error_code=ErrorCode.MISSING_PARAMETER,
        )
        raise typer.Exit(code=2) from None

    secrets_map: dict[str, str] = {}
    if secret:
        for entry in secret:
            try:
                key, value = _parse_secret_arg(entry)
            except typer.BadParameter as exc:
                formatter.error(
                    message=str(exc),
                    error_code=ErrorCode.DATA_APP_INVALID_SECRET,
                )
                raise typer.Exit(code=2) from None
            secrets_map[key] = value
    if secrets_file:
        try:
            secrets_map.update(_read_secrets_file(secrets_file))
        except typer.BadParameter as exc:
            formatter.error(
                message=str(exc),
                error_code=ErrorCode.DATA_APP_INVALID_SECRET,
            )
            raise typer.Exit(code=2) from None

    try:
        result = service.set_data_app_secrets(
            alias=project,
            app_id=app_id,
            secrets=secrets_map,
            branch_id=branch,
            allow_plaintext_on_encrypt_failure=allow_plaintext_on_encrypt_failure,
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

    # Reserved-name shadowing -- emit stderr WARN per collision so a
    # script piping stdout to a JSON parser is unaffected.
    shadowed = result.get("shadowed_by_runtime", [])
    if shadowed and not formatter.json_mode:
        for env_var in shadowed:
            formatter.err_console.print(
                f"[yellow]Warning:[/yellow] {env_var} is auto-injected by the data-app "
                f"runtime; the platform value silently shadows yours. See {_REF_STORAGE_ACCESS}.",
                style="yellow",
            )

    # Plaintext-on-encrypt-failure fallback -- name the secret key-paths written
    # in PLAINTEXT (keys only) so the leak is visible. JSON mode carries the same
    # list on the envelope via plaintext_written, so warn only in human mode.
    plaintext_written = result.get("plaintext_written")
    if plaintext_written and not formatter.json_mode:
        formatter.warning(
            f"{len(plaintext_written)} secret(s) were written in PLAINTEXT (encryption "
            f"failed and --allow-plaintext-on-encrypt-failure was set): "
            f"{', '.join(plaintext_written)}. Rotate these credentials and re-encrypt "
            f"once the Encryption API is reachable -- config version history retains the "
            f"plaintext copy."
        )

    if no_hint_next and isinstance(result, dict):
        result.pop("next_step", None)

    formatter.output(
        result,
        lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
    )
    if not no_hint_next and not formatter.json_mode and result.get("next_step"):
        formatter.console.print(f"[dim]Next: {result['next_step']}[/dim]")


def data_app_secrets_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Storage branch ID for the linked config (defaults to production).",
    ),
    show_fingerprint: bool = typer.Option(
        False,
        "--show-fingerprint",
        help="Include a short ciphertext fingerprint per key. Default omits to keep --json safe to paste into tickets.",
    ),
) -> None:
    """List the keys in parameters.dataApp.secrets, with derived runtime env-var names.

    Never echoes the encrypted ciphertext in full and never decrypts.

    Reference: https://help.keboola.com/data-apps/python-js/
    """

    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    try:
        result = service.list_data_app_secrets(
            alias=project,
            app_id=app_id,
            branch_id=branch,
            show_fingerprint=show_fingerprint,
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

    if formatter.json_mode:
        formatter.output(result)
        return

    if not result["secrets"]:
        formatter.console.print("[dim]No secrets set on this data app.[/dim]")
        return
    formatter.console.print(
        f"\n[bold]{result['count']} secret(s)[/bold] on data app "
        f"[cyan]{result['app_id']}[/cyan] in [magenta]{result['project_alias']}[/magenta]:"
    )
    for entry in result["secrets"]:
        marker = (
            " [yellow](shadowed by runtime)[/yellow]" if entry.get("shadowed_by_runtime") else ""
        )
        line = f"  [bold]{entry['key']}[/bold] -> env [cyan]{entry['env_var']}[/cyan]{marker}"
        if "fingerprint" in entry:
            line += f"  [dim]fingerprint={entry['fingerprint']}  prefix={entry.get('encryption_prefix', '')}[/dim]"
        formatter.console.print(line)


def data_app_secrets_get(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    key: str = typer.Option(
        ..., "--key", help="Env-var key (with optional '#' prefix for encrypted secrets)."
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Storage branch ID for the linked config (defaults to production).",
    ),
) -> None:
    """Show ONE key from parameters.dataApp.secrets.

    For an ENCRYPTED ('#') secret this is metadata only -- the Encryption
    API has no decrypt endpoint, so the CLI never echoes the decrypted
    value. For a PLAIN (unencrypted) config value the literal value is
    shown; it is already stored in clear and visible via `config detail`.

    Reference: https://help.keboola.com/data-apps/python-js/
    """

    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")
    try:
        result = service.get_data_app_secret(
            alias=project,
            app_id=app_id,
            key=key,
            branch_id=branch,
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

    if formatter.json_mode:
        formatter.output(result)
        return
    formatter.console.print(
        f"\n[bold]{result['key']}[/bold] -> env [cyan]{result['env_var']}[/cyan]"
    )
    if result.get("encrypted"):
        formatter.console.print(
            f"  [dim]fingerprint={result['fingerprint']}  prefix={result['encryption_prefix']}[/dim]"
        )
    else:
        formatter.console.print(f"  value (plaintext, unencrypted): {result['value']}")
        formatter.err_console.print(
            "  [yellow]Note:[/yellow] this value is stored unencrypted in the config. "
            "Use `data-app secrets-set '#KEY=...'` to store sensitive values encrypted."
        )
    if result.get("shadowed_by_runtime"):
        # Same stdout/stderr-separation rationale as secrets-set: keep
        # warnings off stdout so a script piping the metadata to a parser
        # is unaffected.
        formatter.err_console.print(
            f"  [yellow]Warning:[/yellow] {result['env_var']} is auto-injected by "
            f"the data-app runtime; the platform value silently shadows yours. "
            f"See {_REF_STORAGE_ACCESS}."
        )


def data_app_secrets_remove(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
    key: list[str] = typer.Option(
        ...,
        "--key",
        help="Env-var key to remove (with optional '#' prefix). Repeatable.",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Storage branch ID for the linked config (defaults to production).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the Storage PUT body without making the call."
    ),
) -> None:
    """Remove one or more app-runtime secrets. Idempotent (missing keys are exit 0).

    A removal can break the running app at the next deploy if it relied on
    the secret; the command flags this in the response and never auto-deploys.

    Reference: https://help.keboola.com/data-apps/python-js/
    """

    formatter = get_formatter(ctx)
    service = get_service(ctx, "data_app_service")

    if (
        not yes
        and not formatter.json_mode
        and not dry_run
        and not typer.confirm(
            f"Remove {len(key)} secret(s) from data app {app_id} in '{project}'? "
            "This may break the app at next deploy if it depends on these values."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.remove_data_app_secrets(
            alias=project,
            app_id=app_id,
            keys=key,
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

    formatter.output(
        result,
        lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
    )


def register_secrets_commands(app: typer.Typer) -> None:
    """Attach the data-app secrets-* commands to the data-app sub-app.

    Module-level command functions registered explicitly (rather than via
    inline ``@app.command`` closures) keep the large bodies un-indented and
    diff-friendly relative to their previous home in ``data_app.py``.
    """
    app.command("secrets-set")(data_app_secrets_set)
    app.command("secrets-list")(data_app_secrets_list)
    app.command("secrets-get")(data_app_secrets_get)
    app.command("secrets-remove")(data_app_secrets_remove)
