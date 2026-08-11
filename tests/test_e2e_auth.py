"""End-to-end verification of programmatic-auth (browser login) sessions.

Exercises a REAL Keboola stack that has the `programmatic-auth` feature flag
(and its `pkce-authorization-flow` / `device-authorization-flow` children)
enabled, using a pre-provisioned session -- these tests never drive an
interactive browser login themselves (see `TestDeviceFlowManualVerification`
at the bottom for the one scenario that genuinely needs a human).

Two-tier gating, both via a SEPARATE set of env vars from the rest of this
suite's static-token tests (deliberately: a programmatic session is a
different credential shape and must never be conflated with `E2E_API_TOKEN`):

1. Base gate -- required for most tests in this file:
     - E2E_URL: stack URL (shared name with the rest of the E2E suite)
     - E2E_SESSION_REFRESH_TOKEN: a pre-provisioned session's refresh token
       (`kbc_rt_*`). NEVER the access token -- it is short-lived (~1h) and
       would be stale before a CI run even starts; the refresh token is the
       only credential these tests take on the command line.
     - E2E_SESSION_PROJECT_ID: numeric ID of a project the session can access
       (for the `X-KBC-ProjectId` header and the Manage API test).

2. Extra gate, only for the Query Service capability test:
     - E2E_SESSION_WORKSPACE_ID / E2E_SESSION_BRANCH_ID: a pre-provisioned
       workspace + branch on the session's project. Not auto-created here so
       this file never needs write access to config/workspace commands
       (out of scope -- those are covered by `TestFullE2E` in test_e2e.py).

3. Separate gate, only for `TestLoginPasswordCommand` (`auth login-password`
   itself, PR #565): E2E_URL_US_EAST4 (a GitHub Actions repo VARIABLE, not a
   secret -- it is a plain stack hostname, non-sensitive) plus
   E2E_LOGIN_EMAIL / E2E_LOGIN_PASSWORD secrets, and optionally
   E2E_LOGIN_TOTP_SECRET if that dedicated service account has TOTP-based
   MFA configured. Deliberately its own stack/account, kept apart from
   E2E_URL / E2E_SESSION_REFRESH_TOKEN below -- these tests drive real
   logins/logouts against it and must never disturb the shared session the
   other classes in this file depend on.

How to provision E2E_SESSION_REFRESH_TOKEN without ever typing it on the
command line or committing it anywhere:

    kbagent auth login --stack <stack-url>          # real browser login, once
    # then read the refresh_token out of the resulting auth.json (0600,
    # cleartext by design -- see docs/programmatic-auth-login-plan.md 4.2)
    # for that stack, and export it into the CI secret store as
    # E2E_SESSION_REFRESH_TOKEN. Never echo it into a log or terminal.
    #
    # Since v0.81.0 the same provisioning step can run fully unattended
    # given a service account's credentials, no browser required:
    #     kbagent auth login-password --stack <stack-url> \\
    #         --email "$SVC_EMAIL" --password-stdin <<< "$SVC_PASSWORD"

Run:
    E2E_URL=connection.keboola.com \\
    E2E_SESSION_REFRESH_TOKEN=kbc_rt_... \\
    E2E_SESSION_PROJECT_ID=12345 \\
    E2E_URL_US_EAST4=connection.us-east4.gcp.keboola.com \\
    E2E_LOGIN_EMAIL=svc@example.com \\
    E2E_LOGIN_PASSWORD=... \\
        make test-e2e-auth

`make test-e2e-auth` runs this file on its own; the default `make test-e2e`
runs it alongside the static-token suite. With the three env vars above unset
every test in here skips, so its presence in the default target costs nothing
on a machine with no session provisioned.

Every test carries `@pytest.mark.e2e` -- the default suite (`make test` / `-m
"not e2e"`) never runs them -- plus `@pytest.mark.e2e_auth`, which selects
exactly this file's tests out of a wider run (`-m e2e_auth`).

Every test's docstring states what it proves and what a failure would mean,
per the "one real call through each bearer-auth capability" requirement in
docs/programmatic-auth-login-plan.md section 7 and review finding NB-3 ("no
merge here advertises an unverified auth path").
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.auth.auth_client import AuthClient
from keboola_agent_cli.auth.models import StackSession
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.auth.token_provider import (
    BearerAuth,
    SessionTokenProvider,
    reset_provider_registry,
)
from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.manage_client import ManageClient
from keboola_agent_cli.models import normalize_stack_url

# ---------------------------------------------------------------------------
# Environment & skip logic -- deliberately separate from test_e2e.py's
# E2E_API_TOKEN gate (a programmatic session is a different credential shape).
# ---------------------------------------------------------------------------

ENV_URL = "E2E_URL"
ENV_SESSION_REFRESH_TOKEN = "E2E_SESSION_REFRESH_TOKEN"
ENV_SESSION_PROJECT_ID = "E2E_SESSION_PROJECT_ID"
ENV_SESSION_WORKSPACE_ID = "E2E_SESSION_WORKSPACE_ID"
ENV_SESSION_BRANCH_ID = "E2E_SESSION_BRANCH_ID"

HAS_SESSION_CREDENTIALS = bool(
    os.environ.get(ENV_URL)
    and os.environ.get(ENV_SESSION_REFRESH_TOKEN)
    and os.environ.get(ENV_SESSION_PROJECT_ID)
)

skip_without_session_credentials = pytest.mark.skipif(
    not HAS_SESSION_CREDENTIALS,
    reason=(
        f"Programmatic-auth E2E tests require {ENV_URL}, {ENV_SESSION_REFRESH_TOKEN} "
        f"and {ENV_SESSION_PROJECT_ID} (a pre-provisioned session on a stack with the "
        "programmatic-auth feature flag enabled). See the module docstring for how to "
        "provision one."
    ),
)

ENV_LOGIN_URL = "E2E_URL_US_EAST4"
ENV_LOGIN_EMAIL = "E2E_LOGIN_EMAIL"
ENV_LOGIN_PASSWORD = "E2E_LOGIN_PASSWORD"
ENV_LOGIN_TOTP_SECRET = "E2E_LOGIN_TOTP_SECRET"

HAS_LOGIN_CREDENTIALS = bool(
    os.environ.get(ENV_LOGIN_URL)
    and os.environ.get(ENV_LOGIN_EMAIL)
    and os.environ.get(ENV_LOGIN_PASSWORD)
)

skip_without_login_credentials = pytest.mark.skipif(
    not HAS_LOGIN_CREDENTIALS,
    reason=(
        f"`auth login-password` E2E tests require {ENV_LOGIN_URL}, {ENV_LOGIN_EMAIL} and "
        f"{ENV_LOGIN_PASSWORD} (a dedicated, least-privileged service account -- never "
        f"a real person's login). {ENV_LOGIN_TOTP_SECRET} is additionally required if "
        "that account has TOTP-based MFA configured; when absent, the account must "
        "have no MFA (or the login test skips the TOTP-specific assertion)."
    ),
)

HAS_WORKSPACE_CREDENTIALS = bool(
    HAS_SESSION_CREDENTIALS
    and os.environ.get(ENV_SESSION_WORKSPACE_ID)
    and os.environ.get(ENV_SESSION_BRANCH_ID)
)

skip_without_workspace_credentials = pytest.mark.skipif(
    not HAS_WORKSPACE_CREDENTIALS,
    reason=(
        f"Query Service bearer-auth coverage additionally requires "
        f"{ENV_SESSION_WORKSPACE_ID} and {ENV_SESSION_BRANCH_ID} (a pre-provisioned "
        "workspace + branch on the session's project)."
    ),
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stack_url() -> str:
    return normalize_stack_url(os.environ[ENV_URL])


@pytest.fixture
def project_id() -> int:
    return int(os.environ[ENV_SESSION_PROJECT_ID])


@pytest.fixture
def session_state_store(tmp_path: Path, stack_url: str) -> Generator[AuthStateStore, None, None]:
    """A throwaway `AuthStateStore` (its own `auth.json` under `tmp_path`) seeded
    with the pre-provisioned session from `E2E_SESSION_REFRESH_TOKEN`.

    `access_token` is deliberately left empty with no expiry, so the very
    first `get_access_token()` call is forced through a real proactive
    refresh -- these tests are only ever handed a refresh token on the
    command line, never a (short-lived) access token.

    Isolated per test (own `tmp_path`) so nothing here ever touches a real
    developer's `auth.json`, and the process-wide `SessionTokenProvider`
    registry is reset on teardown so tests don't leak a cached token into
    the next one.
    """
    store = AuthStateStore(tmp_path)
    store.put_session(
        StackSession(
            stack_url=stack_url,
            session_id="e2e-auth-probe",
            access_token="",
            refresh_token=os.environ[ENV_SESSION_REFRESH_TOKEN],
            access_expires_at=None,
            refresh_expires_at=None,
            created_at=datetime.now(UTC),
        )
    )
    yield store
    reset_provider_registry()


@pytest.fixture
def bearer_client(
    session_state_store: AuthStateStore, stack_url: str, project_id: int
) -> Generator[KeboolaClient, None, None]:
    """A `KeboolaClient` authenticated purely via `BearerAuth` (no Storage token).

    Forces the initial refresh eagerly (`provider.get_access_token()`) before
    handing the client back, so each capability test below exercises "does
    this endpoint accept an already-live `kbc_at_*` token", not incidentally
    re-testing the refresh dance on every single call.
    """
    provider = SessionTokenProvider(stack_url, session_state_store)
    provider.get_access_token()
    auth = BearerAuth(provider, project_id=project_id)
    client = KeboolaClient(stack_url, "", http_auth=auth)
    try:
        yield client
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 1. Bearer capability matrix (plan section 7, review NB-1 / NB-3)
# ---------------------------------------------------------------------------


@skip_without_session_credentials
@pytest.mark.e2e
@pytest.mark.e2e_auth
class TestBearerCapabilityMatrix:
    """One real call through each sub-client that inherits the `BearerAuth`
    hook: Storage, Queue, Query, Encryption, and Sync Actions.

    This is the whole point of this file: it turns "we think bearer auth
    works for these services" into a verified claim. Each test proves that
    ONE specific service accepts a `kbc_at_*` session access token over
    `Authorization: Bearer` -- not the `X-StorageApi-Token` / PAT-style
    static token every other E2E test in this repo uses. A failure in any one
    of these means the `http_auth` plumbing (`http_base.py`, `client/_core.py`)
    regressed for THAT service specifically -- the others may still be fine.
    """

    def test_storage_accepts_bearer_session(self, bearer_client: KeboolaClient) -> None:
        """GET /v2/storage/buckets through the main Storage client (no sub-client)."""
        buckets = bearer_client.list_buckets()
        assert isinstance(buckets, list)

    def test_queue_accepts_bearer_session(self, bearer_client: KeboolaClient) -> None:
        """GET jobs through the Queue sub-client (`queue.<stack>` host)."""
        jobs = bearer_client.list_jobs(limit=1)
        assert isinstance(jobs, list)

    def test_encryption_accepts_bearer_session(
        self, bearer_client: KeboolaClient, project_id: int
    ) -> None:
        """POST /encrypt through the Encryption sub-client (`encryption.<stack>` host)."""
        encrypted = bearer_client.encrypt_values(
            project_id, "keboola.ex-db-snowflake", {"#e2e-auth-probe": "e2e-value"}
        )
        assert encrypted["#e2e-auth-probe"].startswith("KBC::ProjectSecure::")

    def test_sync_actions_accepts_bearer_session(self, bearer_client: KeboolaClient) -> None:
        """POST /actions through the Sync Actions sub-client (`sync-actions.<stack>` host).

        The sync action itself (`testConnection` against a Snowflake extractor
        with no real credentials configured) is EXPECTED to fail at the
        business-logic layer -- that is not what this test checks. It checks
        that the call reaches the component and comes back with a normal
        success/error envelope rather than an authentication error, which
        would mean the bearer session was rejected before the request ever
        reached the component.
        """
        try:
            bearer_client.run_sync_action(
                "keboola.ex-db-snowflake",
                "testConnection",
                {"parameters": {}, "storage": {}},
            )
        except KeboolaApiError as exc:
            assert exc.error_code not in (ErrorCode.INVALID_TOKEN, ErrorCode.ACCESS_DENIED), (
                f"Sync action call was rejected at the auth layer, not the "
                f"business-logic layer: {exc.error_code} {exc.message}"
            )

    @skip_without_workspace_credentials
    def test_query_service_accepts_bearer_session(self, bearer_client: KeboolaClient) -> None:
        """POST a query through the Query Service sub-client (`query.<stack>` host)."""
        branch_id = int(os.environ[ENV_SESSION_BRANCH_ID])
        workspace_id = int(os.environ[ENV_SESSION_WORKSPACE_ID])
        job = bearer_client.submit_query(branch_id, workspace_id, ["SELECT 1"])
        assert job.get("id")


# ---------------------------------------------------------------------------
# 2. Reactive 401 -> refresh -> retry (token_provider.py's BearerAuth contract)
# ---------------------------------------------------------------------------


@skip_without_session_credentials
@pytest.mark.e2e
@pytest.mark.e2e_auth
class TestBearerReactiveRefresh:
    """A stale/invalid access token already in `auth.json` must trigger
    `BearerAuth`'s reactive 401 -> `force_refresh` -> retry-exactly-once path,
    completing the real call transparently.

    Proves the retry-once contract end to end against a REAL 401 from the
    server (not a mocked one). A failure here (call never succeeds, or more
    than one refresh happens) means `BearerAuth.auth_flow` or
    `SessionTokenProvider.force_refresh` regressed.
    """

    def test_stale_access_token_triggers_single_refresh_then_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stack_url: str,
        project_id: int,
    ) -> None:
        store = AuthStateStore(tmp_path)
        store.put_session(
            StackSession(
                stack_url=stack_url,
                session_id="e2e-stale-session",
                access_token="kbc_at_deliberately-stale-and-invalid",
                refresh_token=os.environ[ENV_SESSION_REFRESH_TOKEN],
                # Looks fresh to the LOCAL cache clock even though the server
                # will reject the value on the wire -- this is what forces
                # the 401 to happen for real instead of being pre-empted by
                # a proactive local refresh before any request is sent.
                access_expires_at=datetime.now(UTC) + timedelta(hours=1),
                refresh_expires_at=None,
                created_at=datetime.now(UTC),
            )
        )

        # Spy on the REAL AuthClient.refresh (not a mock) so the network call
        # still happens, while we count how many times it fires.
        refresh_calls: list[str] = []
        original_refresh = AuthClient.refresh

        def counting_refresh(self: AuthClient, refresh_token: str):
            refresh_calls.append(refresh_token)
            return original_refresh(self, refresh_token)

        monkeypatch.setattr(AuthClient, "refresh", counting_refresh)

        provider = SessionTokenProvider(stack_url, store)
        auth = BearerAuth(provider, project_id=project_id)
        try:
            with KeboolaClient(stack_url, "", http_auth=auth) as client:
                buckets = client.list_buckets()
        finally:
            reset_provider_registry()

        assert isinstance(buckets, list)
        assert len(refresh_calls) == 1, (
            f"Expected exactly one refresh call (the reactive 401 retry), got {len(refresh_calls)}"
        )


# ---------------------------------------------------------------------------
# 3. Manage API -- session-compatible (non-privileged) subset only
# ---------------------------------------------------------------------------


@skip_without_session_credentials
@pytest.mark.e2e
@pytest.mark.e2e_auth
class TestBearerManageApi:
    """A programmatic session is USER-scoped and does NOT carry admin/super
    Manage privileges (plan section 4.4). This proves the session-compatible
    SUBSET of the Manage API works over bearer auth.

    Deliberately does NOT assert that a privileged endpoint (e.g.
    `list_organization_projects`, which requires org-admin) succeeds with a
    session -- a normal E2E session credential should not have that
    privilege, and asserting success there would be testing the wrong thing.
    """

    def test_get_own_project_via_bearer_session(
        self, session_state_store: AuthStateStore, stack_url: str, project_id: int
    ) -> None:
        """`ManageClient.get_project` is documented to work for a project member
        without organization-admin rights -- exactly the shape of a session."""
        provider = SessionTokenProvider(stack_url, session_state_store)
        auth = BearerAuth(provider, project_id=project_id)
        with ManageClient(stack_url, "", http_auth=auth) as manage:
            project = manage.get_project(project_id)

        assert project["id"] == project_id


# ---------------------------------------------------------------------------
# 4. The CLI layer itself -- `auth status` end to end
# ---------------------------------------------------------------------------


@skip_without_session_credentials
@pytest.mark.e2e
@pytest.mark.e2e_auth
class TestAuthStatusCommand:
    """Drives the real `kbagent auth status` command against a real session.

    The capability matrix above builds `AuthClient` / `SessionTokenProvider` /
    `KeboolaClient` directly, which leaves the four new commands themselves
    covered only by mocked `CliRunner` tests (review NB-1). `auth status` is the
    one of the four that needs no browser and no interactivity, so it is the one
    that can close that gap unattended: it exercises the whole stack --
    `--config-dir` resolution, `AuthStateStore.from_config_store`, a real
    proactive refresh, introspection, exit-code mapping and `--json` shape.

    The other three stay out: `login` needs a human at a browser, `logout` would
    destroy the provisioned credential these tests share, and
    `register-projects` writes into `config.json`.
    """

    def test_status_reports_a_live_session(
        self, session_state_store: AuthStateStore, stack_url: str
    ) -> None:
        """Exit 0 and a live/refreshed verdict, from a seeded refresh token alone.

        The fixture stores no access token, so anything other than `refreshed`
        would mean the command reported health without proving it.
        """
        result = CliRunner().invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(session_state_store.config_dir),
                "auth",
                "status",
                "--stack",
                stack_url,
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["status"] in {"live", "refreshed"}
        assert data["stack_url"] == stack_url
        assert data["user_email"]
        assert data["accessible_projects"]

    def test_status_never_prints_a_token_value(
        self, session_state_store: AuthStateStore, stack_url: str
    ) -> None:
        """The one output that would be unrecoverable: a credential in a CI log."""
        result = CliRunner().invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(session_state_store.config_dir),
                "auth",
                "status",
                "--stack",
                stack_url,
            ],
        )

        assert result.exit_code == 0, result.output
        refresh_token = os.environ[ENV_SESSION_REFRESH_TOKEN]
        assert refresh_token not in result.output
        # The rotated pair the refresh just minted must not leak either.
        session = session_state_store.get_session(stack_url)
        assert session is not None
        assert session.access_token not in result.output
        assert session.refresh_token not in result.output

    def test_status_exits_3_for_a_stack_with_no_session(self, tmp_path: Path) -> None:
        """A missing session is exit 3 (auth), the code scripts branch on."""
        empty_config_dir = tmp_path / "empty"
        empty_config_dir.mkdir()

        result = CliRunner().invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(empty_config_dir),
                "auth",
                "status",
                "--stack",
                os.environ[ENV_URL],
            ],
        )

        assert result.exit_code == 3, result.output
        assert json.loads(result.output)["data"]["status"] == "missing"


# ---------------------------------------------------------------------------
# 4b. `auth login-password` -- the ONE auth command that IS fully unattended
# (PR #565 review, finding B1: unlike the PKCE/device flows above, this one
# has no exemption from CLAUDE.md convention #16 -- it needs no human and no
# browser, so it is the one that can and must be covered end to end).
# ---------------------------------------------------------------------------


@skip_without_login_credentials
@pytest.mark.e2e
@pytest.mark.e2e_auth
class TestLoginPasswordCommand:
    """Drives the real `kbagent auth login-password` command against a real
    stack, each test in its own throwaway `--config-dir` so nothing here
    touches the shared `E2E_SESSION_REFRESH_TOKEN` session other tests in
    this file depend on.
    """

    def test_login_and_register_projects_succeeds(self, tmp_path: Path) -> None:
        """No-MFA or TOTP-MFA login (whichever the service account requires)
        with `--register-projects` in the SAME call, followed by `auth
        status` reporting it live, then `auth logout --remove-projects` so no
        orphaned session or alias accumulates on the real stack across CI
        runs.

        Exercises the TOTP path specifically when E2E_LOGIN_TOTP_SECRET is
        set -- proving the seed-to-live-code-to-verified-session chain
        (C3/C4 in the PR #565 review) actually round-trips against a real
        stack, not just the unit-test fakes.

        Deliberately ONE login call, not two: the server consumes a TOTP
        time-slice on first submission and rejects any resubmission within
        the same ~30s window (the exact C4 constraint this PR documents), so
        a second `login-password` call moments later in the same test run
        would itself trip the failure it is supposed to guard against.
        """
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        args = [
            "--json",
            "--config-dir",
            str(config_dir),
            "auth",
            "login-password",
            "--email",
            os.environ[ENV_LOGIN_EMAIL],
            "--password",
            os.environ[ENV_LOGIN_PASSWORD],
            "--stack",
            os.environ[ENV_LOGIN_URL],
            "--register-projects",
        ]
        totp_secret = os.environ.get(ENV_LOGIN_TOTP_SECRET)
        if totp_secret:
            args += ["--totp-secret", totp_secret]

        login_result = CliRunner().invoke(app, args)
        assert login_result.exit_code == 0, login_result.output
        login_data = json.loads(login_result.output)["data"]
        assert login_data["method"] == "password"
        assert login_data["user_email"]
        assert login_data["registered_projects"]

        status_result = CliRunner().invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(config_dir),
                "auth",
                "status",
                "--stack",
                os.environ[ENV_LOGIN_URL],
            ],
        )
        assert status_result.exit_code == 0, status_result.output
        assert json.loads(status_result.output)["data"]["status"] in {"live", "refreshed"}

        logout_result = CliRunner().invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(config_dir),
                "auth",
                "logout",
                "--stack",
                os.environ[ENV_LOGIN_URL],
                "--remove-projects",
            ],
        )
        assert logout_result.exit_code == 0, logout_result.output

    def test_wrong_password_reports_invalid_credentials(self, tmp_path: Path) -> None:
        """Regression guard for PR #565 review finding C2: a wrong password
        must report a plain 'invalid email or password', exit 3 -- never the
        generic `INVALID_TOKEN` "Invalid or expired token (token: ****)"
        wording, which misdiagnoses the command's single most likely
        real-world failure."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()

        result = CliRunner().invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(config_dir),
                "auth",
                "login-password",
                "--email",
                os.environ[ENV_LOGIN_EMAIL],
                "--password",
                "definitely-the-wrong-password-e2e-probe",
                "--stack",
                os.environ[ENV_LOGIN_URL],
            ],
        )

        assert result.exit_code == 3, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "AUTH_FLOW_DENIED"
        assert "invalid email or password" in error["message"].lower()
        assert "invalid or expired token" not in error["message"].lower()


# ---------------------------------------------------------------------------
# 5. Device flow -- documented semi-manual scenario (cannot run unattended)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.e2e_auth
@pytest.mark.skip(
    reason=(
        "Manual-only: RFC 8628 device authorization requires a human to approve "
        "the login in a real browser. See this test's docstring for the exact "
        "steps an operator runs."
    )
)
def test_device_flow_manual_verification() -> None:
    """Documented manual verification of `kbagent auth login --device-code`.

    This cannot run unattended in CI: device authorization requires a HUMAN
    to open the verification URL, enter the user code, and approve the
    login. There is no way to automate "approve" that both (a) exercises the
    real poll loop against the real device-token endpoint and (b) requires no
    human -- scripting the approval would mean automating another party's
    browser session, out of scope for this CLI's own test suite.

    Manual steps for an operator verifying this after a change to
    `auth/device.py`, `AuthClient.start_device_authorization`, or
    `AuthClient.poll_device_token`, against a stack with the
    `device-authorization-flow` feature flag enabled:

    1. Run: `kbagent auth login --stack <stack-url> --device-code`
    2. Confirm the CLI prints both `verificationUri` and `userCode` (RFC 8628
       mandates showing both, even though `verificationUriComplete` is also
       best-effort opened automatically).
    3. Open the printed URL (or follow the auto-opened tab), enter the user
       code if it was not pre-filled, and approve the login as the intended
       user.
    4. Confirm the CLI reports success within a few `interval`-spaced polls,
       prints the signed-in user's email/name, and lists accessible
       projects.
    5. Run `kbagent auth status --stack <stack-url>` and confirm status
       "live", with a `session_id` matching step 4.
    6. Run `kbagent auth logout --stack <stack-url>` and confirm the CLI
       reports the remote session revoked, then that `auth status` reports
       "missing".

    A failure at step 2 means `on_prompt` regressed (`device.py`). A failure
    at step 4 means the poll loop (interval/slow_down handling) or
    `AuthClient.poll_device_token`'s envelope parsing regressed. A failure at
    step 5/6 means `AuthService.status`/`logout` regressed, not the device
    flow itself.
    """
