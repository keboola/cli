"""`kbagent http` -- raw HTTP client against the running ``kbagent serve``.

Designed for AI subprocesses spawned by ``kbagent serve`` (scheduled agent
tasks): when the serve injects ``KBAGENT_SERVE_URL`` + ``KBAGENT_SERVE_TOKEN``
into the subprocess env, the spawned ``claude``/``codex``/``gemini`` can call
the live HTTP API via ``kbagent http get /projects`` instead of forking a new
``kbagent`` CLI process tree (which would read a different, possibly stale,
``config.json``).

Thin layer: one ``httpx.request`` per invocation, JSON in/out, no business
logic. The server's authoritative ``ConfigStore`` handles every Keboola
interaction; this command just shuttles bytes.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx
import typer

from ..constants import (
    ENV_KBAGENT_SERVE_TOKEN,
    ENV_KBAGENT_SERVE_URL,
    HTTP_DEFAULT_TIMEOUT,
)
from ..errors import ErrorCode
from ._helpers import get_formatter

http_app = typer.Typer(
    help=(
        "Raw HTTP client against the running `kbagent serve` "
        "(uses KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN env vars)."
    ),
    no_args_is_help=True,
)


def _resolve_serve_endpoint() -> tuple[str, str]:
    """Read the serve URL + token from env or exit with a usage error."""
    url = os.environ.get(ENV_KBAGENT_SERVE_URL, "").rstrip("/")
    token = os.environ.get(ENV_KBAGENT_SERVE_TOKEN, "")
    if not url or not token:
        typer.echo(
            f"Error: `kbagent http` requires {ENV_KBAGENT_SERVE_URL} and "
            f"{ENV_KBAGENT_SERVE_TOKEN} env vars. These are auto-injected by "
            "`kbagent serve` for scheduled-agent subprocesses; outside that "
            "context the command has no target.",
            err=True,
        )
        raise typer.Exit(code=2)
    return url, token


def _resolve_body(body: str | None) -> Any:
    """Parse a --body argument (inline JSON, @file, or - for stdin)."""
    if body is None or body == "":
        return None
    if body == "-":
        raw = sys.stdin.read()
    elif body.startswith("@"):
        with open(body[1:], encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = body
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: --body is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _do_request(
    ctx: typer.Context,
    method: str,
    path: str,
    body: str | None,
    timeout: float,
) -> None:
    """Shared dispatch for get/post/patch/delete."""
    formatter = get_formatter(ctx)
    base_url, token = _resolve_serve_endpoint()

    if not path.startswith("/"):
        path = "/" + path

    payload = _resolve_body(body)
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method,
                f"{base_url}{path}",
                headers=headers,
                json=payload,
            )
    except httpx.HTTPError as exc:
        formatter.error(
            message=f"HTTP transport error: {exc}",
            error_code=ErrorCode.CONNECTION_ERROR,
        )
        raise typer.Exit(code=4) from None

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            decoded: Any = response.json()
        except json.JSONDecodeError:
            decoded = response.text
    else:
        decoded = response.text

    if response.status_code >= 400:
        formatter.error(
            message=f"HTTP {response.status_code}: {decoded}",
            error_code=ErrorCode.API_ERROR,
        )
        raise typer.Exit(code=1)

    if isinstance(decoded, (dict, list)):
        formatter.output(decoded, human_formatter=_print_json)
    else:
        typer.echo(str(decoded))


def _print_json(_console: Any, data: Any) -> None:
    """Human-mode renderer: pipe-safe pretty JSON.

    Uses ``sys.stdout.write`` instead of ``console.print`` because Rich
    can soft-wrap long strings or escape markup, which breaks downstream
    ``json.loads`` consumers. Real-world case: an AI agent piped
    ``kbagent http get /openapi.json`` into ``python3 -c "json.load(sys.stdin)"``
    and hit ``JSONDecodeError`` because Rich had reflowed lines. The output
    of ``kbagent http`` is virtually always parsed by something downstream
    (an LLM, a script, jq) -- so machine-clean stdout is the contract.
    """
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


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
