"""Business logic for ``kbagent oauth``: OAuth broker maintenance by L1 support.

Safety model (see the group's help and gotchas.md):

* Credential: a Manage API **application** token whose scopes are exactly
  ``["oauth:manage"]``. Anything wider is refused on add AND on every use, with
  no override. Such a token cannot reach Maintainers, Users, Organizations or
  Projects -- every one of those Manage endpoints needs an admin/session token
  or another scope (keboola/connection ``RequireManageAccess`` attributes).
* Storage: the token lives only in the macOS keychain, in an item that trusts
  no application, so each use raises a login-password dialog an agent cannot
  answer (``_oauth_keychain.py``).
* Secrets: the broker returns ``appSecret`` as ciphertext; kbagent still shows
  it only as ``<encrypted>``. A new Entra secret moves from ``az`` to ``op`` to
  the broker inside this process and is never printed.
* Writes: the command layer shows the plan and asks for a random code; there
  is no ``--yes``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from ..errors import ErrorCode, KeboolaApiError
from ..manage_client import ManageClient
from ..oauth_broker_client import OAuthBrokerClient
from ._oauth_keychain import KeychainError, OAuthKeychain
from ._oauth_tools import (
    AzureCli,
    OnePasswordCli,
    PasswordCredential,
    SecretCheck,
    ToolError,
    check_client_secret,
)

REQUIRED_SCOPES = ["oauth:manage"]
APPLICATION_TOKEN_TYPE = "super"
SECRET_FIELDS = ("appSecret", "appSecretDocker")
REDACTED = "<encrypted>"
MAX_SECRET_DAYS = 730

ManageClientFactory = Callable[[str, str], ManageClient]
BrokerClientFactory = Callable[[str, str], OAuthBrokerClient]


@dataclass
class StackHosts:
    """The two hosts of one stack. ``key`` (the oauth host) names the identity."""

    key: str
    connection_url: str
    oauth_url: str


def stack_hosts(stack: str) -> StackHosts:
    """Accept a Connection host, an oauth host or a URL; return both hosts.

    ``connection.keboola.com``, ``https://oauth.keboola.com/manage`` and
    ``keboola.com`` all give ``oauth.keboola.com``. Works for
    ``*.keboola.cloud`` single-tenant stacks, Azure and GCP alike.
    """
    raw = stack.strip()
    host = (urlparse(raw).hostname if "://" in raw else raw.split("/")[0]) or ""
    host = host.lower().rstrip(".")
    for prefix in ("connection.", "oauth."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    if "." not in host:
        raise KeboolaApiError(
            f"'{stack}' is not a stack host (expected e.g. connection.keboola.com).",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return StackHosts(
        key=f"oauth.{host}",
        connection_url=f"https://connection.{host}",
        oauth_url=f"https://oauth.{host}",
    )


def check_token_scope(verify: dict[str, Any]) -> dict[str, Any]:
    """Refuse any token that is not an oauth:manage-only application token."""
    problems = []
    if verify.get("type") != APPLICATION_TOKEN_TYPE:
        problems.append(f"its type is '{verify.get('type')}', not an application token")
    scopes = sorted(verify.get("scopes") or [])
    if scopes != REQUIRED_SCOPES:
        problems.append(f"its scopes are {scopes}, not exactly {REQUIRED_SCOPES}")
    if verify.get("isExpired"):
        problems.append("it is expired")
    if verify.get("isDisabled"):
        problems.append("it is disabled")
    if problems:
        raise KeboolaApiError(
            "kbagent oauth refuses this Manage token: "
            + "; ".join(problems)
            + ". Ask a super-admin for an application token with only the oauth:manage scope.",
            error_code=ErrorCode.OAUTH_TOKEN_TOO_BROAD,
        )
    return {
        "token_id": verify.get("id"),
        "description": verify.get("description", ""),
        "expires": verify.get("expires"),
        "scopes": scopes,
    }


def redact_consumer(consumer: dict[str, Any]) -> dict[str, Any]:
    """Copy of a broker consumer with every secret field replaced."""
    return {
        key: (REDACTED if key in SECRET_FIELDS and value else value)
        for key, value in consumer.items()
    }


@dataclass
class LoadedToken:
    """A token read from the keychain and its scope check result."""

    token: str = field(repr=False)
    info: dict[str, Any] = field(default_factory=dict)


def _fingerprint(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()[:12]


@dataclass
class RotationPlan:
    """Everything ``apply_rotation`` will do, computed by read-only calls."""

    hosts: StackHosts
    component_id: str
    app_id: str
    app_name: str
    tenant_id: str
    az_user: str
    op_vault_id: str
    op_vault_name: str
    end_date: str
    current_for_component: list[PasswordCredential]
    other_credentials: int
    secret_fingerprint: str
    token: str = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        """The plan as shown to the human (no token, no secret)."""
        return {
            "stack": self.hosts.key,
            "component_id": self.component_id,
            "app_id": self.app_id,
            "app_name": self.app_name,
            "tenant_id": self.tenant_id,
            "az_user": self.az_user,
            "writes": [
                {
                    "target": "entra",
                    "action": f"add a client secret to app {self.app_id} "
                    f"(displayName '{self.component_id}', expires {self.end_date})",
                },
                {
                    "target": "1password",
                    "action": f"create an item with the new secret in vault "
                    f"'{self.op_vault_name}' ({self.op_vault_id})",
                },
                {
                    "target": "broker",
                    "action": f"PATCH {self.hosts.oauth_url}/manage/{self.component_id} "
                    "appSecret: <changed>",
                },
            ],
            "old_secrets_kept": [
                {"key_id": c.key_id, "expires": c.end_date} for c in self.current_for_component
            ],
            "other_secrets_on_app": self.other_credentials,
        }


class OAuthService:
    """``kbagent oauth`` identities, consumer reads and the Entra secret rotation."""

    def __init__(
        self,
        keychain: OAuthKeychain,
        manage_client_factory: ManageClientFactory = ManageClient,
        broker_client_factory: BrokerClientFactory = OAuthBrokerClient,
        azure: AzureCli | None = None,
        onepassword: OnePasswordCli | None = None,
        secret_checker: Callable[[str, str, str], SecretCheck] = check_client_secret,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._keychain = keychain
        self._manage = manage_client_factory
        self._broker = broker_client_factory
        self._azure = azure or AzureCli()
        self._op = onepassword or OnePasswordCli()
        self._check_secret = secret_checker
        self._today = today

    # ----- identities -----

    def add_identity(self, stack: str, token: str) -> dict[str, Any]:
        """Check the token (scope + broker probe), then store it in the keychain."""
        hosts = stack_hosts(stack)
        info = self._verify_token(hosts, token)
        with self._broker(hosts.oauth_url, token) as broker:
            consumers = broker.list_consumers()
        comment = f"token {info['token_id']} ({info['description']}), expires {info['expires']}"
        self._keychain.store(hosts.key, token, comment)
        return {"status": "stored", "stack": hosts.key, **info, "consumers": len(consumers)}

    def verify_identity(self, stack: str) -> dict[str, Any]:
        """Re-run every check on the stored token."""
        hosts = stack_hosts(stack)
        loaded = self._load_token(hosts)
        with self._broker(hosts.oauth_url, loaded.token) as broker:
            consumers = broker.list_consumers()
        return {"status": "ok", "stack": hosts.key, **loaded.info, "consumers": len(consumers)}

    def _verify_token(self, hosts: StackHosts, token: str) -> dict[str, Any]:
        with self._manage(hosts.connection_url, token) as manage:
            return check_token_scope(manage.verify_token())

    def _load_token(self, hosts: StackHosts) -> LoadedToken:
        if self._keychain.find(hosts.key) is None:
            raise KeychainError(
                f"No token stored for {hosts.key}. "
                f"Run `kbagent oauth identity add --stack {hosts.key}`."
            )
        apps = self._keychain.trusted_apps(hosts.key)
        if apps is None or apps:
            what = "could not be read" if apps is None else f"trusts {', '.join(apps)}"
            raise KeychainError(
                f"The keychain item for {hosts.key} {what}, so macOS may release the token "
                "without asking. Run `kbagent oauth identity add` again to replace it, and "
                "never click 'Always Allow' in the keychain dialog.",
                ErrorCode.OAUTH_KEYCHAIN_UNPROTECTED,
            )
        token = self._keychain.read(hosts.key)
        return LoadedToken(token=token, info=self._verify_token(hosts, token))

    # ----- consumers -----

    def get_consumer(self, stack: str, component_id: str) -> dict[str, Any]:
        hosts = stack_hosts(stack)
        token = self._load_token(hosts).token
        with self._broker(hosts.oauth_url, token) as broker:
            consumer = broker.get_consumer(component_id)
        return {"stack": hosts.key, "consumer": redact_consumer(consumer)}

    # ----- rotation -----

    def prepare_rotation(
        self, stack: str, component_id: str, app_id: str, op_vault: str, expires_in_days: int
    ) -> RotationPlan:
        """Read-only preflight. Raises before any write when a check fails."""
        if not 1 <= expires_in_days <= MAX_SECRET_DAYS:
            raise _preflight(f"--expires-in-days must be between 1 and {MAX_SECRET_DAYS}.")
        hosts = stack_hosts(stack)
        token = self._load_token(hosts).token
        with self._broker(hosts.oauth_url, token) as broker:
            consumer = broker.get_consumer(component_id)
        if str(consumer.get("appKey", "")).lower() != app_id.lower():
            raise _preflight(
                f"The broker consumer {component_id} on {hosts.key} uses appKey "
                f"'{consumer.get('appKey')}', not app {app_id}. Nothing was changed."
            )
        try:
            account = self._azure.account()
            app = self._azure.app(app_id)
            credentials = self._azure.password_credentials(app_id)
            self._op.whoami()
            vault = self._op.vault(op_vault)
        except ToolError as exc:
            raise _preflight(
                f"{exc.message} -- sign in to `az` (with the role that may change this app's "
                "secrets active) and to `op`, then run again. Nothing was changed."
            ) from None
        current = [c for c in credentials if c.display_name == component_id]
        end_date = (self._today() + timedelta(days=expires_in_days)).isoformat()
        return RotationPlan(
            hosts=hosts,
            component_id=component_id,
            app_id=str(app.get("appId", app_id)),
            app_name=str(app.get("displayName", "")),
            tenant_id=str(account.get("tenantId", "")),
            az_user=str((account.get("user") or {}).get("name", "")),
            op_vault_id=str(vault.get("id", "")),
            op_vault_name=str(vault.get("name", "")),
            end_date=end_date,
            current_for_component=current,
            other_credentials=len(credentials) - len(current),
            secret_fingerprint=_fingerprint(consumer.get("appSecret")),
            token=token,
        )

    def apply_rotation(self, plan: RotationPlan) -> dict[str, Any]:
        """Run the writes in order: Entra, 1Password, broker; then verify.

        1Password comes before the broker so the broker never holds a secret
        that is saved nowhere. The old secret is never removed here.
        """
        before = {c.key_id for c in self._azure.password_credentials(plan.app_id)}
        try:
            secret = self._azure.add_password(plan.app_id, plan.component_id, plan.end_date)
        except ToolError as exc:
            raise _incomplete(
                "entra",
                plan,
                "unknown",
                f"{exc.message}. Check `az ad app credential list --id {plan.app_id}` for a new "
                f"secret named '{plan.component_id}'; 1Password and the broker were not changed.",
            ) from None
        new_key_id = self._find_new_key_id(plan.app_id, before)
        try:
            item_id = self._op.create_password_item(
                plan.op_vault_id,
                f"{plan.component_id} @ {plan.hosts.key} - Entra secret {new_key_id[:8]}",
                secret,
                _item_notes(plan, new_key_id, self._today().isoformat()),
            )
        except ToolError as exc:
            raise _incomplete(
                "1password",
                plan,
                new_key_id,
                f"{exc.message}. The new secret {new_key_id} exists in Entra but is saved "
                f"nowhere. Delete it: az ad app credential delete --id {plan.app_id} "
                f"--key-id {new_key_id}. Then run the rotation again.",
            ) from None
        try:
            with self._broker(plan.hosts.oauth_url, plan.token) as broker:
                broker.patch_consumer(plan.component_id, {"appSecret": secret})
                after = broker.get_consumer(plan.component_id)
        except KeboolaApiError as exc:
            raise _incomplete(
                "broker",
                plan,
                new_key_id,
                f"{exc.message}. The new secret is in Entra and in 1Password item {item_id}; "
                "the broker still uses the old secret, which stays valid. Patch the broker "
                "with the value from that item.",
                op_item_id=item_id,
            ) from None
        check = self._check_secret(plan.tenant_id, plan.app_id, secret)
        del secret
        broker_changed = _fingerprint(after.get("appSecret")) != plan.secret_fingerprint
        app_key_same = str(after.get("appKey", "")).lower() == plan.app_id.lower()
        verified = broker_changed and app_key_same and check.ok
        return {
            "status": "rotated" if verified else "rotated_unverified",
            "stack": plan.hosts.key,
            "component_id": plan.component_id,
            "app_id": plan.app_id,
            "new_key_id": new_key_id,
            "new_secret_expires": plan.end_date,
            "op_vault": plan.op_vault_name,
            "op_item_id": item_id,
            "broker_secret_changed": broker_changed,
            "broker_app_key_unchanged": app_key_same,
            "entra_secret_check": {"ok": check.ok, "detail": check.detail},
            "old_secrets_still_valid": [c.key_id for c in plan.current_for_component],
        }

    def _find_new_key_id(self, app_id: str, before: set[str]) -> str:
        try:
            after = self._azure.password_credentials(app_id)
        except ToolError:
            return "unknown"
        new = [c.key_id for c in after if c.key_id not in before]
        return new[0] if len(new) == 1 else "unknown"


def _item_notes(plan: RotationPlan, key_id: str, today: str) -> str:
    return "\n".join(
        [
            f"Entra app: {plan.app_id} ({plan.app_name})",
            f"Key ID: {key_id}",
            f"Component: {plan.component_id}",
            f"Broker: {plan.hosts.oauth_url}",
            f"Expires: {plan.end_date}",
            f"Created by kbagent oauth rotate-azure-secret on {today} ({plan.az_user}).",
        ]
    )


def _preflight(message: str) -> KeboolaApiError:
    return KeboolaApiError(message, error_code=ErrorCode.OAUTH_PREFLIGHT_FAILED)


def _incomplete(
    stage: str, plan: RotationPlan, key_id: str, message: str, op_item_id: str = ""
) -> KeboolaApiError:
    return KeboolaApiError(
        message,
        error_code=ErrorCode.OAUTH_ROTATION_INCOMPLETE,
        details={
            "stage": stage,
            "stack": plan.hosts.key,
            "component_id": plan.component_id,
            "app_id": plan.app_id,
            "new_key_id": key_id,
            "op_item_id": op_item_id,
        },
    )
