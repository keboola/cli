"""Best-effort, per-invocation usage telemetry.

kbagent posts one custom Storage event per command to the acting project's own
events (``POST /v2/storage/events``), the same mechanism the kbc CLI uses. The
event is stored as ``ext.keboola.cli.`` (CLI) or ``ext.keboola.cli.serve``
(serve REST API) and stamped server-side with the caller's token + user agent.

This is telemetry, NOT an audit trail: it is voluntary (the two env vars below
disable it) and best-effort (a failed or blocked events endpoint never affects
the command). Mutating operations are recorded server-side by Connection as
``storage.*`` / ``auditLog.*`` events regardless of this signal.

Everything here is wrapped so a telemetry failure can never change the exit code,
break the command, or stall it (the POST carries a short timeout).
"""

from __future__ import annotations

import os
from typing import Any

import typer

from .config_store import ConfigStore, resolve_config_dir
from .constants import (
    ENV_DISABLE_TELEMETRY,
    ENV_DO_NOT_TRACK,
    TELEMETRY_COMPONENT_ID,
    TELEMETRY_SERVE_CONFIG_ID,
    TELEMETRY_TIMEOUT,
)
from .services.base import make_client_factory
from .services.project_service import ProjectService

# Longest error text carried in ``results.error``. Keeps the event well under the
# events API's 200 KB cap and avoids dumping large response/state buffers.
_MAX_ERROR_LEN = 1000

# Command wrappers that are not a unit of work themselves. ``serve`` posts a
# per-request event from its middleware; ``repl`` posts a per-line event from its
# own loop. The outer invocation of each is excluded so it does not add a second,
# session-level event on top of the real work it dispatches.
_CLI_EXCLUDED_COMMANDS = frozenset({"serve", "repl"})

# serve infra / non-operation paths that should never post a usage event.
SERVE_SKIP_PATHS = frozenset({"/", "/health", "/docs", "/redoc", "/openapi.json", "/ui-config"})

# Recorded by the command error path (map_error_to_exit_code) so the entry
# wrapper can put a real message in ``results.error`` for mapped failures, whose
# exception never propagates out to the wrapper.
_last_error: str | None = None


def reset() -> None:
    """Clear per-invocation state. Called once at the start of each process run."""
    global _last_error
    _last_error = None


def note_command_error(message: str) -> None:
    """Record the message of a mapped command error for the usage event."""
    global _last_error
    _last_error = message


def telemetry_disabled() -> bool:
    """True if the user opted out via KBAGENT_DISABLE_TELEMETRY or DO_NOT_TRACK."""
    return _env_truthy(ENV_DISABLE_TELEMETRY) or _env_truthy(ENV_DO_NOT_TRACK)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def command_from_argv(argv: list[str]) -> str | None:
    """Derive the invoked command path (e.g. ``config list``) from ``sys.argv``.

    Walks the real Click command tree so nested groups resolve fully
    (``semantic-layer add dataset``) and a leaf's own arguments are not mistaken
    for a subcommand (``search foo`` -> ``search``). Returns ``None`` when no
    command was invoked (bare ``kbagent``, global flags only).

    Descent is driven by the presence of a ``commands`` mapping (a group has one,
    a leaf does not) rather than an ``isinstance(click.Group)`` check, which is
    unreliable here: Typer's group is not always the same ``click.Group`` class
    this module would import.

    The command tree is built fresh on each call, not cached: the cache is worth
    almost nothing (telemetry fires once per invocation) and a module-level cache
    is a stale-state trap.
    """
    # Lazy on purpose: cli imports this module at top, so a module-level
    # `from .cli import app` would be a circular import.
    from .cli import app

    node: Any = typer.main.get_command(app)
    parts: list[str] = []
    i = 1  # skip the program name
    while i < len(argv):
        token = argv[i]
        if token == "--":
            break
        if token in _GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        commands = getattr(node, "commands", None)
        if not commands or token not in commands:
            break  # a positional argument of the leaf command, not a subcommand
        parts.append(token)
        node = commands[token]
        i += 1
    return " ".join(parts) if parts else None


# Global options declared on the root callback that take a value; skipped (with
# their value) when reading the command path out of argv.
_GLOBAL_OPTS_WITH_VALUE = frozenset({"--config-dir", "--conversation-id"})


def _option_value(argv: list[str], flag: str) -> str | None:
    """First value of ``--flag VALUE`` or ``--flag=VALUE`` in argv, else None."""
    prefix = flag + "="
    for i, token in enumerate(argv):
        if token == flag and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def emit_cli_invocation(
    argv: list[str],
    exit_code: int,
    error: BaseException | None,
    duration_s: float,
) -> None:
    """Post a usage event for one completed CLI invocation. Never raises."""
    try:
        if telemetry_disabled():
            return
        if any(flag in argv for flag in ("--help", "-h", "--version", "-V")):
            return
        command = command_from_argv(argv)
        if command is None or command in _CLI_EXCLUDED_COMMANDS:
            return

        resolved_dir, source = resolve_config_dir(
            cli_config_dir=_option_value(argv, "--config-dir")
        )
        config_store = ConfigStore(config_dir=resolved_dir, source=source)

        explicit = _option_value(argv, "--project")
        try:
            alias, _ = ProjectService(config_store).resolve_pinned_alias(explicit=explicit)
        except Exception:
            return  # no project context (local command / ambiguous) -> nothing to post
        project = config_store.get_project(alias)
        if project is None or not project.stack_url or not project.token:
            return

        _send_event(
            config_store,
            stack_url=project.stack_url,
            token=project.token,
            command=command,
            project_id=project.project_id,
            success=exit_code == 0,
            error=_resolve_error_text(error),
            duration_s=duration_s,
            configuration_id=None,
            extra_params=None,
        )
    except Exception:
        return  # telemetry must never affect the CLI


def send_serve_event(
    config_store: ConfigStore,
    *,
    method: str,
    path: str,
    operation: str,
    status_code: int,
    duration_s: float,
    project_alias: str | None,
) -> None:
    """Post a usage event for one serve REST request. Never raises.

    Blocking (builds a sync client + posts), so serve calls this off the event
    loop via a worker thread.
    """
    try:
        if telemetry_disabled():
            return
        alias = project_alias
        if not alias:
            try:
                alias = config_store.load().default_project or None
            except Exception:
                alias = None
        if not alias:
            return
        project = config_store.get_project(alias)
        if project is None or not project.stack_url or not project.token:
            return

        _send_event(
            config_store,
            stack_url=project.stack_url,
            token=project.token,
            command=operation,
            project_id=project.project_id,
            success=status_code < 400,
            error=None if status_code < 400 else f"HTTP {status_code}",
            duration_s=duration_s,
            configuration_id=TELEMETRY_SERVE_CONFIG_ID,
            extra_params={"path": f"{method} {path}"},
        )
    except Exception:
        return


def _send_event(
    config_store: ConfigStore,
    *,
    stack_url: str,
    token: str,
    command: str,
    project_id: int | None,
    success: bool,
    error: str | None,
    duration_s: float,
    configuration_id: str | None,
    extra_params: dict[str, Any] | None,
) -> None:
    params: dict[str, Any] = {"command": command}
    if extra_params:
        params.update(extra_params)
    results: dict[str, Any] = {}
    if project_id is not None:
        results["projectId"] = project_id
    if not success and error:
        results["error"] = error

    verb = "done" if success else "failed"
    client = make_client_factory(config_store)(stack_url, token)
    try:
        client.trigger_event(
            component_id=TELEMETRY_COMPONENT_ID,
            message=f"{command} command {verb}.",
            event_type="info" if success else "error",
            params=params,
            results=results or None,
            duration=duration_s,
            configuration_id=configuration_id,
            timeout=TELEMETRY_TIMEOUT,
        )
    finally:
        client.close()


def _resolve_error_text(error: BaseException | None) -> str | None:
    if error is not None and not isinstance(error, KeyboardInterrupt):
        return str(error)[:_MAX_ERROR_LEN]
    if _last_error:
        return _last_error[:_MAX_ERROR_LEN]
    return None
