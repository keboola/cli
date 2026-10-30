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

import httpx

from ..errors import ErrorCode, KeboolaApiError

Runner = Callable[..., subprocess.CompletedProcess[str]]
_TIMEOUT_SECONDS = 120
_STDERR_LIMIT = 300


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
            timeout=_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise ToolError(tool, f"`{tool}` is not installed or not on PATH.") from None
    except OSError as exc:
        raise ToolError(tool, f"`{tool}` could not be started ({type(exc).__name__}).") from None
    except subprocess.TimeoutExpired:
        raise ToolError(tool, f"`{tool} {args[0]}` timed out.") from None
    if result.returncode != 0:
        raise ToolError(tool, result.stderr.strip()[:_STDERR_LIMIT] or f"exit {result.returncode}")
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


def check_client_secret(
    tenant_id: str,
    app_id: str,
    secret: str,
    *,
    wait_seconds: int = 120,
    post: Callable[..., httpx.Response] = httpx.post,
    sleep: Callable[[float], None] = time.sleep,
) -> SecretCheck:
    """Request a client-credentials token with the new secret.

    A new secret can take a short while to work everywhere in Entra, so a
    rejected secret is retried until ``wait_seconds`` have passed.
    """
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    form = {
        "grant_type": "client_credentials",
        "client_id": app_id,
        "client_secret": secret,
        "scope": "https://graph.microsoft.com/.default",
    }
    waited = 0
    delay = 5
    detail = ""
    while True:
        try:
            response = post(url, data=form, timeout=30)
        except httpx.HTTPError as exc:
            detail = f"network error: {type(exc).__name__}"
        else:
            if response.status_code == 200:
                return SecretCheck(ok=True, detail="Entra issued a token with the new secret.")
            try:
                body = response.json()
            except ValueError:
                body = {}
            error = body.get("error", "") if isinstance(body, dict) else ""
            detail = f"HTTP {response.status_code} {error}".strip()
        if waited >= wait_seconds:
            return SecretCheck(ok=False, detail=detail)
        sleep(delay)
        waited += delay
        delay = min(delay * 2, 30)
