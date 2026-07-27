"""Tests for auth/device.py: the RFC 8628 device-authorization poll loop.

Per the auth contract (section 14), these tests use a small fake `AuthClient`
stand-in rather than the real HTTP client, and inject `sleep`/`monotonic` so
the poll loop is driven by a fake clock instead of real time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from keboola_agent_cli.auth.device import DeviceFlowOutcome, run_device_flow
from keboola_agent_cli.auth.models import (
    CliTokenResponse,
    DeviceAuthorization,
    DevicePollResult,
    DevicePollStatus,
)
from keboola_agent_cli.constants import AUTH_DEVICE_MAX_INTERVAL
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError


def _authorization(*, interval: int = 5, expires_in: int = 900) -> DeviceAuthorization:
    return DeviceAuthorization(
        deviceCode="device-code-1",
        userCode="ABCD-EFGH",
        verificationUri="https://connection.keboola.com/device",
        verificationUriComplete="https://connection.keboola.com/device?user_code=ABCD-EFGH",
        expiresIn=expires_in,
        interval=interval,
    )


def _tokens() -> CliTokenResponse:
    return CliTokenResponse(accessToken="kbc_at_x", refreshToken="kbc_rt_x")


@dataclass
class FakeAuthClient:
    """Stand-in for `AuthClient` -- returns a fixed authorization and a scripted
    sequence of poll results, one per `poll_device_token` call."""

    authorization: DeviceAuthorization
    poll_results: list[DevicePollResult]
    start_calls: int = field(default=0, init=False)
    poll_calls: list[str] = field(default_factory=list, init=False)

    def start_device_authorization(self) -> DeviceAuthorization:
        self.start_calls += 1
        return self.authorization

    def poll_device_token(self, device_code: str) -> DevicePollResult:
        self.poll_calls.append(device_code)
        return self.poll_results.pop(0)


class FakeClock:
    """A monotonic clock that advances exactly by however much `sleep` was asked for."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class TestRunDeviceFlowSuccess:
    def test_pending_then_slow_down_with_interval_then_success(self) -> None:
        authorization = _authorization(interval=5)
        client = FakeAuthClient(
            authorization=authorization,
            poll_results=[
                DevicePollResult(status=DevicePollStatus.PENDING),
                DevicePollResult(status=DevicePollStatus.SLOW_DOWN, interval=10),
                DevicePollResult(status=DevicePollStatus.OK, tokens=_tokens()),
            ],
        )
        clock = FakeClock()
        prompts: list[DeviceAuthorization] = []

        outcome = run_device_flow(
            client,  # ty: ignore[invalid-argument-type]
            on_prompt=prompts.append,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert isinstance(outcome, DeviceFlowOutcome)
        assert outcome.tokens.access_token == "kbc_at_x"
        assert outcome.polls == 3
        assert prompts == [authorization]
        assert client.start_calls == 1
        assert client.poll_calls == ["device-code-1", "device-code-1", "device-code-1"]
        # Initial interval honoured twice (PENDING keeps it unchanged), then the
        # server-supplied slow_down interval (10) is adopted for the next wait.
        assert clock.sleep_calls == [5, 5, 10]

    def test_slow_down_without_interval_increments_and_caps_at_max(self) -> None:
        authorization = _authorization(interval=AUTH_DEVICE_MAX_INTERVAL - 5)
        client = FakeAuthClient(
            authorization=authorization,
            poll_results=[
                DevicePollResult(status=DevicePollStatus.SLOW_DOWN),
                DevicePollResult(status=DevicePollStatus.SLOW_DOWN),
                DevicePollResult(status=DevicePollStatus.OK, tokens=_tokens()),
            ],
        )
        clock = FakeClock()

        run_device_flow(
            client,  # ty: ignore[invalid-argument-type]
            on_prompt=lambda _auth: None,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        # start=max-5 -> +5 = max (first slow_down) -> +5 = max+5, capped at max.
        assert clock.sleep_calls == [
            AUTH_DEVICE_MAX_INTERVAL - 5,
            AUTH_DEVICE_MAX_INTERVAL,
            AUTH_DEVICE_MAX_INTERVAL,
        ]

    def test_on_prompt_is_called_exactly_once(self) -> None:
        authorization = _authorization()
        client = FakeAuthClient(
            authorization=authorization,
            poll_results=[DevicePollResult(status=DevicePollStatus.OK, tokens=_tokens())],
        )
        clock = FakeClock()
        prompts: list[DeviceAuthorization] = []

        run_device_flow(
            client,  # ty: ignore[invalid-argument-type]
            on_prompt=prompts.append,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        assert len(prompts) == 1


class TestRunDeviceFlowTerminalOutcomes:
    def test_denied_raises_auth_flow_denied(self) -> None:
        client = FakeAuthClient(
            authorization=_authorization(),
            poll_results=[
                DevicePollResult(status=DevicePollStatus.DENIED, message="The user declined.")
            ],
        )
        clock = FakeClock()

        with pytest.raises(KeboolaApiError) as exc_info:
            run_device_flow(
                client,  # ty: ignore[invalid-argument-type]
                on_prompt=lambda _auth: None,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        assert exc_info.value.error_code == ErrorCode.AUTH_FLOW_DENIED
        assert exc_info.value.message == "The user declined."

    def test_expired_raises_auth_flow_expired(self) -> None:
        client = FakeAuthClient(
            authorization=_authorization(),
            poll_results=[DevicePollResult(status=DevicePollStatus.EXPIRED)],
        )
        clock = FakeClock()

        with pytest.raises(KeboolaApiError) as exc_info:
            run_device_flow(
                client,  # ty: ignore[invalid-argument-type]
                on_prompt=lambda _auth: None,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        assert exc_info.value.error_code == ErrorCode.AUTH_FLOW_EXPIRED

    def test_generic_error_with_refusal_wording_maps_to_denied(self) -> None:
        client = FakeAuthClient(
            authorization=_authorization(),
            poll_results=[
                DevicePollResult(
                    status=DevicePollStatus.ERROR, message="Access was denied by policy."
                )
            ],
        )
        clock = FakeClock()

        with pytest.raises(KeboolaApiError) as exc_info:
            run_device_flow(
                client,  # ty: ignore[invalid-argument-type]
                on_prompt=lambda _auth: None,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        assert exc_info.value.error_code == ErrorCode.AUTH_FLOW_DENIED

    def test_generic_error_without_refusal_wording_maps_to_api_error(self) -> None:
        client = FakeAuthClient(
            authorization=_authorization(),
            poll_results=[
                DevicePollResult(
                    status=DevicePollStatus.ERROR, message="incorrect_client_credentials"
                )
            ],
        )
        clock = FakeClock()

        with pytest.raises(KeboolaApiError) as exc_info:
            run_device_flow(
                client,  # ty: ignore[invalid-argument-type]
                on_prompt=lambda _auth: None,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        assert exc_info.value.error_code == ErrorCode.API_ERROR

    def test_deadline_hit_raises_auth_flow_timeout_without_extra_polls(self) -> None:
        """expires_in=10, interval=5: the second poll lands exactly at the
        deadline, so a third poll must never be issued -- the deadline check
        happens before sleeping/polling again."""
        client = FakeAuthClient(
            authorization=_authorization(interval=5, expires_in=10),
            poll_results=[
                DevicePollResult(status=DevicePollStatus.PENDING),
                DevicePollResult(status=DevicePollStatus.PENDING),
            ],
        )
        clock = FakeClock()

        with pytest.raises(KeboolaApiError) as exc_info:
            run_device_flow(
                client,  # ty: ignore[invalid-argument-type]
                on_prompt=lambda _auth: None,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )

        assert exc_info.value.error_code == ErrorCode.AUTH_FLOW_TIMEOUT
        assert len(client.poll_calls) == 2
        assert client.poll_results == []  # both scripted results were consumed, no more requested
