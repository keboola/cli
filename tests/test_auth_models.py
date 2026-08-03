"""Tests for the programmatic-auth wire/state models (0.80.0).

Focused on `CliTokenResponse.refresh_expiry`, the single place that decides
whether a token response carries a refresh-token expiry. It is load-bearing
precisely because no deployment sends the field today: every real session
therefore stores ``refresh_expires_at = None``, which makes
`StackSession.refresh_token_expired` inert and leaves the server's rejection
(classified in `auth_client._is_rejected_grant`) as the ONLY signal that a
refresh token is dead. These tests pin that as a deliberate property -- and pin
that a backend which starts sending the field is honoured without further code
changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from keboola_agent_cli.auth.models import CliTokenResponse, StackSession

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _response(**extra: object) -> CliTokenResponse:
    """Build a token response from its wire shape, plus any extra server fields."""
    return CliTokenResponse.model_validate(
        {
            "accessToken": "kbc_at_x",
            "refreshToken": "kbc_rt_x",
            "expiresIn": 3600,
            "sessionId": "sess-1",
            **extra,
        }
    )


class TestRefreshExpiry:
    def test_absent_field_yields_none(self) -> None:
        """Today's real wire shape: no refresh expiry is sent, so none is invented."""
        assert _response().refresh_expiry(now=NOW) is None

    def test_seconds_from_now_are_resolved_to_an_absolute_utc_datetime(self) -> None:
        response = _response(refreshExpiresIn=30 * 24 * 3600)
        assert response.refresh_expiry(now=NOW) == NOW + timedelta(days=30)

    def test_float_seconds_are_accepted(self) -> None:
        """The field is a JSON number; a fractional value must not be discarded."""
        assert _response(refreshExpiresIn=90.5).refresh_expiry(now=NOW) == NOW + timedelta(
            seconds=90.5
        )

    def test_bool_is_rejected_rather_than_treated_as_one_second(self) -> None:
        """`True` is an `int` in Python -- without an explicit guard it would mean 1s.

        A one-second refresh expiry would make every session look instantly
        dead and purge it locally on the next command.
        """
        assert _response(refreshExpiresIn=True).refresh_expiry(now=NOW) is None

    def test_non_numeric_value_is_rejected(self) -> None:
        assert _response(refreshExpiresIn="3600").refresh_expiry(now=NOW) is None

    def test_defaults_to_wall_clock_when_no_now_is_injected(self) -> None:
        before = datetime.now(UTC)
        expiry = _response(refreshExpiresIn=600).refresh_expiry()
        assert expiry is not None
        assert (
            before + timedelta(seconds=600) <= expiry <= datetime.now(UTC) + timedelta(seconds=600)
        )


class TestRefreshTokenExpiredIsInertWithoutAServerValue:
    def test_none_expiry_never_reports_expired(self) -> None:
        """The state every real session is in: unknown expiry -> server decides.

        Guessing "expired" here would purge a live 30-day refresh token; the
        local check only ever fires once a backend actually sends an expiry.
        """
        session = StackSession(
            stack_url="https://connection.keboola.com",
            session_id="sess-1",
            access_token="kbc_at_x",
            refresh_token="kbc_rt_x",
            access_expires_at=NOW,
            refresh_expires_at=None,
            created_at=NOW,
        )
        assert session.refresh_token_expired(now=NOW + timedelta(days=365)) is False
