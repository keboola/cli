"""Body-builders, redaction helpers, and git/version resolution for
:mod:`data_app_service`.

Extracted from ``data_app_service.py`` (which is over its file-size budget)
so the service module holds orchestration only. Most functions here are pure
functions of their arguments -- no HTTP, no config store, no I/O. The
exceptions are ``backfill_managed_git`` and ``resolve_effective_version``,
which call the pre-constructed client objects they are passed (no new I/O
wiring of their own); they live here anyway because they are the direct
continuation of ``deploy_data_app``'s git-block/version-resolution logic, not
a separate concern. The service re-exports these names, so existing
``from ...data_app_service import _x`` call sites keep working.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..errors import ErrorCode, KeboolaApiError

logger = logging.getLogger(__name__)

# Encrypted-secret prefixes produced by the Encryption API for PROJECT-scoped
# ciphertext -- one variant per cloud, and exactly these three exist:
# ``KBC::ProjectSecure::`` (AWS KMS), ``KBC::ProjectSecureGKMS::`` (Google KMS)
# and ``KBC::ProjectSecureKV::`` (Azure Key Vault). All three are project-bound
# and decrypt only with the originating project's key. The wider
# ``ComponentSecure*`` / ``ConfigSecure*`` / ``ProjectWideSecure*`` scopes are
# deliberately NOT accepted here -- they are not bound to this project.
#
# Source of truth: the platform's own cipher registry, keboola/keboola-operator
# ``internal/encryptor/wrapper/registry.go`` (mirrored by the wrappers in
# keboola/object-encryptor) and
# https://developers.keboola.com/overview/encryption/. A fourth entry,
# ``KBC::ProjectSecureKMS::``, was carried here from 0.27.0 but appears nowhere
# in the platform -- the AWS wrapper is *named* ``PrefixProjectKMS`` while the
# prefix it emits is plain ``KBC::ProjectSecure::``. Dropped in 0.86.0 (#607).
ENCRYPTED_PASSWORD_PREFIXES: tuple[str, ...] = (
    "KBC::ProjectSecure::",
    "KBC::ProjectSecureGKMS::",
    "KBC::ProjectSecureKV::",
)


# Env vars the data-app runtime auto-injects. Setting a secret whose
# derived env-var name collides with one of these is silently shadowed
# at runtime by the platform value. See storage-access canon at
# https://help.keboola.com/data-apps/storage-access/.
#
# The workspace trio was added in 0.87.0, when `data-app create` started
# writing ``runtime.workspace.enabled: true`` by default: the platform now
# injects these on a new app unless ``--no-workspace`` was passed, so a
# secret whose derived name collides is silently shadowed in the DEFAULT
# case rather than an exotic one. Concretely, someone migrating an app off
# the older `parameters.dataApp.secrets.WORKSPACE_ID` convention would set
# `#WORKSPACE_ID` and get no warning that the platform value wins.
#
# A shadowing WARN is advisory (stderr, never blocking), so a false positive
# on an app that turned the workspace off costs a line of noise, whereas a
# false negative costs a secret that silently does nothing.
#
# TODO: still not verified exhaustively against a running data-app env --
# the runtime may inject more (BRANCH_ID and others). KBC_TOKEN + KBC_URL
# are the canon-documented floor; the workspace trio is documented at
# help.keboola.com/data-apps/storage-access/.
RESERVED_RUNTIME_ENV_VARS: frozenset[str] = frozenset(
    {
        "KBC_TOKEN",
        "KBC_URL",
        # Injected when runtime.workspace.enabled is true (0.87.0+ default).
        "WORKSPACE_ID",
        "QUERY_SERVICE_URL",
        "KBC_WORKSPACE_MANIFEST_PATH",
    }
)


def _derive_runtime_env_var_name(secret_key: str) -> str:
    """Translate a ``#``-prefixed secret key into the runtime env-var name.

    Rule from help.keboola.com/data-apps/python-js/: strip the leading
    ``#``, replace ``-`` with ``_``, uppercase. Examples (verbatim from
    the help canon):

    - ``#KBC_TOKEN`` -> ``KBC_TOKEN``
    - ``#my-custom-var`` -> ``MY_CUSTOM_VAR``
    """
    stripped = secret_key.lstrip("#")
    return stripped.replace("-", "_").upper()


def _secret_fingerprint(ciphertext: str) -> str:
    """First 8 chars of the ciphertext payload after the ``KBC::*::`` prefix.

    The full ciphertext is not a secret in the cryptographic sense (it
    can only be decrypted by the project's KMS), but echoing it in full
    invites copy-paste leakage into tickets and chat. The fingerprint is
    enough to compare two ciphertexts without exposing the payload.
    Returns empty string for non-ciphertext input.
    """
    if not isinstance(ciphertext, str):
        return ""
    for prefix in ENCRYPTED_PASSWORD_PREFIXES:
        if ciphertext.startswith(prefix):
            payload = ciphertext[len(prefix) :]
            return payload[:8]
    return ""


def _build_simple_auth_block() -> dict[str, Any]:
    """Authorization block for password-gated apps (writeup §11.2)."""
    return {
        "app_proxy": {
            "auth_providers": [{"id": "simpleAuth", "type": "password"}],
            "auth_rules": [
                {
                    "type": "pathPrefix",
                    "value": "/",
                    "auth_required": True,
                    "auth": ["simpleAuth"],
                }
            ],
        },
    }


def _build_public_auth_block() -> dict[str, Any]:
    """Authorization block for publicly-accessible apps (no auth gate).

    Mirrors the kbc-ui ``noneProxyAuthorization`` constant exactly.
    Authoritative source — the public backend validator at
    ``keboola/job-queue-job-configuration``
    ``src/JobDefinition/Configuration/Authorization/AppProxyDefinition.php``
    (when ``auth_required=false``, ``auth`` MUST NOT be set; see
    https://github.com/keboola/job-queue-job-configuration). The
    ``keboola/ui`` repo (private; Keboola org members only) corroborates:
    its ``apps/kbc-ui/src/scripts/modules/data-apps/constants.ts``
    exports this exact shape as ``noneProxyAuthorization`` for the
    "None" UI option.

    Without this block, ``--auth public`` shipped in 0.27.0 wrote no
    ``authorization`` key at all -- the Keboola app-proxy refused to
    route traffic and the UI's "Authentication Type" selector showed
    blank. Fixed in 0.28.0.
    """
    return {
        "app_proxy": {
            "auth_providers": [],
            "auth_rules": [
                {
                    "type": "pathPrefix",
                    "value": "/",
                    "auth_required": False,
                }
            ],
        },
    }


def _auth_block_for(auth: str) -> dict[str, Any]:
    """Dispatch on the validated --auth value.

    The validator at :meth:`DataAppService._validate_create_inputs`
    rejects anything other than ``password`` / ``public`` at the service
    boundary, so this code path should only ever see those two values in
    production. We raise loudly on an unexpected value rather than
    silently writing no ``authorization`` block (the v0.27.0 bug this
    helper exists to prevent — see the (since v0.28.0) gotcha entry).
    """
    if auth == "password":
        return _build_simple_auth_block()
    if auth == "public":
        return _build_public_auth_block()
    raise ValueError(
        f"_auth_block_for missing dispatch for {auth!r}; "
        "_validate_create_inputs should have rejected this upstream."
    )


def _build_runtime_block(*, size: str, workspace: bool) -> dict[str, Any]:
    """Build ``configuration.runtime`` for a data-app create.

    ``runtime.workspace.enabled: true`` is what makes the platform provision
    the ephemeral workspace and inject ``WORKSPACE_ID``, ``QUERY_SERVICE_URL``
    and ``KBC_WORKSPACE_MANIFEST_PATH`` into the container -- i.e. it is the
    single switch that decides whether the app can read Storage at all
    (help.keboola.com/data-apps/storage-access/).

    It defaults ON because omitting it fails *silently*: the app
    deploys, reports ``state=running``, passes its health probe, and then
    either serves empty results or crash-loops behind the probe -- which of
    the two depends on whether the app checks its own environment, and the
    platform reports neither. An unused workspace on an app that never reads
    Storage is the far cheaper mistake, so ``--no-workspace`` is the opt-out
    rather than ``--workspace`` the opt-in.

    The block is a SIBLING of ``backend`` -- both live under ``runtime`` (the
    shape the UI and ``modify_python_js_data_app`` both write). When disabled
    we omit the key entirely rather than writing ``enabled: false``, matching
    the pre-0.87.0 body byte-for-byte.
    """
    runtime: dict[str, Any] = {"backend": {"size": size}}
    if workspace:
        runtime["workspace"] = {"enabled": True}
    return runtime


def _build_managed_git_block(repo: dict[str, Any], app_id: str) -> dict[str, Any]:
    """Build ``parameters.dataApp.git`` for a managed-repo app from a
    ``get_git_repo`` response.

    See CLI-15: a managed-repo app's first deploy does not get a workspace
    just from omitting ``configVersion`` -- provisioning is gated on this
    block being present in Storage config, independent of
    ``managedGitRepoId``/``hasManagedGitRepo``. ``deploy_data_app`` calls this
    to backfill it before deploying, same as an external-git app.

    Raises if the lookup returned neither URL -- fail loudly rather than
    deploy with no source pointer.
    """
    git_url = repo.get("httpsUrl") or repo.get("sshUrl")
    if not git_url:
        raise KeboolaApiError(
            error_code=ErrorCode.API_ERROR,
            message=f"App {app_id} is managed but git-repo lookup returned no URL",
            status_code=500,
            retryable=False,
        )
    return {"repository": git_url, "branch": "main", "private": True}


@dataclass(frozen=True)
class ManagedGitBackfillTarget:
    """The identifying context :func:`backfill_managed_git` needs,
    grouped so the call site at ``deploy_data_app`` stays one line."""

    app_id: str
    config_id: str
    branch_id: int | None
    latest_version: str


def backfill_managed_git(
    ds_client: Any,
    storage_client: Any,
    configuration: dict[str, Any],
    ctx: ManagedGitBackfillTarget,
) -> str:
    """Resolve + persist ``parameters.dataApp.git`` for a managed-repo app with
    no git block yet; returns the resulting configVersion to pin.

    See CLI-15: ``deploy_data_app``'s configVersion-omission branch alone does
    not get the app a workspace -- provisioning is gated on this block being
    present. Takes pre-constructed clients (not a service instance) so
    ``deploy_data_app`` can call it inline with no new constructor wiring.
    """
    git_block = _build_managed_git_block(ds_client.get_git_repo(ctx.app_id), ctx.app_id)
    configuration.setdefault("parameters", {}).setdefault("dataApp", {})["git"] = git_block
    updated = storage_client.update_config(
        component_id="keboola.data-apps",  # mirrors data_app_service.DATA_APP_COMPONENT_ID
        config_id=ctx.config_id,
        configuration=configuration,
        change_description="Auto-backfill managed-repo git block (workspace provisioning fix)",
        branch_id=ctx.branch_id,
    )
    new_version = str(updated.get("version", "") or ctx.latest_version)
    logger.info(
        "Backfilled parameters.dataApp.git for managed-repo app %s (config %s); "
        "Storage version %s -> %s",
        ctx.app_id,
        ctx.config_id,
        ctx.latest_version,
        new_version,
    )
    return new_version


def resolve_effective_version(
    ds_client: Any,
    storage_client: Any,
    app: dict[str, Any],
    app_id: str,
    config_id: str,
    branch_id: int | None,
) -> str:
    """Resolve the Storage ``configVersion`` for ``deploy_data_app`` to pin.

    configVersion resolution depends on where the app's *source* lives:

    * Streamlit / external-git (``parameters.dataApp.git`` present) -- pin the
      latest Storage version so the operator reads the current git block.
    * A pure managed repo (``hasManagedGitRepo``, no git block yet) -- backfill
      the git block first (CLI-15: omitting configVersion alone does not get
      the app a workspace; see :func:`backfill_managed_git`), then pin the
      version that backfill wrote.

    Raises if neither a git block can be backfilled nor a Storage version is
    resolvable.
    """
    storage_config = storage_client.get_config_detail(
        "keboola.data-apps",  # mirrors data_app_service.DATA_APP_COMPONENT_ID
        config_id,
        branch_id=branch_id,
    )
    latest_version = str(storage_config.get("version", "") or "")
    configuration = _coerce_config_dict(storage_config.get("configuration"))
    data_app_cfg = (configuration.get("parameters") or {}).get("dataApp") or {}
    is_managed = bool(app.get("hasManagedGitRepo"))
    has_git_block = bool(data_app_cfg.get("git"))
    if is_managed and not has_git_block:
        ctx = ManagedGitBackfillTarget(app_id, config_id, branch_id, latest_version)
        return backfill_managed_git(ds_client, storage_client, configuration, ctx)
    if not latest_version:
        raise KeboolaApiError(
            message=(
                f"Cannot resolve a Storage configVersion for app {app_id}; "
                "Storage config returned no version."
            ),
            status_code=500,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )
    return latest_version


def _redact_secret(value: Any) -> Any:
    """Replace encrypted ``#`` values with a placeholder for human output."""
    if isinstance(value, str) and value.startswith("KBC::"):
        return "<encrypted>"
    return value


def _redact_git_block(git: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the git block with the encrypted password redacted."""
    redacted = dict(git)
    if "#password" in redacted:
        redacted["#password"] = _redact_secret(redacted["#password"])
    return redacted


def _coerce_config_dict(configuration: Any) -> dict[str, Any]:
    """Return a Storage config's ``configuration`` as a dict.

    ``get_config_detail`` parses the whole response via ``response.json()`` so
    ``configuration`` is normally already a dict, but some Storage payloads echo
    it as a JSON string. Mirror the defensive handling in ``get_data_app`` so a
    string never crashes the chained ``.get()`` lookups downstream.
    """
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except (ValueError, TypeError):
            return {}
    return configuration if isinstance(configuration, dict) else {}


def _redact_secrets_block(secrets: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``parameters.dataApp.secrets`` with each ciphertext redacted.

    Used by ``get_data_app`` so the ``raw.storage_config`` echo cannot
    leak any secret's encrypted value into ``--json`` output. Same
    defence-in-depth rationale as :func:`_redact_git_block`.
    """
    if not isinstance(secrets, dict):
        return secrets
    return {key: _redact_secret(value) for key, value in secrets.items()}


def _redact_storage_config(storage_config: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy the Storage config dict and redact any nested encrypted PAT.

    Used by ``get_data_app`` so the ``raw.storage_config`` echo cannot leak
    the encrypted git PAT verbatim into ``--json`` output. The redaction is
    cosmetic (the ciphertext is not a secret in the cryptographic sense --
    it can only be decrypted by Keboola's KMS), but defense-in-depth:
    keeping ciphertext out of consumed JSON limits its blast radius if a
    downstream consumer logs it.
    """
    if not isinstance(storage_config, dict):
        return storage_config
    redacted = dict(storage_config)
    configuration = redacted.get("configuration")
    if isinstance(configuration, dict):
        configuration = dict(configuration)
        parameters = configuration.get("parameters")
        if isinstance(parameters, dict):
            parameters = dict(parameters)
            data_app = parameters.get("dataApp")
            if isinstance(data_app, dict):
                data_app = dict(data_app)
                git = data_app.get("git")
                if isinstance(git, dict):
                    data_app["git"] = _redact_git_block(git)
                secrets = data_app.get("secrets")
                if isinstance(secrets, dict):
                    data_app["secrets"] = _redact_secrets_block(secrets)
                parameters["dataApp"] = data_app
            configuration["parameters"] = parameters
        redacted["configuration"] = configuration
    return redacted
