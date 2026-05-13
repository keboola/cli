"""HttpForwarderService -- self-call HTTP transport against the running ``kbagent serve``.

This is the *business* layer for ``kbagent http get|post|patch|delete``.
The command (``commands/http_client.py``) is a thin Typer wrapper that
parses CLI options, calls ``HttpForwarderService.request()``, and
formats the result. All HTTP transport, body parsing, env-var
resolution, and response decoding live here so the CLI stays a pure
presentation layer (CONTRIBUTING.md §3-Layer architecture).

Why a service for what is conceptually "shuttle bytes between two
processes"? Three reasons:

1. **Layer discipline.** The 3-layer rule has no "thin forwarder"
   carve-out, so the next ``/kbagent:review`` would re-flag the
   ``import httpx`` if it lived in the command file. Splitting also
   makes the surface mockable without ``pytest-httpx`` transport
   interception (callers can replace the service in ``ctx.obj``).
2. **Reuse.** Future scheduler internals (e.g. an in-process retry of
   a failed agent task) can call the forwarder without going through
   Typer/argv -- e.g. a Python integration test that wants to call
   ``GET /agents`` without spawning a subprocess.
3. **Single error vocabulary.** The transport surface returns / raises
   the same ``ForwarderError`` regardless of cause (missing env, body
   parse, transport error, HTTP non-2xx); commands map those to exit
   codes once, not per-method.

Unlike most Keboola-facing services, this one does *not* take a
``ConfigStore`` -- there is no per-project state to resolve. The serve
URL + token come from process env vars injected by the parent
``kbagent serve`` when it spawned this subprocess.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from ..constants import ENV_KBAGENT_SERVE_TOKEN, ENV_KBAGENT_SERVE_URL
from ..errors import ErrorCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForwarderError(Exception):
    """Single error type for every failure mode of ``HttpForwarderService``.

    Pairs an :class:`ErrorCode` with a message and a CLI-appropriate exit
    code so the command layer can map exceptions to ``typer.Exit`` with one
    handler instead of one per call site.

    The exit-code semantics mirror the rest of the kbagent CLI:
    - ``2``  usage error (missing env vars, malformed --body)
    - ``4``  network error (connection refused, DNS, timeout)
    - ``1``  general API error (HTTP 4xx/5xx response from the serve)
    """

    error_code: ErrorCode
    message: str
    exit_code: int

    def __str__(self) -> str:  # pragma: no cover -- format only
        return self.message


@dataclass(frozen=True)
class ForwardedResponse:
    """Result of a successful HTTP forward.

    Distinguishes JSON-decoded payloads (rendered as pretty JSON) from
    plain text (echoed verbatim) so the command layer doesn't have to
    re-inspect ``response.headers["content-type"]`` -- the service has
    already done that work.
    """

    decoded: Any
    is_json: bool


class HttpForwarderService:
    """Forwarder for ``kbagent http`` -- self-call against the running serve.

    Stateless and dependency-free at the HTTP layer: every request opens
    a fresh ``httpx.Client`` (context-managed for descriptor safety per
    CONTRIBUTING.md §Resource management). Endpoint discovery and body
    parsing are pure functions, exposed as ``@staticmethod`` so callers
    that want to validate inputs without performing a request (e.g. a
    dry-run) can use them directly.

    Construction takes no arguments because the serve URL and token live
    in process env vars (set by the parent ``kbagent serve`` when it
    spawned this subprocess). Tests inject a stub via Typer ``ctx.obj``.
    """

    @staticmethod
    def resolve_endpoint() -> tuple[str, str]:
        """Read ``KBAGENT_SERVE_URL`` + ``KBAGENT_SERVE_TOKEN`` from env.

        Raises:
            ForwarderError: with ``exit_code=2`` if either variable is
                unset/empty. The message names both required vars and the
                expected context (parent serve subprocess) so the operator
                doesn't have to grep the source.
        """
        url = os.environ.get(ENV_KBAGENT_SERVE_URL, "").rstrip("/")
        token = os.environ.get(ENV_KBAGENT_SERVE_TOKEN, "")
        if not url or not token:
            raise ForwarderError(
                error_code=ErrorCode.CONFIG_ERROR,
                message=(
                    f"`kbagent http` requires {ENV_KBAGENT_SERVE_URL} and "
                    f"{ENV_KBAGENT_SERVE_TOKEN} env vars. These are auto-injected "
                    "by `kbagent serve` for scheduled-agent subprocesses; outside "
                    "that context the command has no target."
                ),
                exit_code=2,
            )
        return url, token

    @staticmethod
    def resolve_body(body: str | None) -> Any:
        """Parse a ``--body`` argument into a Python object for ``json=``.

        Three input shapes:
        - ``None`` / ``""`` -> ``None`` (no body sent)
        - ``"-"``           -> read JSON from stdin
        - ``"@<path>"``     -> read JSON from a file
        - anything else     -> treat as inline JSON literal

        Raises:
            ForwarderError: with ``exit_code=2`` if the resulting string
                is not valid JSON. Includes the parser's column/line so
                the user can fix the literal without rerunning with
                ``--verbose``.
        """
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
            raise ForwarderError(
                error_code=ErrorCode.CONFIG_ERROR,
                message=f"--body is not valid JSON: {exc}",
                exit_code=2,
            ) from None

    def request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        timeout: float,
    ) -> ForwardedResponse:
        """Perform one HTTP request against the running ``kbagent serve``.

        Resolves env-vars + parses body BEFORE opening the HTTP client, so
        a usage error doesn't even create a connection. The client itself
        is context-managed (``with httpx.Client()``) so descriptors close
        on every exit path including exceptions -- the LLM-trap that
        CONTRIBUTING.md §Resource management calls out specifically.

        Args:
            method: HTTP verb (``GET`` / ``POST`` / ``PATCH`` / ``DELETE``).
            path: Endpoint path. Leading ``/`` is added if missing so
                callers can be sloppy ("``projects``" works as well as
                "``/projects``").
            body: ``--body`` shape (see :meth:`resolve_body`).
            timeout: Per-request seconds. Plumbed from the caller so a
                slow Storage listing can opt into a longer wait.

        Returns:
            :class:`ForwardedResponse` with the decoded payload and a
            flag the command layer uses to pick the renderer (pretty
            JSON vs plain text).

        Raises:
            ForwarderError: usage / network / non-2xx HTTP. The
                ``exit_code`` field is the CLI's intended outcome.
        """
        base_url, token = self.resolve_endpoint()
        payload = self.resolve_body(body)

        if not path.startswith("/"):
            path = "/" + path
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
            raise ForwarderError(
                error_code=ErrorCode.CONNECTION_ERROR,
                message=f"HTTP transport error: {exc}",
                exit_code=4,
            ) from None

        content_type = response.headers.get("content-type", "")
        is_json = "application/json" in content_type
        if is_json:
            try:
                decoded: Any = response.json()
            except json.JSONDecodeError:
                decoded = response.text
                is_json = False
        else:
            decoded = response.text

        if response.status_code >= 400:
            raise ForwarderError(
                error_code=ErrorCode.API_ERROR,
                message=f"HTTP {response.status_code}: {decoded}",
                exit_code=1,
            )

        return ForwardedResponse(decoded=decoded, is_json=is_json)
