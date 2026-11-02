"""External tools behind ``kbagent oauth rotate-azure-secret``: ``az``, ``op`` and Entra.

Each wrapper runs one CLI as a subprocess and hands back parsed JSON. A new
client secret only ever moves through pipes: ``az`` writes it to our stdout
pipe, we pass it to ``op`` on stdin and to the broker in a request body. It is
never put on argv, in a file, or in an error message.

The runners are injected so tests replace them with fakes.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..constants import (
    OAUTH_ENTRA_CHECK_FIRST_DELAY_SECONDS,
    OAUTH_ENTRA_CHECK_MAX_DELAY_SECONDS,
    OAUTH_ENTRA_CHECK_WAIT_SECONDS,
    OAUTH_TOOL_STDERR_LIMIT,
    OAUTH_TOOL_TIMEOUT_SECONDS,
)
from ..entra_client import EntraClient, EntraTokenAttempt
from ..errors import ErrorCode, KeboolaApiError

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ToolError(KeboolaApiError):
    """An external CLI failed. The message never contains a secret."""

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}", error_code=ErrorCode.OAUTH_PREFLIGHT_FAILED)
        self.tool = tool


def _run_json(run: Runner, tool: str, args: list[str], stdin: str | None = None) -> Any:
    try:
        result = run(
            [tool, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=OAUTH_TOOL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise ToolError(tool, f"`{tool}` is not installed or not on PATH.") from None
    except OSError as exc:
        raise ToolError(tool, f"`{tool}` could not be started ({type(exc).__name__}).") from None
    except subprocess.TimeoutExpired:
        raise ToolError(tool, f"`{tool} {args[0]}` timed out.") from None
    if result.returncode != 0:
        raise ToolError(
            tool, result.stderr.strip()[:OAUTH_TOOL_STDERR_LIMIT] or f"exit {result.returncode}"
        )
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        raise ToolError(tool, f"`{tool} {args[0]}` did not return JSON.") from None


@dataclass
class PasswordCredential:
    """One client secret of an app registration (never the value)."""

    key_id: str
    display_name: str
    end_date: str


class AzureCli:
    """The ``az`` calls of the rotation. Uses the signed-in ``az`` account."""

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._run = runner

    def account(self) -> dict[str, Any]:
        return _run_json(self._run, "az", ["account", "show", "-o", "json"])

    def app(self, app_id: str) -> dict[str, Any]:
        return _run_json(self._run, "az", ["ad", "app", "show", "--id", app_id, "-o", "json"])

    def password_credentials(self, app_id: str) -> list[PasswordCredential]:
        rows = _run_json(
            self._run, "az", ["ad", "app", "credential", "list", "--id", app_id, "-o", "json"]
        )
        return [
            PasswordCredential(
                key_id=str(row.get("keyId", "")),
                display_name=str(row.get("displayName") or ""),
                end_date=str(row.get("endDateTime") or ""),
            )
            for row in rows or []
        ]

    def add_password(self, app_id: str, display_name: str, end_date: str) -> str:
        """Append a new client secret and return its value (kept in memory only)."""
        data = _run_json(
            self._run,
            "az",
            [
                "ad", "app", "credential", "reset", "--id", app_id, "--append",
                "--display-name", display_name, "--end-date", end_date, "-o", "json",
            ],
        )  # fmt: skip
        secret = (data or {}).get("password")
        if not secret:
            raise ToolError("az", "`az ad app credential reset` returned no password.")
        return secret


class OnePasswordCli:
    """The ``op`` calls of the rotation. Uses the desktop-app integration (Touch ID)."""

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._run = runner

    def whoami(self) -> dict[str, Any]:
        return _run_json(self._run, "op", ["whoami", "--format", "json"])

    def vault(self, vault: str) -> dict[str, Any]:
        return _run_json(self._run, "op", ["vault", "get", vault, "--format", "json"])

    def create_password_item(self, vault: str, title: str, secret: str, notes: str) -> str:
        """Create a Password item from a JSON template on stdin; return its ID."""
        template = {
            "title": title,
            "category": "PASSWORD",
            "fields": [
                {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "value": secret},
                {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "value": notes},
            ],
        }
        data = _run_json(
            self._run,
            "op",
            ["item", "create", "--vault", vault, "--format", "json", "-"],
            stdin=json.dumps(template),
        )
        item_id = (data or {}).get("id")
        if not item_id:
            raise ToolError("op", "`op item create` returned no item ID.")
        return str(item_id)


@dataclass
class SecretCheck:
    """Result of signing in to Entra with the new secret."""

    ok: bool
    detail: str


def _attempt_with_entra(tenant_id: str, app_id: str, secret: str) -> EntraTokenAttempt:
    with EntraClient() as client:
        return client.client_credentials(tenant_id, app_id, secret)


def check_client_secret(
    tenant_id: str,
    app_id: str,
    secret: str,
    *,
    wait_seconds: int = OAUTH_ENTRA_CHECK_WAIT_SECONDS,
    attempt: Callable[[str, str, str], EntraTokenAttempt] = _attempt_with_entra,
    sleep: Callable[[float], None] = time.sleep,
) -> SecretCheck:
    """Request a client-credentials token with the new secret.

    A new secret can take a short while to work everywhere in Entra, so a
    rejected secret is retried until ``wait_seconds`` have passed.
    """
    waited = 0
    delay = OAUTH_ENTRA_CHECK_FIRST_DELAY_SECONDS
    while True:
        result = attempt(tenant_id, app_id, secret)
        if result.status_code == 200:
            return SecretCheck(ok=True, detail="Entra issued a token with the new secret.")
        detail = (
            f"HTTP {result.status_code} {result.error}".strip()
            if result.status_code
            else result.error
        )
        if waited >= wait_seconds:
            return SecretCheck(ok=False, detail=detail)
        sleep(delay)
        waited += delay
        delay = min(delay * 2, OAUTH_ENTRA_CHECK_MAX_DELAY_SECONDS)
