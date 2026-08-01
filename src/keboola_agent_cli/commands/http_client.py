"""`kbagent http` -- thin Typer wrapper for HttpForwarderService.

All transport / body parsing / env-var resolution lives in
``services/http_forwarder_service.py``; this command layer only does
what CONTRIBUTING.md §3-Layer architecture requires of commands:

1. Parse Typer arguments.
2. Call the service method.
3. Format and output the result via ``OutputFormatter``.
4. Catch :class:`ForwarderError` and map ``exit_code`` to ``typer.Exit``.

For the "why a service for self-call HTTP" rationale, see the docstring
of :mod:`keboola_agent_cli.services.http_forwarder_service`.
"""

from __future__ import annotations

import json
from typing import Any

import typer

from ..constants import HTTP_DEFAULT_TIMEOUT
from ..output import write_machine_output
from ..services.http_forwarder_service import (
    ForwardedResponse,
    ForwarderError,
    HttpForwarderService,
)
from ._helpers import get_formatter

http_app = typer.Typer(
    help=(
        "Raw HTTP client against the running `kbagent serve` "
        "(uses KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN env vars)."
    ),
    no_args_is_help=True,
)


def _print_json(_console: Any, data: Any) -> None:
    """Human-mode renderer: pipe-safe pretty JSON.

    Uses a raw stdout write instead of ``console.print`` because Rich
    can soft-wrap long strings or escape markup, which breaks downstream
    ``json.loads`` consumers. Real-world case: an AI agent piped
    ``kbagent http get /openapi.json`` into ``python3 -c "json.load(sys.stdin)"``
    and hit ``JSONDecodeError`` because Rich had reflowed lines. The output
    of ``kbagent http`` is virtually always parsed by something downstream
    (an LLM, a script, jq) -- so machine-clean stdout is the contract.
    """
    write_machine_output(json.dumps(data, indent=2) + "\n")


def _resolve_service(ctx: typer.Context) -> HttpForwarderService:
    """Pull the service from ctx.obj, falling back to a fresh instance.

    The service is registered in cli.py for normal CLI invocations, but
    the ``http`` subcommands also run inside a few unit-test entry
    points that bypass the cli root callback (where ctx.obj is empty).
    Falling back to a default-constructed instance keeps those callers
    working without a separate fixture; production callers always go
    through the registered instance because cli.py wires it up before
    any command executes.
    """
    obj = ctx.obj or {}
    svc = obj.get("http_forwarder_service")
    return svc if isinstance(svc, HttpForwarderService) else HttpForwarderService()


def _do_request(
    ctx: typer.Context,
    method: str,
    path: str,
    body: str | None,
    timeout: float,
) -> None:
    """Shared dispatch for get/post/patch/delete -- thin wrapper layer."""
    formatter = get_formatter(ctx)
    service = _resolve_service(ctx)
    try:
        result: ForwardedResponse = service.request(
            method,
            path,
            body=body,
            timeout=timeout,
        )
    except ForwarderError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=exc.exit_code) from None

    if isinstance(result.decoded, (dict, list)):
        formatter.output(result.decoded, human_formatter=_print_json)
    else:
        typer.echo(str(result.decoded))


@http_app.command("get")
def http_get(
    ctx: typer.Context,
    path: str = typer.Argument(
        ..., help="Endpoint path, e.g. /projects or /agents/cron/preview?cron=*+*+*+*+*"
    ),
    timeout: float = typer.Option(
        HTTP_DEFAULT_TIMEOUT, "--timeout", help="Request timeout (seconds)"
    ),
) -> None:
    """GET an endpoint on the running kbagent serve."""
    _do_request(ctx, "GET", path, body=None, timeout=timeout)


@http_app.command("post")
def http_post(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Endpoint path, e.g. /agents/test"),
    body: str | None = typer.Option(
        None,
        "--body",
        "-d",
        help="JSON body: inline JSON, @file.json, or - for stdin",
    ),
    timeout: float = typer.Option(
        HTTP_DEFAULT_TIMEOUT, "--timeout", help="Request timeout (seconds)"
    ),
) -> None:
    """POST to an endpoint on the running kbagent serve."""
    _do_request(ctx, "POST", path, body=body, timeout=timeout)


@http_app.command("patch")
def http_patch(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Endpoint path"),
    body: str | None = typer.Option(
        None,
        "--body",
        "-d",
        help="JSON body: inline JSON, @file.json, or - for stdin",
    ),
    timeout: float = typer.Option(
        HTTP_DEFAULT_TIMEOUT, "--timeout", help="Request timeout (seconds)"
    ),
) -> None:
    """PATCH an endpoint on the running kbagent serve."""
    _do_request(ctx, "PATCH", path, body=body, timeout=timeout)


@http_app.command("delete")
def http_delete(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Endpoint path"),
    timeout: float = typer.Option(
        HTTP_DEFAULT_TIMEOUT, "--timeout", help="Request timeout (seconds)"
    ),
) -> None:
    """DELETE an endpoint on the running kbagent serve."""
    _do_request(ctx, "DELETE", path, body=None, timeout=timeout)
