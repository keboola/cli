"""Shared command-layer helpers to eliminate duplication across command files.

Provides common patterns used by all CLI commands:
- Context extraction (formatter, services)
- Exit code mapping for API errors
- Warning emission for multi-project operations
- Branch resolution for --branch flag
"""

import getpass
import json
import os
import secrets
import sys
from collections.abc import Callable, Iterable, Sized
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import yaml

from .. import telemetry
from ..config_store import ConfigStore
from ..constants import (
    ENV_KBC_MANAGE_API_TOKEN,
    ENV_KBC_TOKEN,
    EXIT_PERMISSION_DENIED,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError, PermissionDeniedError, exit_code_for
from ..output import OutputFormatter
from ..permissions import FLAG_ESCALATIONS
from ..update_crossing import print_held_warning, refusal_for


def resolve_manage_token(*, allow_env: bool = False) -> str:
    """Resolve the manage token from a permitted source.

    Default-deny: KBC_MANAGE_API_TOKEN is ignored unless ``allow_env=True``
    (set by the top-level ``--allow-env-manage-token`` flag). The change
    closes the AI-exfiltration risk where any subprocess running as the
    same user can read the env var; the new default is "human at a TTY".

    Resolution order:
    1. ``KBC_MANAGE_API_TOKEN`` env var, IF ``allow_env`` is True. Otherwise
       a one-shot stderr warning is emitted and the env var is ignored.
    2. Interactive prompt with hidden input (if stdin is a TTY).
    3. Exit 2 with an actionable error naming the opt-in flag.

    Args:
        allow_env: When True, restores the legacy env-var-first behaviour
            for the current invocation. Plumbed from the top-level
            ``--allow-env-manage-token`` flag via ``ctx.obj``.

    Returns:
        The manage API token.

    Raises:
        typer.Exit: If no token can be resolved (exit code 2).
    """
    env_token = os.environ.get(ENV_KBC_MANAGE_API_TOKEN)
    if env_token:
        if allow_env:
            return env_token
        typer.echo(
            f"Warning: {ENV_KBC_MANAGE_API_TOKEN} found in environment "
            "but ignored. Pass --allow-env-manage-token to opt in.",
            err=True,
        )

    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if is_tty:
        return typer.prompt("Manage API token", hide_input=True)

    typer.echo(
        "Error: No manage token available. Run interactively, or pass "
        f"--allow-env-manage-token to read {ENV_KBC_MANAGE_API_TOKEN} "
        "from env.",
        err=True,
    )
    raise typer.Exit(code=2)


def parse_json_arg(raw: str, *, label: str, allow_yaml: bool = False) -> Any:
    """Parse a ``JSON|@file|-`` argument: inline JSON, ``@path``, or ``-`` for stdin.

    The house input contract for structured flags (spec rule I1): ``config
    update --configuration``, ``transformation edit --op``, ``merge-request
    resolve --resolved``, ``encrypt values --input``, ``http post --body`` and
    the others. ``allow_yaml`` parses the text as YAML, a superset of JSON
    (``flow new/update/validate --file``).

    Raises:
        ValueError: On a missing/unreadable file or malformed JSON -- the message
            names ``label`` (the flag) so the caller can print it at exit 2 as-is.
    """
    loads: Callable[[str], Any] = yaml.safe_load if allow_yaml else json.loads
    try:
        if raw == "-":
            return loads(sys.stdin.read())
        if raw.startswith("@"):
            file_path = Path(raw[1:])
            if not file_path.is_file():
                raise ValueError(f"{label}: file not found: {file_path}")
            return loads(file_path.read_text(encoding="utf-8"))
        return loads(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label}: invalid {'YAML' if allow_yaml else 'JSON'}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        # a directory, a permission problem, a binary file -- a usage error, not a crash
        raise ValueError(f"{label}: cannot read input: {exc}") from exc


def split_multi(values: Iterable[str] | None) -> list[str]:
    """Flatten a repeatable option that also takes a comma list (spec rule A4).

    ``--x a --x b,c`` gives ``["a", "b", "c"]``. Empty parts are dropped, and a
    value given twice is kept once, in first-seen order. Use it only for IDs
    and fixed values: a free-form value (JSON, ``KEY=VALUE``, SQL, text) can
    contain a comma, so those options are repeat-only and stay unsplit.
    """
    parts = (part.strip() for value in values or () for part in value.split(","))
    return list(dict.fromkeys(part for part in parts if part))


def split_int_multi(values: Iterable[str] | None, *, label: str) -> list[int]:
    """``split_multi`` for integer IDs; a part that is not an integer raises ``ValueError``."""
    parts = split_multi(values)
    bad = [part for part in parts if not (part.isascii() and part.isdigit())]
    if bad:
        raise ValueError(f"{label} takes integer IDs, got: {', '.join(bad)}")
    return [int(part) for part in parts]


def read_text_arg(formatter: OutputFormatter, path: Path, *, label: str) -> str:
    """Read a UTF-8 file option; an unreadable file is ``INVALID_ARGUMENT`` naming ``label``."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        formatter.error(
            message=f"{label}: cannot read file {path}: {exc}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None


def _is_file(path: str) -> bool:
    """``Path.is_file`` that answers False where it would raise (a name too long)."""
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


class RefuseAtFile:
    """Option callback for free text that refuses ``@<existing file>`` (spec rule I3).

    Without it, ``--text @notes.md`` stores the literal text ``@notes.md``.
    The refusal is ``INVALID_ARGUMENT`` (exit 2) with a hint that names the
    file option of the command, also for the case where the text itself starts
    with ``@``. ``@`` followed by anything that is not a file (a relative path
    is relative to the current directory) stays text. Use it as
    ``typer.Option(..., callback=RefuseAtFile("--file").callback)``;
    ``scripts/check_cli_spec.py`` finds the free-text options without it.
    """

    def __init__(self, file_option: str) -> None:
        self.file_option = file_option

    def callback(
        self, ctx: typer.Context, param: typer.CallbackParam, value: str | None
    ) -> str | None:
        if value is not None and value.startswith("@") and _is_file(value[1:]):
            get_formatter(ctx).error(
                message=(
                    f"{param.opts[0]} got {value!r}, the name of an existing file (relative "
                    f"to the current directory). Use {self.file_option} {value[1:]} to read "
                    f"the file. To store the text {value!r} itself, write it to a file and "
                    f"pass that file with {self.file_option}."
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2)
        return value


def read_password_stdin(label: str = "Password: ") -> str:
    """Read a secret from stdin.

    TTY -> getpass (hidden, line-based, Enter to confirm).
    Pipe/redirected -> read to EOF, strip whitespace.
    Using `sys.stdin.read()` unconditionally would hang interactively
    until the user sent EOF (Ctrl-D); getpass on TTY fixes that.

    Shared by `auth login-password --password-stdin`, `dev-portal identity
    add/edit --password-stdin` and `project add/edit --token-stdin` -- the
    same input contract, so one helper rather than a private copy per
    command module. ``label`` is the prompt shown on a TTY.
    """
    if sys.stdin.isatty():
        return getpass.getpass(label).strip()
    return sys.stdin.read().strip()


def _read_token_file(path: Path) -> str:
    """Read a Storage API token out of a file, and check the file's mode."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise typer.BadParameter(
            f"Cannot read token file {path}: {exc}",
            param_hint="--token-file",
        ) from exc
    if not token:
        raise typer.BadParameter(f"Token file {path} is empty.", param_hint="--token-file")
    # Windows reports fabricated POSIX bits (an ordinary file reads as ~0o666),
    # so the mode check would warn on every use. Skip it there, as
    # auth/state_store.py and services/doctor_service.py already do.
    if os.name != "nt":
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:  # pragma: no cover - the read above already succeeded
            mode = 0
        if mode & 0o077:
            typer.echo(
                f"Warning: {path} is accessible to other users (mode {mode:04o}). "
                "A token file should be 0600.",
                err=True,
            )
    return token


def _require_single_line(token: str, param_hint: str) -> str:
    """Refuse a token that holds a line break or another control character.

    A Storage token is one printable line. When the value is several lines --
    a whole file or ``.env`` piped in by mistake -- the HTTP layer fails with
    an error that quotes the entire header value ("Illegal header value b'...'"),
    and nothing catches it, so the traceback would print the token and
    everything piped in with it. Refusing here keeps the value out of that
    message: the error names the flag, never the value. An empty or blank
    value is refused too, so an unset variable piped in does not reach the API.
    """
    if not token.strip():
        raise typer.BadParameter("The token is empty.", param_hint=param_hint)
    if not token.isprintable():
        raise typer.BadParameter(
            "The token contains a line break or another control character. "
            "Pass the token alone, on one line.",
            param_hint=param_hint,
        )
    return token


# Shared declarations for the token-input flags. `project add` and `project
# edit` offer the same set, and one definition keeps their help text from
# drifting apart -- `project.py` is already over its soft size ceiling.
TOKEN_STDIN_OPTION = typer.Option(
    False,
    "--token-stdin",
    help="Read the token from stdin instead of --token. On a TTY this is a hidden "
    "prompt (Enter to confirm); on a pipe it reads until EOF (e.g. "
    "`printf '%s' \"$TOKEN\" | kbagent project edit --project P --token-stdin`). "
    "The token stays out of your shell history and out of a process listing.",
)

TOKEN_FILE_OPTION = typer.Option(
    None,
    "--token-file",
    help="Read the token from this file. kbagent deletes the file after the command "
    "succeeds -- pass --keep-token-file to keep it. Deleting a file removes the "
    "directory entry, not the bytes on disk. Give the file mode 0600.",
    exists=True,
    readable=True,
    dir_okay=False,
)

TOKEN_ENV_OPTION = typer.Option(
    None,
    "--token-env",
    help="Read the token from the environment variable with this name, e.g. "
    "`--token-env CI_KBC_TOKEN`. kbagent reads no variable unless you name it. "
    "Use this in CI, where the runner sets the variable from a secret.",
)

KEEP_TOKEN_FILE_OPTION = typer.Option(
    False,
    "--keep-token-file",
    help="Keep the --token-file file instead of deleting it. Use this for a "
    "read-only mount, or for a file you read more than once.",
)


def token_came_from_command_line(ctx: typer.Context, param_name: str = "token") -> bool:
    """Report whether a parameter's value was typed on the command line.

    `--token` and its `envvar=` reach the command in one parameter, and only
    the typed one is in the shell history, so the warning has to tell them
    apart. Click records the origin, but Typer bundles its own copy of Click
    (`typer._click`), so the `ParameterSource` enum a running context returns
    is a different class from `click.core.ParameterSource`. Comparing the two
    with `==` is always False, which switches the warning off without a word.
    Match on the member name, which is the same in both copies.

    An unknown origin counts as the command line: a missing warning defeats
    the point of the flag, while a spare one costs the reader a line.
    """
    source = ctx.get_parameter_source(param_name)
    return source is None or source.name == "COMMANDLINE"


def resolve_storage_token_input(
    *,
    token: str | None,
    token_stdin: bool = False,
    token_file: Path | None = None,
    token_env: str | None = None,
    keep_token_file: bool = False,
    required: bool,
    token_from_cli: bool = True,
) -> str | None:
    """Resolve a Storage API token from exactly one permitted source.

    The point of every source other than ``--token`` is that the value
    never reaches the command line, where it lands in the shell history,
    in the kbagent REPL history file, and in a process listing.

    Resolution order -- the four explicit sources are mutually exclusive:

    1. ``--token-stdin`` -- hidden prompt on a TTY, read to EOF on a pipe.
    2. ``--token-file`` -- read the file. Call ``discard_token_file()``
       after the command succeeds to delete it.
    3. ``--token-env`` -- read the named environment variable.
    4. ``token`` -- the ``--token`` value. For ``project add`` this also
       carries ``KBC_TOKEN``, because Typer merges the flag and the envvar
       into one parameter. Such a value is implicit, so it does not count
       as an explicit source and it gets no shell-history warning; the
       caller reports the difference through ``token_from_cli``.
    5. Nothing given -- prompt on a TTY when ``required`` is True, and
       exit 2 when there is no TTY. A caller whose token is optional
       (``project edit``) passes ``required=False`` and gets ``None``.

    Args:
        token: Value of ``--token``, or of the envvar the caller declares.
        token_stdin: ``--token-stdin`` was passed.
        token_file: Path from ``--token-file``.
        token_env: Variable name from ``--token-env``.
        keep_token_file: ``--keep-token-file`` was passed. Validated here
            because it is meaningless without ``--token-file``; the flag
            itself is acted on by ``discard_token_file()``.
        required: The command cannot continue without a token.
        token_from_cli: ``token`` came from the command line, not from the
            environment. Keeps the shell-history warning truthful.

    Returns:
        The token, or ``None`` when nothing was given and ``required`` is
        False.

    Raises:
        typer.BadParameter: More than one explicit source, an unreadable or
            empty file, an unset or empty named variable, an empty or blank
            value from any source (``--token ""`` included), a value with a
            line break or another control character, or ``--keep-token-file``
            without ``--token-file``.
        typer.Exit: ``required`` is True and there is nothing to read
            (exit code 2).
    """
    if keep_token_file and token_file is None:
        raise typer.BadParameter(
            "--keep-token-file applies only together with --token-file.",
            param_hint="--keep-token-file",
        )

    given = [
        flag
        for flag, present in (
            ("--token", token is not None and token_from_cli),
            ("--token-stdin", token_stdin),
            ("--token-file", token_file is not None),
            ("--token-env", token_env is not None),
        )
        if present
    ]
    if len(given) > 1:
        raise typer.BadParameter(
            f"Specify exactly one of {' / '.join(given)}.",
            param_hint=given[0],
        )

    if token_stdin:
        return _require_single_line(
            read_password_stdin(label="Storage API token: "), "--token-stdin"
        )

    if token_file is not None:
        return _require_single_line(_read_token_file(token_file), "--token-file")

    if token_env is not None:
        value = os.environ.get(token_env, "")
        if not value:
            raise typer.BadParameter(
                f"Environment variable {token_env} is unset or empty.",
                param_hint="--token-env",
            )
        return _require_single_line(value, "--token-env")

    if token is not None:
        _require_single_line(token, "--token")
        if token_from_cli and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            typer.echo(
                "Warning: the token you passed with --token is now in your shell "
                "history. Delete that history entry, or invalidate the token. "
                "Use --token-stdin to keep the next one out of the history.",
                err=True,
            )
        return token

    if not required:
        return None

    if hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        return typer.prompt("Storage API token", hide_input=True)

    typer.echo(
        "Error: No token available. Pass --token-stdin, --token-file, "
        f"--token-env, or --token; set {ENV_KBC_TOKEN}; or run interactively.",
        err=True,
    )
    raise typer.Exit(code=2)


def discard_token_file(path: Path | None, *, keep: bool = False, dry_run: bool = False) -> None:
    """Delete the ``--token-file`` after the command that read it succeeded.

    The file carries the token to kbagent; it is not a store. Deleting it
    at read time would cost the user the file every time the token failed
    verification, and deleting it during ``--dry-run`` would make a preview
    destructive. So the caller invokes this only on the success path.

    A failed unlink is a warning, not an error. A read-only mount (a
    Kubernetes secret, a CI volume) is a legitimate setup, and the command
    has already done its work by this point.

    ``unlink`` removes the directory entry; it does not scrub the bytes. On
    a copy-on-write filesystem or an SSD the content can survive.
    """
    if path is None:
        return
    if dry_run:
        typer.echo(f"Note: {path} was kept (--dry-run). It still holds the token.", err=True)
        return
    if keep:
        typer.echo(
            f"Warning: {path} still holds the token (--keep-token-file). Delete it yourself.",
            err=True,
        )
        return
    try:
        path.unlink()
    except OSError as exc:
        typer.echo(
            f"Warning: could not delete {path} ({exc}). It still holds the token.",
            err=True,
        )
        return
    typer.echo(f"Deleted the token file {path}.", err=True)


def get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def get_service(ctx: typer.Context, key: str) -> Any:
    """Retrieve a service from the Typer context."""
    return ctx.obj[key]


def map_error_to_exit_code(exc: KeboolaApiError) -> int:
    """Map a KeboolaApiError to a CLI exit code.

    The exit code comes from ``ERROR_CODE_TABLE`` in ``errors.py`` (spec rule
    E1), and ``docs/error-codes.md`` lists it per code. A code that is not an
    ``ErrorCode`` member exits 1, like ``UNKNOWN_ERROR``.
    """
    telemetry.note_command_error(str(exc))
    return exit_code_for(exc.error_code)


def emit_api_error(formatter: OutputFormatter, exc: KeboolaApiError, *, project: str = "") -> None:
    """Print a KeboolaApiError with its code, message, ``retryable`` and ``details``.

    The one way a command handler reports an API error. A handler that passes
    only the code and the message prints ``retryable: false`` for a retryable
    error, and the agent context tells agents to retry on ``retryable``.
    ``tests/test_error_forwarding.py`` fails on a handler that drops them.
    """
    formatter.error(
        error_code=exc.error_code,
        message=exc.message,
        project=project,
        retryable=exc.retryable,
        details=exc.details,
    )


def service_error_exit_code(exc: ConfigError | KeboolaApiError) -> int:
    """The CLI exit code for a service error: config 5, a bad option value 2, else by error code."""
    if isinstance(exc, ConfigError):
        return 5
    if exc.error_code == ErrorCode.INVALID_ARGUMENT:
        return 2
    return map_error_to_exit_code(exc)


def handle_service_call[T](
    ctx: typer.Context, func: Callable[..., T], *args: Any, **kwargs: Any
) -> T:
    """Run a service call; a ``ConfigError`` / ``KeboolaApiError`` becomes a formatted error + exit."""
    formatter = get_formatter(ctx)
    try:
        return func(*args, **kwargs)
    except ConfigError as exc:
        formatter.error(
            message=exc.message,
            error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR),
        )
        raise typer.Exit(code=service_error_exit_code(exc)) from None
    except KeboolaApiError as exc:
        emit_api_error(formatter, exc)
        raise typer.Exit(code=service_error_exit_code(exc)) from None


def item_failure_exit_code(failed: int) -> int | None:
    """Map the number of failed items of a multi-item operation to a CLI exit code.

    Commands that keep going after one item fails (a config in ``sync push``,
    a project in ``org setup``, a row in ``project invite --from-csv``) used to
    collect those failures into the result and still exit 0 -- so a run where
    every item failed was indistinguishable from a clean one by exit code
    alone, and a script could not tell success from total failure (issue #745).

    Returns ``None`` when ``failed`` is 0, else 1: the convention already used
    by the bulk storage commands (``storage delete-table``, ``file-tag``,
    ``describe-migrate``). Like :func:`map_error_to_exit_code` it only maps;
    the caller raises ``typer.Exit`` AFTER the result has been emitted, so
    JSON callers still receive the full payload with the per-item errors. A
    ``--dry-run`` that reports a failed item exits 1 like the real run.
    """
    if not failed:
        return None
    telemetry.note_command_error(f"{failed} item(s) failed")
    return 1


def finish_items(
    formatter: OutputFormatter,
    result: Any,
    *,
    failed: int,
    total: int,
    noun: str,
    summary: str,
    render: Callable[[], object] | None = None,
) -> None:
    """End a command that processes several items: output, one headline, exit code.

    The one way to end such a command (HO2, E4): ``--json`` gets the envelope
    with ``exit_code``; human mode gets ``render()`` (the per-item lines) and
    then one headline, ``Success: <summary>`` or ``Failed: N of M <noun>.``
    The command exits 1 when an item failed. ``noun`` is plural ("tables").

    One wording for every summary (HO2): ``<N> <noun>(s) <what happened>``,
    with details after a comma and a closing period, for example
    ``2 table(s) deleted.`` or ``3 config(s) pushed: 1 created, 2 updated, 0 deleted.``
    A dry run says ``would be <...>``.
    The fan-out reads end through ``_all_projects.finish_fanout``, which
    counts failed projects (E4: ``PAYG_NOT_AVAILABLE`` is not a failure).
    """
    code = item_failure_exit_code(failed)
    if formatter.json_mode:
        formatter.output(result, exit_code=code or 0)
    else:
        if render is not None:
            render()
        formatter.item_headline(failed=failed, total=total, noun=noun, summary=summary)
    if code:
        raise typer.Exit(code=code)


def finish_bulk(
    formatter: OutputFormatter,
    result: dict[str, Any],
    done: Sized,
    noun: str,
    done_as: str,
    render: Callable[[], object],
) -> None:
    """:func:`finish_items` for a bulk command whose result lists its failures in ``failed``.

    ``done`` holds the items that succeeded; ``noun`` is singular ("table"),
    ``done_as`` what happened to them ("deleted", "would be deleted"), so the
    headline reads ``Success: 2 table(s) deleted.`` or
    ``Failed: 1 of 3 tables. 2 table(s) deleted.``
    """
    failed = len(result["failed"])
    finish_items(
        formatter,
        result,
        failed=failed,
        total=len(done) + failed,
        noun=f"{noun}s",
        summary=f"{len(done)} {noun}(s) {done_as}.",
        render=render,
    )


def _is_help_request(ctx: typer.Context) -> bool:
    """Check if the current invocation is a --help request.

    The group callback fires before Click parses subcommand arguments,
    so --help for a subcommand (e.g. 'branch delete --help') is still
    in sys.argv at this point. We allow help through even for blocked commands.

    Also respects Click's resilient_parsing mode (tab completions).
    """
    import sys

    if "--help" in sys.argv or "-h" in sys.argv:
        return True
    return bool(ctx.resilient_parsing)


def check_cli_permission(ctx: typer.Context, group_name: str) -> None:
    """Check CLI command permissions using the active policy.

    Called from sub-app callbacks. Constructs operation name as
    '{group_name}.{subcommand}' and checks against the permission engine.
    Always allows --help through (no API calls made).

    Args:
        ctx: Typer context (must have permission_engine in obj).
        group_name: The sub-app name (e.g., 'branch', 'config').
    """
    if _is_help_request(ctx):
        return

    subcommand = ctx.invoked_subcommand
    if subcommand is None:
        return

    operation = f"{group_name}.{subcommand}"
    # The policy first: a write that it denies must not use up the one
    # refusal after a major update.
    engine = ctx.obj.get("permission_engine")
    if engine is not None and engine.active:
        check_cli_operation(ctx, operation)

    refuse_first_write_after_major_update(ctx, operation)


def refuse_first_write_after_major_update(ctx: typer.Context, operation: str) -> None:
    """Refuse the first write or destructive command after a major update (#791 row 28).

    The startup check records an update to a higher major that the user did
    not start with ``kbagent update --major`` in a state file
    (:mod:`..update_crossing`). The first write or destructive command that
    claims the file exits 6 with ``UPDATED_ACROSS_MAJOR``. Every later
    command runs.
    """
    refusal = refusal_for(operation)
    if refusal is None:
        print_held_warning()
        return
    get_formatter(ctx).error(
        message=refusal["message"],
        error_code=ErrorCode.UPDATED_ACROSS_MAJOR,
        retryable=True,
        details=refusal["details"],
    )
    raise typer.Exit(code=EXIT_PERMISSION_DENIED)


def check_cli_operation(ctx: typer.Context, operation: str) -> None:
    """Check one named operation against the active policy.

    Use for a check the group callback cannot express, where a flag carries a
    higher risk class than its command (see `permissions.FLAG_ESCALATIONS`).
    The callback has already cleared the bare command by the time a command
    body runs, so this is an additional gate, not a replacement.

    A command calls this exactly when the escalating flag (or an inherited
    organization scope) applies, so it also raises the command category that
    the ``TARGET_FROM_ACTIVE_CHOICE`` warning reads (spec T12).
    """
    if FLAG_ESCALATIONS.get(operation) == "destructive":
        get_formatter(ctx).command_category = "destructive"
    engine = ctx.obj.get("permission_engine")
    if engine is None or not engine.active:
        return

    try:
        engine.check_or_raise(operation)
    except PermissionDeniedError as exc:
        formatter = get_formatter(ctx)
        formatter.error(message=exc.message, error_code=ErrorCode.PERMISSION_DENIED)
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None


def resolve_project_alias(
    ctx: typer.Context,
    formatter: OutputFormatter,
    explicit: str | None,
) -> str:
    """Resolve the effective project alias for a single-project operation.

    Precedence (first match wins):
    1. ``explicit`` (typically the CLI ``--project`` flag)
    2. ``KBAGENT_PROJECT`` env var
    3. Persisted pin (``config.default_project`` set by ``kbagent project use``)
    4. Sole registered project when exactly one exists (convenience)
    5. Exit code 5 with a CONFIG_ERROR if none of the above resolves

    Use this from write/destructive command paths where implicit fan-out
    (``resolve_projects(None)`` returning every project) would be surprising
    or unsafe. Read paths should keep their existing fan-out behavior.

    Args:
        ctx: Typer context (must contain ``project_service``).
        formatter: Output formatter for structured error emission.
        explicit: The value of the CLI --project flag, or None.

    Returns:
        The resolved project alias (guaranteed to be registered).
    """
    from ..errors import ConfigError as _ConfigError

    service = get_service(ctx, "project_service")
    try:
        alias, _source = service.resolve_pinned_alias(explicit=explicit)
    except _ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    return alias


# T15: a branch ID belongs to one project, so --branch with several projects is refused.
BRANCH_ONE_PROJECT_MESSAGE = (
    "--branch needs one project: a branch ID belongs to one project, so it cannot be "
    "used with several --project values or with --all-projects"
)


def resolve_project_alias_or_none(
    ctx: typer.Context, formatter: OutputFormatter, explicit: str | None
) -> str | None:
    """Like :func:`resolve_project_alias`, but None when the T1 chain finds no project.

    For a command that has an offline answer without a project (`flow schema
    --full`, `flow validate`, spec T17): no registered project, or several and
    none active, is None. A wrong choice (an unknown alias, a bad
    ``KBAGENT_PROJECT``) is still ``CONFIG_ERROR`` with exit 5.
    """
    from ..errors import ConfigError as _ConfigError
    from ..services.base import NoImplicitProjectError

    service = get_service(ctx, "project_service")
    try:
        alias, _source = service.resolve_pinned_alias(explicit=explicit)
    except NoImplicitProjectError:
        return None
    except _ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    return alias


def is_interactive_terminal() -> bool:
    """True when a person can see a prompt on stderr and answer it on stdin."""
    return bool(
        hasattr(sys.stdin, "isatty")
        and sys.stdin.isatty()
        and hasattr(sys.stderr, "isatty")
        and sys.stderr.isatty()
    )


CONFIRMATION_HINT = (
    "Run the command again with --yes to confirm. "
    "AI agents: ask the user before you retry with --yes."
)


def _confirm_or_abort(formatter: OutputFormatter, yes: bool, question: str) -> None:
    """The shared safety prompt (spec S4).

    ``--yes`` and ``--json`` skip it: both mean consent. On a terminal the
    question goes to stderr, and a declined prompt prints ``Aborted.`` and
    exits 0 (E6). Without a terminal nobody can answer, and an open stdin
    pipe must not answer for the user (D6): the command refuses with
    ``CONFIRMATION_REQUIRED`` and exit 2, and the message names ``--yes``.
    """
    if yes or formatter.json_mode:
        return
    if not is_interactive_terminal():
        formatter.error(
            message=f"{ErrorCode.CONFIRMATION_REQUIRED.value}: {question} "
            f"There is no terminal to answer it. {CONFIRMATION_HINT}",
            error_code=ErrorCode.CONFIRMATION_REQUIRED,
        )
        raise typer.Exit(code=2)
    if not typer.confirm(question, err=True):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)


# The ``code`` of a ``warnings[]`` entry for a deprecated form (spec C3, O3).
DEPRECATED_CODE = "DEPRECATED"


@dataclass(frozen=True)
class Deprecation:
    """One deprecated command-line form (spec C3).

    ``replacement`` is the form to use instead, or None when the old form
    has no effect and the caller only drops it. ``remove_after`` is the ISO
    date after which a minor release may remove the old form. It is None
    while that date is unknown: the window of a form that 1.0.0 hides
    starts at the migration time M, which exists only once v1.0.0 is
    published.
    """

    message: str
    hint: str
    replacement: str | None
    remove_after: str | None = None


# C3: every deprecated form, keyed by its command path and option. A removed
# form stays here, so that its error can name the replacement.
DEPRECATIONS: dict[str, Deprecation] = {
    "stream delete --force": Deprecation(
        message="stream delete --force is deprecated. Use --yes to skip the confirmation.",
        hint="--yes",
        replacement="--yes",
    ),
    "semantic-layer import --yes": Deprecation(
        message="semantic-layer import --yes is deprecated and has no effect. Omit it.",
        hint="omit --yes (skip on conflict is the default)",
        replacement=None,
    ),
    # T17: --project names the target project on every other command; here the
    # value is the alias of the new project.
    "project add --project": Deprecation(
        message="project add --project is deprecated. Use --alias.",
        hint="--alias",
        replacement="--alias",
    ),
    "project create --project": Deprecation(
        message="project create --project is deprecated. Use --alias.",
        hint="--alias",
        replacement="--alias",
    ),
    # T4: "all projects" is spelled --all-projects on every command.
    "project refresh --all": Deprecation(
        message="project refresh --all is deprecated. Use --all-projects.",
        hint="--all-projects",
        replacement="--all-projects",
    ),
    "POST /org/refresh refresh_all": Deprecation(
        message="The refresh_all field of POST /org/refresh is deprecated. Use all_projects.",
        hint="all_projects",
        replacement="all_projects",
    ),
    # V9: `kbagent serve` partial updates move from PUT to PATCH. A REST form
    # is keyed "METHOD /path"; its warning goes into the Kbagent-Warnings header.
    "PUT /semantic-layer/items/{kind}/{name}": Deprecation(
        message="PUT /semantic-layer/items/{kind}/{name} is deprecated. Use PATCH.",
        hint="PATCH /semantic-layer/items/{kind}/{name}",
        replacement="PATCH /semantic-layer/items/{kind}/{name}",
    ),
    "PUT /merge-requests/{project}/{merge_request_id}": Deprecation(
        message="PUT /merge-requests/{project}/{merge_request_id} is deprecated. Use PATCH.",
        hint="PATCH /merge-requests/{project}/{merge_request_id}",
        replacement="PATCH /merge-requests/{project}/{merge_request_id}",
    ),
    "PUT /data-apps/{project}/{app_id}/secrets": Deprecation(
        message="PUT /data-apps/{project}/{app_id}/secrets is deprecated. Use PATCH.",
        hint="PATCH /data-apps/{project}/{app_id}/secrets",
        replacement="PATCH /data-apps/{project}/{app_id}/secrets",
    ),
    # Only the merge is deprecated: PUT with `replace: true` stays the full replace.
    "PUT /configs/{project}/{component_id}/{config_id}/variables": Deprecation(
        message="PUT .../variables without replace: true merges the variables, which is "
        "deprecated. Use PATCH to merge, or PUT with replace: true to replace all variables. "
        "After the window, a PUT without replace: true is refused, never run as a replace.",
        hint="PATCH /configs/{project}/{component_id}/{config_id}/variables",
        replacement="PATCH /configs/{project}/{component_id}/{config_id}/variables",
    ),
}


def deprecation_entry(form: str) -> tuple[str, dict[str, Any]]:
    """The message and the extra fields of the ``DEPRECATED`` warning of ``form``."""
    entry = DEPRECATIONS[form]
    when = (
        f"It can be removed after {entry.remove_after}."
        if entry.remove_after
        else "It keeps working until at least 28 days after the 1.0 migration date."
    )
    extra = {
        "hint": entry.hint,
        "replacement": entry.replacement,
        "remove_after": entry.remove_after,
    }
    return f"{entry.message} {when}", extra


def warn_deprecated(formatter: OutputFormatter, form: str) -> None:
    """Add the ``DEPRECATED`` warning of a ``DEPRECATIONS`` entry (one stderr line in human mode)."""
    message, extra = deprecation_entry(form)
    formatter.warning(message, code=DEPRECATED_CODE, **extra)


def deprecated_option(form: str, *param_decls: str, renamed_to: str | None = None) -> Any:
    """A hidden option for a deprecated form in ``DEPRECATIONS`` (spec C3).

    The vendored click has no option-level ``deprecated``. The callback adds the
    ``DEPRECATED`` warning when the caller gives the option. Without
    ``renamed_to`` it is a flag, and the command reads its value (for an alias,
    ``yes = yes or force``). With ``renamed_to`` it is the old name of an option
    with a value: the callback passes the value on as the default of the
    parameter ``renamed_to``. Click processes the given options first, so that
    parameter, when the caller left it out, takes this value, also when it is
    required.
    """
    if form not in DEPRECATIONS:
        raise KeyError(f"{form!r} has no DEPRECATIONS entry")

    def _warn(ctx: typer.Context, value: Any) -> Any:
        formatter = (ctx.obj or {}).get("formatter")
        if value and formatter is not None and not ctx.resilient_parsing:
            warn_deprecated(formatter, form)
        if value and renamed_to is not None:
            ctx.default_map = {**(ctx.default_map or {}), renamed_to: value}
        return value

    default = None if renamed_to is not None else False
    return typer.Option(default, *param_decls, hidden=True, callback=_warn)


_CONFIRM_CODE_LENGTH = 4


def require_random_code_confirmation(action_description: str) -> None:
    """Require the user to type a random hex code to confirm a high-risk action.

    Stops an AI agent from approving production-affecting writes (Developer
    Portal updates, permission policy changes) by mistake: without a real
    terminal the action is refused. This is friction, not a hard lockout: a
    process that runs as the same OS user can get around it (issue #271
    sec-09, docs/adr/0002-sec-09-config-privilege-separation.md).

    Behaviour:
    - No TTY -> raise typer.Exit(EXIT_PERMISSION_DENIED).
    - TTY + correct code -> return None (caller proceeds).
    - TTY + wrong code / EOF / interrupt -> raise typer.Exit(EXIT_PERMISSION_DENIED).

    Args:
        action_description: Short verb phrase shown in the prompt
            (e.g. "patch keboola.ex-foo", "update permission policy").
    """
    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if not is_tty:
        sys.stderr.write(
            f"\nRefusing to {action_description}: this action requires a "
            "real terminal so a human can type the confirmation code. "
            "There is no --yes bypass by design.\n"
        )
        raise typer.Exit(code=EXIT_PERMISSION_DENIED)

    code = secrets.token_hex(_CONFIRM_CODE_LENGTH)
    sys.stderr.write(f"\nTo {action_description}, type this code: {code}\n")
    sys.stderr.write("Confirmation: ")
    sys.stderr.flush()

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    if user_input != code:
        sys.stderr.write("Confirmation failed. Aborting.\n")
        raise typer.Exit(code=EXIT_PERMISSION_DENIED)


def resolve_identity_alias(ctx: typer.Context, explicit: str | None) -> str:
    """Resolve the dev-portal identity alias for this invocation.

    Order: explicit --identity flag > default from config > error.
    """
    if explicit:
        return explicit
    config_store: ConfigStore = get_service(ctx, "config_store")
    default = config_store.load().default_dev_portal_identity
    if not default:
        raise typer.BadParameter(
            "No Developer Portal identity selected. Pass --identity <alias>, "
            "or set a default via `kbagent dev-portal identity use <alias>`."
        )
    return default


def get_dev_portal_service(ctx: typer.Context):
    """Build a DeveloperPortalService bound to the current ConfigStore."""
    from ..dev_portal_client import DeveloperPortalClient
    from ..services.dev_portal_service import DeveloperPortalService

    config_store: ConfigStore = get_service(ctx, "config_store")
    return DeveloperPortalService(
        config_store=config_store,
        client_factory=lambda identity: DeveloperPortalClient(identity),
    )
