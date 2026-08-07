"""Wire models and persisted state for programmatic auth (browser login).

Two families of model live here:

- Wire models (`AuthUser`, `CliTokenResponse`, `DeviceAuthorization`,
  `AuthProject`, `IntrospectResponse`, `DevicePollResult`, `RevokeResult`,
  `SudoResult`, `PatItem`, `PatCreateResult`): shaped after the Keboola
  auth-service JSON responses, never persisted.
- Persisted state (`StackSession`, `AuthState`): the exact shape written to
  and read from ``auth.json`` by `AuthStateStore`.

All wire models use ``populate_by_name`` (so both the camelCase alias and the
Pythonic name work as constructor kwargs in tests) and ``extra="allow"`` (so
an unrecognised field from a newer backend passes through instead of raising).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from ..constants import (
    AUTH_DEVICE_DEFAULT_INTERVAL,
    AUTH_REFRESH_LEASE_MAX_HORIZON,
    AUTH_REFRESH_MARGIN,
    AUTH_STATE_VERSION,
)

_WIRE_MODEL_CONFIG = {"populate_by_name": True, "extra": "allow"}


def _coerce_id_to_str(value: object) -> object:
    """Coerce a numeric user id to str before validation.

    Some Keboola endpoints return the user id as a JSON number rather than a
    string; normalising here keeps `AuthUser.id` a stable `str` regardless of
    which shape the backend sent.
    """
    if isinstance(value, int):
        return str(value)
    return value


def _ensure_utc(value: object) -> object:
    """Coerce a naive datetime (e.g. read back from an older auth.json) to UTC.

    All persisted/parsed datetimes in this module are timezone-aware UTC.
    A naive value is assumed to already be in UTC (that is how this module
    always writes them) rather than the local timezone.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class AuthUser(BaseModel):
    """The signed-in Keboola user, as embedded in token/introspect responses."""

    id: str = ""
    email: str = ""
    name: str = ""

    model_config = _WIRE_MODEL_CONFIG

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value: object) -> object:
        return _coerce_id_to_str(value)


class CliTokenResponse(BaseModel):
    """Token pair returned by the PKCE exchange, device-token, and refresh endpoints."""

    access_token: str = Field(alias="accessToken")
    refresh_token: str = Field(alias="refreshToken")
    token_type: str = Field(default="Bearer", alias="tokenType")
    expires_in: int = Field(default=3600, alias="expiresIn")
    session_id: str = Field(default="", alias="sessionId")
    user: AuthUser | None = None

    model_config = _WIRE_MODEL_CONFIG

    def refresh_expiry(self, *, now: datetime | None = None) -> datetime | None:
        """Absolute refresh-token expiry from ``refreshExpiresIn``, or None if absent.

        No Keboola deployment sends a refresh-token expiry today -- this frozen
        contract has no dedicated field for it -- so the value is read
        opportunistically out of ``model_extra`` (the model is
        ``extra="allow"``) as seconds-from-now. Until some backend starts
        sending it, every persisted `StackSession` carries
        ``refresh_expires_at = None``, which makes
        `StackSession.refresh_token_expired` inert **by design**: the only
        signal that a refresh token is dead is the server rejecting it,
        classified in `auth_client._is_rejected_grant`. Never substitute a
        guessed TTL here -- guessing too short purges a live session, too long
        keeps re-presenting a dead one.

        Lives on the model (rather than in each caller) so `AuthService.login`
        and `SessionTokenProvider._perform_refresh` cannot drift on which key
        carries this or what it means: before this existed, refresh honoured
        the field and login hardcoded None, so a backend that started sending
        it would have been ignored until the first refresh.
        """
        seconds_in = (self.model_extra or {}).get("refreshExpiresIn")
        if isinstance(seconds_in, bool) or not isinstance(seconds_in, int | float):
            return None
        moment = now if now is not None else datetime.now(UTC)
        return moment + timedelta(seconds=seconds_in)


class DeviceAuthorization(BaseModel):
    """Response to ``POST /v1/auth/device`` (RFC 8628 device_authorization_response)."""

    device_code: str = Field(alias="deviceCode")
    user_code: str = Field(alias="userCode")
    verification_uri: str = Field(alias="verificationUri")
    verification_uri_complete: str = Field(default="", alias="verificationUriComplete")
    expires_in: int = Field(default=900, alias="expiresIn")
    interval: int = Field(default=AUTH_DEVICE_DEFAULT_INTERVAL)

    model_config = _WIRE_MODEL_CONFIG


class AuthProject(BaseModel):
    """One project accessible to the signed-in session, from introspect."""

    id: int
    name: str = ""
    role: str = ""

    model_config = _WIRE_MODEL_CONFIG


class IntrospectResponse(BaseModel):
    """Response to ``GET /v1/auth/token/introspect``."""

    active: bool = True
    session_id: str = Field(default="", alias="sessionId")
    user: AuthUser | None = None
    projects: list[AuthProject] = Field(default_factory=list)
    expires_at: datetime | None = Field(default=None, alias="expiresAt")

    model_config = _WIRE_MODEL_CONFIG

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at_utc(cls, value: object) -> object:
        return _ensure_utc(value)


class DevicePollStatus(StrEnum):
    """Outcome of a single RFC 8628 device-token poll."""

    OK = "ok"
    PENDING = "pending"
    SLOW_DOWN = "slow_down"
    DENIED = "denied"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass(frozen=True)
class DevicePollResult:
    """Result of one `AuthClient.poll_device_token` call.

    A dataclass (not a bare tuple) so callers pattern-match on `.status`
    instead of positional indices, per the project's multi-value-return rule.
    """

    status: DevicePollStatus
    tokens: CliTokenResponse | None = None
    interval: int | None = None  # params.interval echoed on slow_down
    message: str = ""  # server-supplied detail for ERROR/DENIED


@dataclass(frozen=True)
class RevokeResult:
    """Outcome of POST /v1/auth/token/revoke.

    ``confirmed`` False means the server session may STILL BE LIVE -- callers
    must report that distinctly from a confirmed revoke instead of pretending
    logout fully succeeded.
    """

    confirmed: bool
    message: str = ""


@dataclass(frozen=True)
class SudoResult:
    """Outcome of POST /v1/auth/sudo (step-up authentication).

    Carries no token -- the sudo window is server-side state on the existing
    session, not a new credential. ``expires_at`` is the raw RFC 3339 string
    from the response, kept as-is since it is only ever displayed, never
    computed on.
    """

    verified: bool
    expires_at: str = ""
    timeout_seconds: int = 0


@dataclass(frozen=True)
class SudoChallengeResult:
    """Outcome of POST /v1/auth/sudo/challenge -- a WebAuthn ceremony to complete in a browser.

    ``options`` is the serialized `PublicKeyCredentialRequestOptions` the
    ceremony page feeds to `navigator.credentials.get({publicKey: options})`
    -- passed through opaquely, this CLI never inspects its contents.
    """

    challenge_token: str
    options: dict
    expires_in: int = 0


class PatItem(BaseModel):
    """A Personal Access Token's metadata. Never carries the secret value."""

    id: str
    name: str
    read_only: bool = Field(default=False, alias="readOnly")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    created_at: datetime | None = Field(default=None, alias="createdAt")

    model_config = _WIRE_MODEL_CONFIG


class PatCreateResult(BaseModel):
    """Response to POST /v1/auth/pat.

    ``access_token`` is the PAT's bearer value, shown exactly once by this
    response and never retrievable again -- callers must print it and not
    persist it (mirrors the "no token value is ever logged" rule the session
    login flow already follows).
    """

    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="Bearer", alias="tokenType")
    expires_in: int = Field(default=0, alias="expiresIn")
    pat: PatItem

    model_config = _WIRE_MODEL_CONFIG


class StackSession(BaseModel):
    """One persisted programmatic-auth session, keyed by normalized stack URL.

    This is the exact shape written to and read from ``auth.json``. Both
    token fields are plaintext -- the file is protected by 0600 permissions
    only, the same posture as the static Storage tokens already kept in
    ``config.json`` (deliberate RFC deviation, see
    docs/programmatic-auth-login-plan.md section 4.2).
    """

    stack_url: str
    session_id: str
    user_email: str = ""
    user_name: str = ""
    access_token: str
    refresh_token: str
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    created_at: datetime
    orphaned_session_ids: list[str] = Field(default_factory=list)

    # Forward compat: a newer kbagent writing an extra field must not break
    # an older kbagent reading the same auth.json.
    model_config = {"extra": "allow"}

    @field_validator("access_expires_at", "refresh_expires_at", "created_at", mode="before")
    @classmethod
    def _timestamps_utc(cls, value: object) -> object:
        return _ensure_utc(value)

    def access_token_fresh(
        self, *, margin: int = AUTH_REFRESH_MARGIN, now: datetime | None = None
    ) -> bool:
        """True when the access token is not within ``margin`` seconds of expiry.

        A missing ``access_expires_at`` (should not happen for a session
        written by this codebase, but defends against a hand-edited file) is
        treated as stale so callers refresh rather than trust an unknown TTL.
        """
        if self.access_expires_at is None:
            return False
        moment = now if now is not None else datetime.now(UTC)
        return (self.access_expires_at - moment).total_seconds() > margin

    def refresh_token_expired(self, *, now: datetime | None = None) -> bool:
        """True when the refresh token is known to be expired.

        Returns False when ``refresh_expires_at`` is None (unknown expiry --
        let the server be the authority and reject the refresh call itself
        rather than guessing).
        """
        if self.refresh_expires_at is None:
            return False
        moment = now if now is not None else datetime.now(UTC)
        return moment >= self.refresh_expires_at


class RefreshLease(BaseModel):
    """Claim on the right to refresh one stack's token pair, held across processes.

    A refresh token may only ever be presented to the server by one request at a
    time: a second, concurrent presentation of the same token is what triggers
    server-side family revocation (a hard logout). The lease is what enforces
    that without keeping ``auth.json.lock`` held across the network call --
    whoever holds it does the refresh, everyone else waits for the result.

    ``expires_at`` makes the claim self-healing: a holder that crashes, or one
    whose request was abandoned at the wall-clock ceiling, cannot wedge every
    other process forever.
    """

    holder: str
    expires_at: datetime

    model_config = {"extra": "allow"}

    @field_validator("expires_at", mode="before")
    @classmethod
    def _timestamps_utc(cls, value: object) -> object:
        return _ensure_utc(value)

    def is_live(self, *, now: datetime | None = None) -> bool:
        """True while the claim still stands, judged against the READER's clock.

        A lease crosses process boundaries, so its expiry has to be a wall-clock
        instant -- and the clock that wrote it may have been wrong. An expiry
        further out than `AUTH_REFRESH_LEASE_MAX_HORIZON` is therefore treated as
        unusable rather than honoured: no TTL this code grants reaches that far,
        so such a value can only come from a skewed clock or a hand-edited file,
        and honouring it would lock every later process out of the stack for the
        whole skew with no recovery short of `auth logout`.

        The opposite skew -- a claim written by a clock running *behind*, which
        every reader sees as already expired -- cannot be detected from the
        payload at all, and fails towards taking the lease over. That direction is
        the residual risk of anchoring liveness in wall clock, and it is the
        reason the abandon path exists: a token that may still be in flight is
        protected by a claim, not by a lock.
        """
        moment = now if now is not None else datetime.now(UTC)
        if self.expires_at <= moment:
            return False
        return self.expires_at <= moment + timedelta(seconds=AUTH_REFRESH_LEASE_MAX_HORIZON)


class AuthState(BaseModel):
    """Top-level shape of ``auth.json``: a version tag, sessions and refresh leases.

    ``refresh_leases`` is deliberately a sibling of ``sessions`` rather than a
    field on `StackSession`: a session write replaces the whole per-stack row, so
    a lease living inside it could be dropped by an unrelated `put_session`.
    """

    version: int = AUTH_STATE_VERSION
    sessions: dict[str, StackSession] = Field(default_factory=dict)
    refresh_leases: dict[str, RefreshLease] = Field(default_factory=dict)


__all__ = [
    "AuthProject",
    "AuthState",
    "AuthUser",
    "CliTokenResponse",
    "DeviceAuthorization",
    "DevicePollResult",
    "DevicePollStatus",
    "IntrospectResponse",
    "RefreshLease",
    "RevokeResult",
    "StackSession",
]
