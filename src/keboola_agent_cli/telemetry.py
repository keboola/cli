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

import contextlib
import logging
import os
import threading
from typing import Any

import typer

from .auth.sentinel import is_session_token
from .config_store import ConfigStore, resolve_config_dir
from .constants import (
    ENV_DISABLE_TELEMETRY,
    ENV_DO_NOT_TRACK,
    TELEMETRY_COMPONENT_ID,
    TELEMETRY_SERVE_CONFIG_ID,
    TELEMETRY_TIMEOUT,
)
from .services.base import make_telemetry_client
from .services.project_service import ProjectService

# Telemetry is best-effort and swallows every failure, but not silently: each
# swallow/skip path logs at DEBUG (visible under `kbagent --verbose`), so a
# permanently broken events path can be diagnosed instead of shipping as a
# no-op nobody sees.
logger = logging.getLogger(__name__)

# Longest error text carried in ``results.error``. Keeps the event well under the
# events API's 200 KB cap and avoids dumping large response/state buffers.
_MAX_ERROR_LEN = 1000

# Command wrappers that are not a unit of work themselves. ``serve`` posts a
# per-request event from its middleware; ``repl`` posts a per-line event from its
# own loop. The outer invocation of each is excluded so it does not add a second,
# session-level event on top of the real work it dispatches.
_CLI_EXCLUDED_COMMANDS = frozenset({"serve", "repl"})

# Local-only commands that do no Storage work: `context` and `changelog` echo
# bundled text, and `version` is a meta command (its only network call is an
# optional GitHub update check, never Storage). Posting a usage event for these
# would turn a local command into a network-dependent one -- slow, or a needless
# failure, on an offline machine -- for no telemetry worth having.
_LOCAL_ONLY_COMMANDS = frozenset({"context", "changelog", "version"})

# serve infra / non-operation paths that should never post a usage event.
SERVE_SKIP_PATHS = frozenset({"/", "/health", "/docs", "/redoc", "/openapi.json", "/ui-config"})

# Non-zero exit codes a command returns as a documented, expected outcome, not a
# failure. `auth status` returns 3 for a signed-out or missing session -- its own
# help calls that a normal result an agent polls for -- so telemetry records it as
# `info`, not `error`. Keep this aligned with the exit codes the commands document.
_EXPECTED_NONZERO_EXITS: dict[str, frozenset[int]] = {"auth status": frozenset({3})}

# Recorded by the command error path (map_error_to_exit_code) so the entry
# wrapper can put a real message in ``results.error`` for mapped failures, whose
# exception never propagates out to the wrapper.
_last_error: str | None = None

# One reusable client per (stack_url, static token), so `kbagent serve` does not
# pay a fresh TCP+TLS handshake for every event. Guarded by a lock because serve
# posts events from worker threads. Session-token clients are never cached: their
# peeked access token rotates, so a cached one would go stale. Closed at serve
# shutdown via close_shared_clients (and cleared by reset() between test runs).
_client_cache: dict[tuple[str, str], Any] = {}
_client_cache_lock = threading.Lock()


def close_shared_clients() -> None:
    """Close and drop every cached telemetry client (serve shutdown / test reset)."""
    with _client_cache_lock:
        for client in _client_cache.values():
            with contextlib.suppress(Exception):
                client.close()
        _client_cache.clear()


def reset() -> None:
    """Clear per-invocation state. Called once at the start of each process run."""
    global _last_error
    _last_error = None
    close_shared_clients()


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
        if command is None or command in _CLI_EXCLUDED_COMMANDS or command in _LOCAL_ONLY_COMMANDS:
            return

        resolved_dir, source = resolve_config_dir(
            cli_config_dir=_option_value(argv, "--config-dir")
        )
        config_store = ConfigStore(config_dir=resolved_dir, source=source)

        # `-p` is the declared alias for `--project` on feature / stream / project / token.
        explicit = _option_value(argv, "--project") or _option_value(argv, "-p")
        try:
            alias, _ = ProjectService(config_store).resolve_pinned_alias(explicit=explicit)
        except Exception as exc:
            logger.debug("usage event skipped: no project context (%s)", exc)
            return  # local command / ambiguous -> nothing to post
        project = config_store.get_project(alias)
        if project is None or not project.stack_url or not project.token:
            logger.debug("usage event skipped: project %r has no token or stack", alias)
            return

        expected_nonzero = exit_code in _EXPECTED_NONZERO_EXITS.get(command, frozenset())
        _send_event(
            config_store,
            stack_url=project.stack_url,
            token=project.token,
            command=command,
            project_id=project.project_id,
            success=exit_code == 0 or expected_nonzero,
            error=_resolve_error_text(error),
            duration_s=duration_s,
            configuration_id=None,
            extra_params=None,
        )
    except Exception as exc:
        logger.debug("usage event failed: %s", exc)
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
    except Exception as exc:
        logger.debug("serve usage event failed: %s", exc)
        return


def _telemetry_client(config_store: ConfigStore, stack_url: str, token: str) -> tuple[Any, bool]:
    """Return ``(client, shared)`` for the events POST, or ``(None, False)`` to skip.

    A shared client is cached and reused, so the caller must NOT close it. A static
    token gets one cached client per (stack, token); a session token is never cached
    (its peeked access token rotates), so it gets a fresh, caller-closed client.
    """
    if is_session_token(token):
        return make_telemetry_client(config_store, stack_url, token), False
    key = (stack_url, token)
    with _client_cache_lock:
        client = _client_cache.get(key)
        if client is None:
            client = make_telemetry_client(config_store, stack_url, token)
            if client is not None:
                _client_cache[key] = client
    return client, True


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
    client, shared = _telemetry_client(config_store, stack_url, token)
    if client is None:
        # A session project whose token would need a refresh to become usable.
        # Best-effort telemetry never refreshes, so it skips this one event.
        logger.debug("usage event skipped: session token needs a refresh")
        return
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
        if not shared:
            client.close()


def _resolve_error_text(error: BaseException | None) -> str | None:
    if error is not None and not isinstance(error, KeyboardInterrupt):
        return str(error)[:_MAX_ERROR_LEN]
    if _last_error:
        return _last_error[:_MAX_ERROR_LEN]
    return None
