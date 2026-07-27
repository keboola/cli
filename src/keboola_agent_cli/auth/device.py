"""RFC 8628 device authorization: polling runner for `kbagent auth login --device-code`.

Used both as the forced flow (`--device-code`) and as the automatic fallback
from `auth/pkce.py` when `auth/environment.py` detects a remote/headless
machine, or when the PKCE setup/callback step fails before any code was
exchanged (see `pkce.py` module docstring for why that boundary matters).

`AuthClient` (package B, `auth/auth_client.py`) is imported only under
`TYPE_CHECKING`: this module must be importable -- and testable with a fake
client -- before that module lands, per the contract in
`docs/programmatic-auth-login-plan.md`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..constants import AUTH_DEVICE_MAX_INTERVAL, AUTH_DEVICE_SLOW_DOWN_INCREMENT
from ..errors import ErrorCode, KeboolaApiError
from .models import CliTokenResponse, DeviceAuthorization, DevicePollStatus

if TYPE_CHECKING:
    from .auth_client import AuthClient

# Substrings that mark a generic ERROR poll result as a user refusal rather
# than a protocol/client error, per the loop rules in the auth contract
# (section 8): the server's `invalid_grant` / `incorrect_client_credentials`
# family does not carry a dedicated "denied" status, but some deployments
# describe an explicit refusal in the free-text message instead.
_REFUSAL_KEYWORDS: tuple[str, ...] = ("denied", "declined", "rejected", "refused")


@dataclass(frozen=True)
class DeviceFlowOutcome:
    """Result of a completed device authorization flow."""

    tokens: CliTokenResponse
    polls: int  # how many token polls were issued (surfaced in --verbose/tests)


def _is_refusal_message(message: str) -> bool:
    """True when a generic ERROR poll result's message reads like a user refusal."""
    lowered = message.lower()
    return any(keyword in lowered for keyword in _REFUSAL_KEYWORDS)


def run_device_flow(
    client: AuthClient,
    *,
    on_prompt: Callable[[DeviceAuthorization], None],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DeviceFlowOutcome:
    """Drive the RFC 8628 device authorization flow to a token pair.

    ``on_prompt`` is invoked exactly once with the authorization so the caller
    can display ``verificationUri`` + ``userCode`` (mandatory) and
    best-effort open ``verificationUriComplete``. ``sleep``/``monotonic`` are
    injected so tests drive the poll loop without real time.

    Loop rules (auth contract section 8): honour ``authorization.interval``;
    on SLOW_DOWN adopt ``result.interval`` when the server sent one, else add
    `AUTH_DEVICE_SLOW_DOWN_INCREMENT`, capped at `AUTH_DEVICE_MAX_INTERVAL`;
    the deadline is ``authorization.expires_in`` seconds from the start of
    this call, checked before every poll so a slow terminal loop never
    overruns it by more than one interval.
    """
    authorization = client.start_device_authorization()
    on_prompt(authorization)

    interval = authorization.interval
    deadline = monotonic() + authorization.expires_in
    polls = 0

    while True:
        if monotonic() >= deadline:
            raise KeboolaApiError(
                "Device login timed out waiting for approval in the browser.",
                error_code=ErrorCode.AUTH_FLOW_TIMEOUT,
            )

        sleep(interval)
        polls += 1
        result = client.poll_device_token(authorization.device_code)

        if result.status == DevicePollStatus.OK:
            if result.tokens is None:  # pragma: no cover - defensive, contract guarantees this
                raise KeboolaApiError(
                    "Device token poll reported success without a token payload.",
                    error_code=ErrorCode.API_ERROR,
                )
            return DeviceFlowOutcome(tokens=result.tokens, polls=polls)

        if result.status == DevicePollStatus.PENDING:
            continue

        if result.status == DevicePollStatus.SLOW_DOWN:
            interval = (
                result.interval
                if result.interval is not None
                else interval + AUTH_DEVICE_SLOW_DOWN_INCREMENT
            )
            interval = min(interval, AUTH_DEVICE_MAX_INTERVAL)
            continue

        if result.status == DevicePollStatus.DENIED:
            raise KeboolaApiError(
                result.message or "The login request was denied.",
                error_code=ErrorCode.AUTH_FLOW_DENIED,
            )

        if result.status == DevicePollStatus.EXPIRED:
            raise KeboolaApiError(
                result.message or "The device login code expired before it was approved.",
                error_code=ErrorCode.AUTH_FLOW_EXPIRED,
            )

        # DevicePollStatus.ERROR
        message = result.message or "The device login failed."
        error_code = (
            ErrorCode.AUTH_FLOW_DENIED if _is_refusal_message(message) else ErrorCode.API_ERROR
        )
        raise KeboolaApiError(message, error_code=error_code)


__all__ = ["DeviceFlowOutcome", "run_device_flow"]
