"""Data-app password delivery -- ``data-app password``, and after ``create`` / ``deploy --wait``.

Split out of ``data_app.py`` to respect the file-size budget (CONTRIBUTING.md
"File-size budgets"). ``data-app password`` attaches to the ``data-app``
sub-app through :func:`register_password_command`; ``create`` and ``deploy``
in ``data_app.py`` use the same options (:data:`CopyOption`,
:data:`RevealOption`) and the same delivery (:func:`deliver_password`), so
the three commands cannot drift apart.

The password never goes to stdout unless ``--reveal`` asks for it, so that it
does not enter the context of an AI agent that runs the command:

- In a terminal (human mode, stdin and stdout a TTY) the user presses ``c`` to
  copy it (``CopyableUrlWait`` from ``_url_copy.py``, the same prompt device
  login uses).
- Anywhere else (an agent, CI, ``--json``) it is copied only with ``--copy``.

The platform creates the password during the first deploy of a password-auth
app, so ``create`` / ``deploy`` can deliver it only after ``--wait`` sees the
app running -- and they read it only when something will deliver it (a flag,
or the terminal prompt). ``create --dry-run`` accepts and refuses the same
flags and only reports the delivery it would make (``password_delivery``).
The clipboard and the browser are local to the user's machine, which is why
this lives in the command layer and not in the service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from ..auth import environment
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..output import OutputFormatter
from ..services._data_app_password import PASSWORD_AUTH, DataAppPassword, data_app_auth_kind
from ..services.data_app_service import RUNNING_STATE
from . import _url_copy
from ._helpers import check_cli_operation, get_formatter, get_service, map_error_to_exit_code

DELIVERED_TO_CLIPBOARD = "clipboard"
DELIVERED_TO_STDOUT = "stdout"

# Long enough for `webbrowser` to start the opener, short enough that an
# opener it blocks on (a text-mode browser) cannot hold the command.
_BROWSER_OPEN_WAIT_SECONDS = 2.0

_COPY_PROMPT_TIMEOUT_SECONDS = 120.0
_COPY_PROMPT_HINT = "Press c to copy the password, Enter to finish"
# Enter arrives as "\n" in cbreak mode on POSIX and as "\r" from msvcrt on Windows.
_COPY_PROMPT_FINISH_KEYS = frozenset({"\n", "\r", "\x1b", "q"})
# The terminal flow prints the UI URL itself, so its lines only refer to it.
_UI_URL_SHOWS_IT = "The UI URL shows it under Open App after login."

# -- Shared options: one definition for `password`, `create` and `deploy` -------

CopyOption = Annotated[
    bool,
    typer.Option(
        "--copy",
        help=(
            "Copy the data-app password to the clipboard at once, without the terminal "
            "prompt. The only way to copy it when there is no terminal (an AI agent, CI) "
            "or with --json. On create / deploy it needs --wait."
        ),
    ),
]
RevealOption = Annotated[
    bool,
    typer.Option(
        "--reveal",
        help=(
            "Print the data-app password (human and --json output). For scripts and CI; "
            "in an AI agent session the password then goes into the chat history. "
            "On create / deploy it needs --wait."
        ),
    ),
]


# The operation that reads a password. `create` and `deploy` are checked as
# their own operations by the group callback, so reading the password after
# their deploy needs its own check: a policy can deny this one on purpose.
PASSWORD_OPERATION = "data-app.password"


@dataclass(frozen=True)
class PasswordFlags:
    """The --copy / --reveal choice of one invocation.

    ``permitted`` is False when the permission policy denies
    :data:`PASSWORD_OPERATION`; :func:`check_password_flags` sets it.
    """

    copy: bool = False
    reveal: bool = False
    permitted: bool = True

    @property
    def any(self) -> bool:
        return self.copy or self.reveal


@dataclass(frozen=True)
class PasswordDelivery:
    """How :func:`deliver_password` delivered the password (never the password itself)."""

    delivered_to: str | None  # DELIVERED_TO_CLIPBOARD, DELIVERED_TO_STDOUT or None
    message: str
    prompted: bool  # the terminal prompt ran and printed its own lines

    def json_fields(self, lookup: DataAppPassword) -> dict[str, Any]:
        """``ui_url`` and ``password_delivered_to``; ``password`` only for --reveal."""
        fields: dict[str, Any] = {
            "ui_url": lookup.ui_url,
            "password_delivered_to": self.delivered_to,
        }
        if self.delivered_to == DELIVERED_TO_STDOUT:
            fields["password"] = lookup.password
        return fields


def _invalid_argument(formatter: OutputFormatter, message: str) -> NoReturn:
    formatter.error(error_code=ErrorCode.INVALID_ARGUMENT, message=message)
    raise typer.Exit(code=2)


def check_password_flags(
    ctx: typer.Context, flags: PasswordFlags, *, deploy_blocker: str = ""
) -> PasswordFlags:
    """Check the flags before any API call and return them with ``permitted`` set.

    Exits 2 (INVALID_ARGUMENT) on flags that cannot work, and 6
    (PERMISSION_DENIED) when --copy / --reveal asks for a password that the
    policy denies (:data:`PASSWORD_OPERATION`). ``deploy_blocker`` is for
    ``create`` / ``deploy``: why the command will not wait for a deploy (e.g.
    "--wait is missing"), "" when it will.
    """
    formatter = get_formatter(ctx)
    if flags.copy and flags.reveal:
        _invalid_argument(formatter, "--reveal and --copy are mutually exclusive.")
    if flags.any and deploy_blocker:
        _invalid_argument(
            formatter,
            f"--copy and --reveal cannot be used here: {deploy_blocker}. The platform "
            "creates the password during the deploy, so kbagent can read it only after "
            "--wait sees the app running.",
        )
    if flags.any:
        check_cli_operation(ctx, PASSWORD_OPERATION)
    engine = ctx.obj.get("permission_engine") if isinstance(ctx.obj, dict) else None
    permitted = engine is None or not engine.active or engine.is_allowed(PASSWORD_OPERATION)
    return replace(flags, permitted=permitted)


def deploy_blocker(
    *, wait: bool, no_deploy: bool = False, use_managed_git_repo: bool = False
) -> str:
    """Why ``create`` / ``deploy`` will not wait for a deploy; "" when it will.

    ``--dry-run`` is deliberately not a blocker: a dry run accepts and refuses
    exactly what the real run does.
    """
    if no_deploy:
        return "--no-deploy skips the deploy"
    if use_managed_git_repo:
        return "--use-managed-git-repo skips the deploy (the repository starts empty)"
    if not wait:
        return "--wait is missing"
    return ""


def _copy_now(password: str) -> bool:
    """Copy through the detected clipboard tool; False when there is none or it fails."""
    copier = _url_copy.detect_clipboard()
    return copier is not None and copier(password)


def _open_app(app_url: str) -> bool:
    """Open the app in the browser; False when there is no URL or no browser."""
    if not app_url:
        return False
    return environment.open_browser(app_url, wait_seconds=_BROWSER_OPEN_WAIT_SECONDS)


def _non_interactive_message(
    lookup: DataAppPassword, delivered_to: str | None, *, copy: bool
) -> str:
    if delivered_to == DELIVERED_TO_CLIPBOARD:
        return f"The password of data app {lookup.app_id} is on the clipboard."
    if delivered_to == DELIVERED_TO_STDOUT:
        return f"The password of data app {lookup.app_id} is in this output (--reveal)."
    if copy:
        return (
            f"The password of data app {lookup.app_id} was not copied: no clipboard tool "
            f"was found, or the copy failed. {lookup.ui_hint()}"
        )
    return (
        f"The password of data app {lookup.app_id} was not copied; pass --copy to copy "
        f"it to the clipboard. {lookup.ui_hint()}"
    )


def _print_line(console: Console, *parts: str | tuple[str, str]) -> None:
    """One line of plain text parts: no markup parsing, no highlighting, and a
    URL in it is never folded (non-TTY consoles wrap at 80 columns)."""
    console.print(Text.assemble(*parts), highlight=False, soft_wrap=True)


def _print_links(console: Console, *, app_url: str, ui_url: str | None, app_opened: bool) -> None:
    _print_line(console, ("  App URL:", "bold"), f" {app_url or '-'}")
    _print_line(console, ("  UI URL:", "bold"), f"  {ui_url or '-'}")
    if app_opened:
        console.print("  Opened the app in the browser.")


def _print_result(console: Console, data: dict[str, Any]) -> None:
    label = (
        ("Success:", "bold green") if data["password_delivered_to"] else ("Warning:", "bold yellow")
    )
    _print_line(console, label, f" {data['message']}")
    _print_links(
        console, app_url=data["app_url"], ui_url=data["ui_url"], app_opened=data["app_opened"]
    )
    if "password" in data:
        _print_line(console, ("\nPassword:", "bold yellow"), f" {data['password']}")


def _copy_on_keypress(console: Console, lookup: DataAppPassword, *, app_opened: bool) -> bool:
    """Terminal flow: show the links, then copy the password when the user presses c."""
    wait = _url_copy.CopyableUrlWait(
        console,
        interactive=True,
        hint=_COPY_PROMPT_HINT,
        finish_keys=_COPY_PROMPT_FINISH_KEYS,
    )
    if not wait.enabled:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] No clipboard tool was found, so the password "
            f"of data app {escape(lookup.app_id)} cannot be copied. {_UI_URL_SHOWS_IT}"
        )
        _print_links(console, app_url=lookup.app_url, ui_url=lookup.ui_url, app_opened=app_opened)
        return False
    console.print(f"The password of data app {escape(lookup.app_id)} is ready to copy.")
    _print_links(console, app_url=lookup.app_url, ui_url=lookup.ui_url, app_opened=app_opened)
    wait.prompt_and_wait(lookup.password, _COPY_PROMPT_TIMEOUT_SECONDS)
    if wait.copied:
        return True
    # Also reached when c was pressed but the clipboard tool failed.
    seconds = int(_COPY_PROMPT_TIMEOUT_SECONDS)
    reason = "" if wait.finished else f"No key pressed for {seconds} s. "
    console.print(f"{reason}The password was not copied. {_UI_URL_SHOWS_IT}")
    return False


def _would_prompt(formatter: OutputFormatter, flags: PasswordFlags) -> bool:
    """The terminal ``c`` prompt runs only in human mode, in a foreground terminal, with no flag."""
    return not (flags.any or formatter.json_mode) and _url_copy.stdio_is_interactive()


def deliver_password(
    formatter: OutputFormatter,
    lookup: DataAppPassword,
    flags: PasswordFlags,
    *,
    app_opened: bool = False,
) -> PasswordDelivery:
    """Deliver the password: the terminal ``c`` prompt, ``--copy``, or ``--reveal``.

    The prompt runs only in human mode with a terminal and neither flag; it
    prints its own lines (``prompted=True``). Otherwise nothing is printed
    here -- the caller renders ``message`` and :meth:`PasswordDelivery.json_fields`.
    """
    if _would_prompt(formatter, flags):
        copied = _copy_on_keypress(formatter.console, lookup, app_opened=app_opened)
        state = "is on the clipboard" if copied else "was not copied"
        return PasswordDelivery(
            delivered_to=DELIVERED_TO_CLIPBOARD if copied else None,
            message=f"The password of data app {lookup.app_id} {state}.",
            prompted=True,
        )
    delivered_to: str | None = None
    if flags.reveal:
        delivered_to = DELIVERED_TO_STDOUT
    elif flags.copy and _copy_now(lookup.password):
        delivered_to = DELIVERED_TO_CLIPBOARD
    return PasswordDelivery(
        delivered_to=delivered_to,
        message=_non_interactive_message(lookup, delivered_to, copy=flags.copy),
        prompted=False,
    )


def _delivery_data(
    lookup: DataAppPassword, delivery: PasswordDelivery, *, app_opened: bool
) -> dict[str, Any]:
    return {
        **lookup.metadata(),
        **delivery.json_fields(lookup),
        "app_opened": app_opened,
        "message": delivery.message,
    }


# -- After `create --wait` / `deploy --wait` ----------------------------------


def _add_warning(result: dict[str, Any], message: str) -> None:
    result.setdefault("warnings", []).append(message)


def _no_password_warning(subject: str, auth: str) -> str:
    return f"{subject} uses auth '{auth}', so it has no password to copy."


def _read_failure_warning(app_id: str, exc: Exception) -> str:
    if isinstance(exc, KeboolaApiError) and exc.error_code == ErrorCode.NOT_FOUND:
        return (
            f"The deploy succeeded, but the password of data app {app_id} is not ready yet; "
            "run `kbagent data-app password` in a moment."
        )
    if isinstance(exc, KeboolaApiError) and exc.error_code == ErrorCode.VALIDATION_ERROR:
        return exc.message
    # Only the type for an unexpected exception: its text is not known to be safe to show.
    detail = exc.message if isinstance(exc, KeboolaApiError | ConfigError) else type(exc).__name__
    return (
        f"The deploy succeeded, but the password was not read ({detail}); "
        "run `kbagent data-app password` to try again."
    )


def _plan_password_delivery(result: dict[str, Any], flags: PasswordFlags) -> None:
    """``create --dry-run``: record the delivery the real run would make, call nothing.

    Reached only when a delivery would happen (a flag, or the terminal prompt).
    Checks only what can be checked without side effects: the planned auth,
    and whether a clipboard tool exists (probed, never run).
    """
    planned_config = (result.get("requests") or {}).get("put_storage_config") or {}
    auth = data_app_auth_kind(planned_config)
    if auth != PASSWORD_AUTH:
        if flags.any:
            _add_warning(result, _no_password_warning("The data app", auth))
        return
    if flags.reveal:
        result["password_delivery"] = DELIVERED_TO_STDOUT
        return
    result["password_delivery"] = DELIVERED_TO_CLIPBOARD if flags.copy else "prompt"
    if _url_copy.detect_clipboard() is None:
        _add_warning(result, "No clipboard tool was found, so the password would not be copied.")


def password_after_deploy(
    formatter: OutputFormatter,
    service: Any,
    result: dict[str, Any],
    flags: PasswordFlags,
    *,
    alias: str,
    waited: bool,
) -> DataAppPassword | None:
    """The password to deliver after a deploy the command waited for, or None.

    The app id comes from ``result`` (both the create and the deploy result carry it).

    Returns None, with no API call and no change to ``result``, when the
    command did not wait for a deploy or nothing would deliver the password
    (no --copy / --reveal and no terminal prompt: no terminal, a background
    job, or --json). A dry run records its plan instead (``password_delivery``).
    The deploy already succeeded, so nothing here fails the command: a problem
    becomes a ``warnings[]`` entry and the return is None. An app without
    password auth is silent unless --copy / --reveal asked for its password.
    """
    if not waited or not (flags.any or _would_prompt(formatter, flags)):
        return None
    if not flags.permitted:  # only the implicit prompt gets here; the flags exit earlier
        return None
    if result.get("dry_run"):
        _plan_password_delivery(result, flags)
        return None
    app_id = str(result.get("app_id") or "")
    state = str(result.get("state") or "")
    if state != RUNNING_STATE:
        if flags.any:
            _add_warning(
                result,
                f"Data app {app_id} is not running (state={state or '?'}), "
                "so its password was not read.",
            )
        return None
    auth = result.get("auth")  # `create` knows it; `deploy` leaves it to the service
    if auth is not None and auth != PASSWORD_AUTH:
        if flags.any:
            _add_warning(result, _no_password_warning(f"Data app {app_id}", auth))
        return None
    try:
        return service.get_data_app_password(alias=alias, app_id=app_id)
    # Everything here runs after a deploy that succeeded, so ANY failure is a
    # warning with exit 0 -- never a failed command that invites a redeploy.
    except Exception as exc:
        not_password_auth = (
            isinstance(exc, KeboolaApiError) and exc.error_code == ErrorCode.VALIDATION_ERROR
        )
        if flags.any or not not_password_auth:
            _add_warning(result, _read_failure_warning(app_id, exc))
        return None


def output_with_password(
    formatter: OutputFormatter,
    result: dict[str, Any],
    lookup: DataAppPassword | None,
    flags: PasswordFlags,
    *,
    human: Callable[[Console, dict[str, Any]], None],
) -> None:
    """Print a create / deploy result, then deliver the password when ``lookup`` is set.

    ``--json`` adds only ``ui_url``, ``password_delivered_to`` and (``--reveal``)
    ``password`` to the result. Human mode prints the result with ``human``,
    a dry run's ``password_delivery`` plan, the ``warnings``, and then the
    prompt or the delivery message.
    """
    if formatter.json_mode:
        if lookup is not None:
            result.update(deliver_password(formatter, lookup, flags).json_fields(lookup))
        formatter.output(result)
        return
    human(formatter.console, result)
    if result.get("password_delivery"):  # a dry run's plan
        formatter.console.print(f"  Password after the deploy: {result['password_delivery']}")
    for warning in result.get("warnings") or []:
        formatter.warning(escape(str(warning)))
    if lookup is None:
        return
    delivery = deliver_password(formatter, lookup, flags)
    if not delivery.prompted:
        _print_result(formatter.console, _delivery_data(lookup, delivery, app_opened=False))


# -- `data-app password` ---------------------------------------------------------


def register_password_command(app: typer.Typer) -> None:
    """Attach ``data-app password`` to the data-app sub-app."""

    @app.command("password")
    def data_app_password(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        app_id: str = typer.Option(..., "--app-id", help="Data Science numeric app id"),
        copy: CopyOption = False,
        reveal: RevealOption = False,
        open_app: bool = typer.Option(
            False, "--open", help="Also open the app URL in the browser."
        ),
    ) -> None:
        """Copy the password of a password-protected data app to the clipboard.

        The password is not printed, so it does not go into an AI agent's
        context. In a terminal the command shows the app URL and the Keboola
        UI page, then waits: press c to copy the password, Enter, Esc or q to
        finish (it gives up after 120 s). Without a terminal (an AI agent, CI)
        or with --json there is no prompt: only --copy copies the password.
        The clipboard tool gets it on stdin (pbcopy, clip, wl-copy, xclip,
        xsel, or clip.exe on WSL). When nothing is copied the command still
        exits 0 with `password_delivered_to: null`; `ui_url` is the Keboola UI
        page that shows the password under Open App.

        Needs only the project token (static or browser-login session), no
        Manage API token. Fails with VALIDATION_ERROR when the app does not
        use password auth, and with NOT_FOUND when it has no password yet
        (the platform creates it during the first deploy). The Keboola UI
        can reset the password.
        """
        formatter = get_formatter(ctx)
        flags = check_password_flags(ctx, PasswordFlags(copy=copy, reveal=reveal))
        service = get_service(ctx, "data_app_service")
        try:
            lookup = service.get_data_app_password(alias=project, app_id=app_id)
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

        app_opened = _open_app(lookup.app_url) if open_app else False
        delivery = deliver_password(formatter, lookup, flags, app_opened=app_opened)
        if delivery.prompted:
            return
        formatter.output(_delivery_data(lookup, delivery, app_opened=app_opened), _print_result)
