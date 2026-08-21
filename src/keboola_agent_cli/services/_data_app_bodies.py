"""Pure body-builders and redaction helpers for :mod:`data_app_service`.

Extracted from ``data_app_service.py`` (which is over its file-size budget)
so the service module holds orchestration only. Everything here is a pure
function of its arguments: no HTTP, no config store, no I/O. The service
re-exports these names, so existing ``from ...data_app_service import _x``
call sites keep working.
"""

from __future__ import annotations

import json
from typing import Any

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
    either serves empty results or crash-loops behind the probe, with the only
    diagnostic buried in the container log as
    ``Missing env vars: WORKSPACE_ID``. An unused workspace on an app that
    never reads Storage is the far cheaper mistake, so ``--no-workspace`` is
    the opt-out rather than ``--workspace`` the opt-in.

    The block is a SIBLING of ``backend`` -- both live under ``runtime`` (the
    shape the UI and ``modify_python_js_data_app`` both write). When disabled
    we omit the key entirely rather than writing ``enabled: false``, matching
    the pre-0.87.0 body byte-for-byte.
    """
    runtime: dict[str, Any] = {"backend": {"size": size}}
    if workspace:
        runtime["workspace"] = {"enabled": True}
    return runtime


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
