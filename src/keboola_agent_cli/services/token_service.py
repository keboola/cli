"""Storage API token service -- scoped-token minting, revocation, and rotation.

Business logic for the ``kbagent token`` command group. Wraps the Storage API
token endpoints (``POST /v2/storage/tokens``, ``DELETE .../{id}``,
``POST .../{id}/refresh``) behind kbagent-alias resolution and an injectable
:class:`KeboolaClient` factory (testability).

These are **Storage API** operations authenticated with the per-project Storage
token (``X-StorageApi-Token``) -- no manage token is involved. The acting token
must itself carry ``canManageTokens``; the API rejects the mint/rotate otherwise
(surfaced as an ``ACCESS_DENIED`` :class:`KeboolaApiError` with the token masked).

The scoped-token use case is the Keboola "single-bucket write token" pattern
(and device enrollment, ADR 0005 in keboola/jasnost): mint a narrow, expiring
token that can upload Files + write one sink bucket and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..client import KeboolaClient
from ..config_store import ConfigStore
from .base import (
    ResolvedProjectCredentials,
    default_client_factory,
    make_client_factory,
    resolve_project_credentials,
)

logger = logging.getLogger(__name__)

KeboolaClientFactory = Callable[[str, str], KeboolaClient]


def default_token_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Construct a :class:`KeboolaClient` bound to ``stack_url`` + ``token``.

    Delegates to :func:`services.base.default_client_factory` so this stays
    a static-token-only builder (fails fast on a session sentinel) while
    keeping its own name for callers/tests that inject it explicitly. Real
    usage goes through :class:`TokenService`'s bearer-aware default
    (``make_client_factory``) instead.
    """
    return default_client_factory(stack_url, token)


class TokenService:
    """Business logic for scoped Storage token create / delete / refresh."""

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: KeboolaClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory or make_client_factory(config_store)

    def create_scoped_token(
        self,
        *,
        alias: str,
        description: str,
        bucket_write: list[str] | None = None,
        bucket_read: list[str] | None = None,
        component_access: list[str] | None = None,
        can_read_all_file_uploads: bool = False,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        """Mint a scoped token in ``alias``'s project.

        ``bucket_write`` / ``bucket_read`` become the ``bucketPermissions`` map
        (write wins if a bucket appears in both). The returned dict is the raw
        API response (its ``token`` field is a one-time secret reveal) plus the
        resolving ``alias``.
        """
        creds = self._resolve_project(alias)
        bucket_permissions: dict[str, str] = {}
        for bucket_id in bucket_read or []:
            bucket_permissions[bucket_id] = "read"
        for bucket_id in bucket_write or []:
            # write is the stronger grant -- it wins over a read on the same bucket.
            bucket_permissions[bucket_id] = "write"
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            result = client.create_scoped_token(
                description=description,
                bucket_permissions=bucket_permissions or None,
                component_access=component_access or None,
                can_read_all_file_uploads=can_read_all_file_uploads,
                expires_in=expires_in,
            )
            return {"alias": alias, **result}
        finally:
            client.close()

    def list_tokens(self, *, alias: str) -> dict[str, Any]:
        """List every Storage token in ``alias``'s project.

        Returns ``{"alias", "count", "tokens"}``. Each entry is the raw API
        token dict **minus its ``token`` field**: a project with the
        ``force-decrypted-token`` feature has the Storage API embed live secret
        values in the listing, and echoing those would break the group's
        "the secret is revealed once, at mint" contract for every token at
        once. Everything else is passed through untouched.

        The acting token needs ``canManageTokens``, same as create/refresh.
        """
        creds = self._resolve_project(alias)
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            tokens = [
                {key: value for key, value in token.items() if key != "token"}
                for token in client.list_tokens()
            ]
            return {"alias": alias, "count": len(tokens), "tokens": tokens}
        finally:
            client.close()

    def delete_token(self, *, alias: str, token_id: str) -> dict[str, Any]:
        """Revoke a token immediately in ``alias``'s project."""
        creds = self._resolve_project(alias)
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            client.delete_token(token_id)
            return {"status": "deleted", "alias": alias, "token_id": token_id}
        finally:
            client.close()

    def refresh_token(self, *, alias: str, token_id: str) -> dict[str, Any]:
        """Rotate a token (old value invalidated) in ``alias``'s project."""
        creds = self._resolve_project(alias)
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            result = client.refresh_token(token_id)
            return {"alias": alias, **result}
        finally:
            client.close()

    def _resolve_project(self, alias: str) -> ResolvedProjectCredentials:
        """Resolve ``alias`` to its stack URL + token (or raise ConfigError)."""
        return resolve_project_credentials(self._config_store, alias)
