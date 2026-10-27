"""Tests for services/oauth_service.py, its keychain store and its tool wrappers."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from typing import Any, Self, cast

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services._oauth_keychain import (
    KeychainError,
    OAuthKeychain,
    _decrypt_entry_apps,
)
from keboola_agent_cli.services._oauth_tools import (
    AzureCli,
    OnePasswordCli,
    PasswordCredential,
    SecretCheck,
    ToolError,
    check_client_secret,
)
from keboola_agent_cli.services.oauth_service import (
    OAuthService,
    check_token_scope,
    redact_consumer,
    stack_hosts,
)

TOKEN = "123-oauthmanagetoken"
SECRET = "CANARY-new-client-secret-value"
APP_ID = "d7b1abc4-a7de-4005-a40e-2f2aa46987df"
COMPONENT = "kds-team.app-powerbi-refresh"
GOOD_VERIFY = {
    "id": 42,
    "description": "L1 oauth",
    "type": "super",
    "scopes": ["oauth:manage"],
    "isExpired": False,
    "isDisabled": False,
    "expires": None,
}


# ----- fakes -----


class FakeKeychain:
    def __init__(self, token: str | None = TOKEN, trusted: list[str] | None = None) -> None:
        self.token = token
        self.trusted: list[str] | None = [] if trusted is None else trusted
        self.stored: dict[str, str] = {}

    def find(self, host: str) -> Any:
        return object() if self.token is not None else None

    def trusted_apps(self, host: str) -> list[str] | None:
        return self.trusted

    def read(self, host: str) -> str:
        assert self.token is not None
        return self.token

    def store(self, host: str, token: str, comment: str) -> None:
        self.stored[host] = token


class FakeManage:
    def __init__(self, verify: dict[str, Any]) -> None:
        self.verify = verify

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def verify_token(self) -> dict[str, Any]:
        return self.verify


class FakeBroker:
    def __init__(self, consumer: dict[str, Any], patch_error: Exception | None = None) -> None:
        self.consumer = dict(consumer)
        self.patch_error = patch_error
        self.patches: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def list_consumers(self) -> list[dict[str, Any]]:
        return [self.consumer]

    def get_consumer(self, component_id: str) -> dict[str, Any]:
        return dict(self.consumer)

    def patch_consumer(self, component_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.patch_error:
            raise self.patch_error
        self.patches.append(body)
        self.consumer["appSecret"] = "KBC::ciphertext-new"
        return dict(self.consumer)


class FakeAzure:
    def __init__(self, add_error: Exception | None = None) -> None:
        self.creds = [
            PasswordCredential("old-key", COMPONENT, "2026-11-01T14:54:00Z"),
            PasswordCredential("other-key", "kds-team.wr-powerbi", "2026-11-01T14:54:00Z"),
        ]
        self.add_error = add_error
        self.calls: list[str] = []

    def account(self) -> dict[str, Any]:
        return {"tenantId": "tenant-1", "user": {"name": "l1@example.com"}}

    def app(self, app_id: str) -> dict[str, Any]:
        return {"appId": app_id, "displayName": "Power BI"}

    def password_credentials(self, app_id: str) -> list[PasswordCredential]:
        return list(self.creds)

    def add_password(self, app_id: str, display_name: str, end_date: str) -> str:
        self.calls.append(f"add:{display_name}:{end_date}")
        if self.add_error:
            raise self.add_error
        self.creds.append(PasswordCredential("new-key-1234", display_name, end_date))
        return SECRET


class FakeOp:
    def __init__(self, create_error: Exception | None = None) -> None:
        self.create_error = create_error
        self.items: list[dict[str, str]] = []

    def whoami(self) -> dict[str, Any]:
        return {"email": "l1@example.com"}

    def vault(self, vault: str) -> dict[str, Any]:
        return {"id": "vault-id", "name": "[PROD] OAuth"}

    def create_password_item(self, vault: str, title: str, secret: str, notes: str) -> str:
        if self.create_error:
            raise self.create_error
        self.items.append({"vault": vault, "title": title, "secret": secret})
        return "item-1"


def _service(
    *,
    keychain: FakeKeychain | None = None,
    verify: dict[str, Any] | None = None,
    broker: FakeBroker | None = None,
    azure: FakeAzure | None = None,
    op: FakeOp | None = None,
    check: SecretCheck | None = None,
) -> OAuthService:
    broker = broker or FakeBroker(
        {"componentId": COMPONENT, "appKey": APP_ID, "appSecret": "KBC::old"}
    )
    manage = FakeManage(verify or GOOD_VERIFY)
    return OAuthService(
        keychain=cast(Any, keychain or FakeKeychain()),
        manage_client_factory=cast(Any, lambda url, token: manage),
        broker_client_factory=cast(Any, lambda url, token: broker),
        azure=cast(Any, azure or FakeAzure()),
        onepassword=cast(Any, op or FakeOp()),
        secret_checker=lambda tenant, app, secret: check or SecretCheck(True, "ok"),
        today=lambda: date(2026, 10, 27),
    )


# ----- hosts, scope, redaction -----


@pytest.mark.parametrize(
    "stack",
    [
        "connection.keboola.com",
        "oauth.keboola.com",
        "https://oauth.keboola.com/manage",
        "https://connection.keboola.com/",
        "keboola.com",
    ],
)
def test_stack_hosts_normalises_every_form(stack: str) -> None:
    hosts = stack_hosts(stack)
    assert hosts.key == "oauth.keboola.com"
    assert hosts.connection_url == "https://connection.keboola.com"
    assert hosts.oauth_url == "https://oauth.keboola.com"


def test_stack_hosts_single_tenant() -> None:
    assert (
        stack_hosts("connection.acme.keboola.cloud").oauth_url == "https://oauth.acme.keboola.cloud"
    )


def test_stack_hosts_rejects_garbage() -> None:
    with pytest.raises(KeboolaApiError):
        stack_hosts("localhost")


def test_check_token_scope_accepts_oauth_manage_only() -> None:
    info = check_token_scope(GOOD_VERIFY)
    assert info["token_id"] == 42
    assert info["scopes"] == ["oauth:manage"]


@pytest.mark.parametrize(
    "change",
    [
        {"type": "admin"},
        {"scopes": ["oauth:manage", "manage:storage-tokens"]},
        {"scopes": []},
        {"isExpired": True},
        {"isDisabled": True},
    ],
)
def test_check_token_scope_refuses_anything_wider(change: dict[str, Any]) -> None:
    with pytest.raises(KeboolaApiError) as err:
        check_token_scope({**GOOD_VERIFY, **change})
    assert err.value.error_code == ErrorCode.OAUTH_TOKEN_TOO_BROAD


def test_redact_consumer_hides_both_secret_fields() -> None:
    out = redact_consumer({"appKey": "k", "appSecret": "KBC::x", "appSecretDocker": "KBC::y"})
    assert out == {"appKey": "k", "appSecret": "<encrypted>", "appSecretDocker": "<encrypted>"}


# ----- identities -----


def test_add_identity_stores_after_checks() -> None:
    keychain = FakeKeychain(token=None)
    result = _service(keychain=keychain).add_identity("connection.keboola.com", TOKEN)
    assert keychain.stored == {"oauth.keboola.com": TOKEN}
    assert result["status"] == "stored"
    assert TOKEN not in json.dumps(result)


def test_add_identity_refuses_broad_token_and_stores_nothing() -> None:
    keychain = FakeKeychain(token=None)
    service = _service(keychain=keychain, verify={**GOOD_VERIFY, "type": "admin"})
    with pytest.raises(KeboolaApiError):
        service.add_identity("keboola.com", TOKEN)
    assert keychain.stored == {}


def test_load_token_missing_identity() -> None:
    with pytest.raises(KeychainError) as err:
        _service(keychain=FakeKeychain(token=None)).verify_identity("keboola.com")
    assert err.value.error_code == ErrorCode.OAUTH_IDENTITY_MISSING


@pytest.mark.parametrize("trusted", [["/usr/bin/security"], None])
def test_load_token_refuses_widened_or_unreadable_acl(trusted: list[str] | None) -> None:
    keychain = FakeKeychain()
    keychain.trusted = trusted
    with pytest.raises(KeychainError) as err:
        _service(keychain=keychain).verify_identity("keboola.com")
    assert err.value.error_code == ErrorCode.OAUTH_KEYCHAIN_UNPROTECTED


def test_verify_identity_rechecks_scope_on_use() -> None:
    service = _service(verify={**GOOD_VERIFY, "scopes": ["oauth:manage", "projects:read"]})
    with pytest.raises(KeboolaApiError) as err:
        service.verify_identity("keboola.com")
    assert err.value.error_code == ErrorCode.OAUTH_TOKEN_TOO_BROAD


def test_get_consumer_is_redacted() -> None:
    result = _service().get_consumer("keboola.com", COMPONENT)
    assert result["consumer"]["appSecret"] == "<encrypted>"


# ----- rotation -----


def test_prepare_rotation_plan() -> None:
    plan = _service().prepare_rotation("keboola.com", COMPONENT, APP_ID, "vault", 730)
    data = plan.as_dict()
    assert data["old_secrets_kept"] == [{"key_id": "old-key", "expires": "2026-11-01T14:54:00Z"}]
    assert data["other_secrets_on_app"] == 1
    assert plan.end_date == "2028-10-26"
    assert [w["target"] for w in data["writes"]] == ["entra", "1password", "broker"]
    assert TOKEN not in json.dumps(data)


def test_prepare_rotation_refuses_wrong_app() -> None:
    broker = FakeBroker({"componentId": COMPONENT, "appKey": "other-app", "appSecret": "x"})
    with pytest.raises(KeboolaApiError) as err:
        _service(broker=broker).prepare_rotation("keboola.com", COMPONENT, APP_ID, "v", 730)
    assert err.value.error_code == ErrorCode.OAUTH_PREFLIGHT_FAILED


def test_prepare_rotation_refuses_when_a_tool_is_not_signed_in() -> None:
    class SignedOutOp(FakeOp):
        def whoami(self) -> dict[str, Any]:
            raise ToolError("op", "not signed in")

    op = SignedOutOp()
    with pytest.raises(KeboolaApiError) as err:
        _service(op=op).prepare_rotation("keboola.com", COMPONENT, APP_ID, "v", 730)
    assert err.value.error_code == ErrorCode.OAUTH_PREFLIGHT_FAILED


def test_prepare_rotation_refuses_bad_lifetime() -> None:
    with pytest.raises(KeboolaApiError):
        _service().prepare_rotation("keboola.com", COMPONENT, APP_ID, "v", 0)


def test_apply_rotation_happy_path() -> None:
    broker = FakeBroker({"componentId": COMPONENT, "appKey": APP_ID, "appSecret": "KBC::old"})
    op = FakeOp()
    azure = FakeAzure()
    service = _service(broker=broker, op=op, azure=azure)
    plan = service.prepare_rotation("keboola.com", COMPONENT, APP_ID, "vault", 730)
    result = service.apply_rotation(plan)
    assert result["status"] == "rotated"
    assert result["new_key_id"] == "new-key-1234"
    assert result["old_secrets_still_valid"] == ["old-key"]
    assert azure.calls == [f"add:{COMPONENT}:2028-10-26"]
    assert op.items[0]["secret"] == SECRET
    assert broker.patches == [{"appSecret": SECRET}]
    assert SECRET not in json.dumps(result)


def test_apply_rotation_unverified_when_entra_rejects() -> None:
    service = _service(check=SecretCheck(False, "HTTP 401 invalid_client"))
    plan = service.prepare_rotation("keboola.com", COMPONENT, APP_ID, "vault", 730)
    result = service.apply_rotation(plan)
    assert result["status"] == "rotated_unverified"


def test_apply_rotation_entra_failure_changes_nothing_else() -> None:
    broker = FakeBroker({"componentId": COMPONENT, "appKey": APP_ID, "appSecret": "KBC::old"})
    op = FakeOp()
    service = _service(broker=broker, op=op, azure=FakeAzure(add_error=ToolError("az", "403")))
    plan = service.prepare_rotation("keboola.com", COMPONENT, APP_ID, "vault", 730)
    with pytest.raises(KeboolaApiError) as err:
        service.apply_rotation(plan)
    assert err.value.error_code == ErrorCode.OAUTH_ROTATION_INCOMPLETE
    assert err.value.details["stage"] == "entra"
    assert op.items == [] and broker.patches == []


def test_apply_rotation_1password_failure_names_the_key_to_delete() -> None:
    broker = FakeBroker({"componentId": COMPONENT, "appKey": APP_ID, "appSecret": "KBC::old"})
    service = _service(broker=broker, op=FakeOp(create_error=ToolError("op", "locked")))
    plan = service.prepare_rotation("keboola.com", COMPONENT, APP_ID, "vault", 730)
    with pytest.raises(KeboolaApiError) as err:
        service.apply_rotation(plan)
    assert err.value.details["stage"] == "1password"
    assert "new-key-1234" in err.value.message
    assert SECRET not in err.value.message
    assert broker.patches == []


def test_apply_rotation_broker_failure_points_at_the_item() -> None:
    broker = FakeBroker(
        {"componentId": COMPONENT, "appKey": APP_ID, "appSecret": "KBC::old"},
        patch_error=KeboolaApiError("HTTP 500", status_code=500),
    )
    service = _service(broker=broker)
    plan = service.prepare_rotation("keboola.com", COMPONENT, APP_ID, "vault", 730)
    with pytest.raises(KeboolaApiError) as err:
        service.apply_rotation(plan)
    assert err.value.details["stage"] == "broker"
    assert err.value.details["op_item_id"] == "item-1"
    assert SECRET not in err.value.message


# ----- keychain -----

DUMP_SAFE = """keychain: "/Users/x/Library/Keychains/login.keychain-db"
class: "genp"
attributes:
    "acct"<blob>="oauth.keboola.com"
    "svce"<blob>="kbagent-oauth"
access: 3 entries
    entry 0:
        authorizations (1): encrypt
        applications: <null>
    entry 1:
        authorizations (6): decrypt derive export_clear export_wrapped mac sign
        don't-require-password
        applications (0):
    entry 2:
        authorizations (1): change_acl
        applications (0):
"""

DUMP_ALWAYS_ALLOWED = DUMP_SAFE.replace(
    "        applications (0):\n    entry 2:",
    "        applications (1):\n            0: /usr/bin/security (OK)\n"
    '                requirement: identifier "com.apple.security"\n    entry 2:',
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_decrypt_entry_apps() -> None:
    block = DUMP_SAFE.split("keychain: ")[1]
    assert _decrypt_entry_apps(block) == []
    widened = DUMP_ALWAYS_ALLOWED.split("keychain: ")[1]
    assert _decrypt_entry_apps(widened) == ["/usr/bin/security"]
    anyone = block.replace("applications (0):\n    entry 2", "applications: <null>\n    entry 2", 1)
    assert _decrypt_entry_apps(anyone) == ["<any application>"]


def test_keychain_trusted_apps_finds_the_item() -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(DUMP_ALWAYS_ALLOWED)

    assert OAuthKeychain(runner=run, platform="darwin").trusted_apps("oauth.keboola.com") == [
        "/usr/bin/security"
    ]


def test_keychain_store_passes_token_on_stdin_only() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs.get("input")))
        if args[1] == "find-generic-password":
            return _completed('    "icmt"<blob>="token 42"\n')
        return _completed()

    OAuthKeychain(runner=run, platform="darwin").store("oauth.keboola.com", TOKEN, "token 42")
    add = [c for c in calls if c[0][1:] == ["-i"]]
    assert len(add) == 1
    assert TOKEN in (add[0][1] or "") and '-T ""' in (add[0][1] or "")
    assert all(TOKEN not in " ".join(args) for args, _ in calls)


def test_keychain_store_rejects_odd_token() -> None:
    keychain = OAuthKeychain(runner=lambda *a, **k: _completed(), platform="darwin")
    with pytest.raises(KeychainError):
        keychain.store("oauth.keboola.com", 'bad" -w x', "c")


def test_keychain_read_missing_item() -> None:
    keychain = OAuthKeychain(runner=lambda *a, **k: _completed(returncode=44), platform="darwin")
    with pytest.raises(KeychainError) as err:
        keychain.read("oauth.keboola.com")
    assert err.value.error_code == ErrorCode.OAUTH_IDENTITY_MISSING


def test_keychain_refuses_other_platforms() -> None:
    with pytest.raises(KeychainError):
        OAuthKeychain(runner=lambda *a, **k: _completed(), platform="linux").find("x")


# ----- tools -----


def test_azure_add_password_keeps_secret_off_argv() -> None:
    seen: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(args)
        return _completed(json.dumps({"appId": APP_ID, "password": SECRET}))

    assert AzureCli(runner=run).add_password(APP_ID, COMPONENT, "2028-10-26") == SECRET
    assert "--append" in seen[0] and SECRET not in " ".join(seen[0])


def test_azure_error_never_echoes_stdout() -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout=SECRET, stderr="Forbidden")

    with pytest.raises(ToolError) as err:
        AzureCli(runner=run).add_password(APP_ID, COMPONENT, "2028-10-26")
    assert SECRET not in err.value.message


def test_op_create_item_sends_secret_on_stdin() -> None:
    seen: list[tuple[list[str], str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((args, kwargs["input"]))
        return _completed(json.dumps({"id": "item-1"}))

    assert OnePasswordCli(runner=run).create_password_item("v", "t", SECRET, "n") == "item-1"
    args, stdin = seen[0]
    assert args[-1] == "-" and SECRET not in " ".join(args)
    assert json.loads(stdin)["fields"][0]["value"] == SECRET


class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def json(self) -> dict[str, str]:
        return {"error": "invalid_client"}


def test_check_client_secret_retries_until_entra_accepts() -> None:
    statuses = iter([401, 401, 200])
    result = check_client_secret(
        "t",
        APP_ID,
        SECRET,
        post=cast(Any, lambda *a, **k: _Response(next(statuses))),
        sleep=lambda s: None,
    )
    assert result.ok


def test_check_client_secret_gives_up() -> None:
    result = check_client_secret(
        "t",
        APP_ID,
        SECRET,
        wait_seconds=10,
        post=cast(Any, lambda *a, **k: _Response(401)),
        sleep=lambda s: None,
    )
    assert not result.ok and "401" in result.detail
