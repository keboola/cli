"""Storage API token service -- scoped-token minting, revocation, and rotation.

Business logic for the ``kbagent token`` command group. Wraps the Storage API
token endpoints (``POST /v2/storage/tokens``, ``DELETE .../{id}``,
``POST .../{id}/refresh``) behind kbagent-alias resolution and an injectable
:class:`KeboolaClient` factory (testability).

These are **Storage API** operations authenticated with the per-project Storage
token (``X-StorageApi-Token``) -- no manage token is involved. Minting and
rotating require a **master (admin) Storage token** -- ``canManageTokens`` alone
is not sufficient: the Storage API's ``CreateTokenVoter`` treats a non-admin
token carrying that flag as an impossible state and throws a ``LogicException``
(surfaced as a generic 500 "Application error."), which is exactly the shape of
token ``org setup`` / ``project refresh`` mint (issue #599). A pre-flight guard
turns that into a clean ``MISSING_MASTER_TOKEN`` error before any write.
Listing and deleting only need ``canManageTokens`` (verified live in #599) and
are deliberately not guarded.

The scoped-token use case is the Keboola "single-bucket write token" pattern
(and device enrollment, ADR 0005 in keboola/jasnost): mint a narrow, expiring
token that can upload Files + write one sink bucket and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..client import KeboolaClient
from ..errors import ErrorCode, KeboolaApiError
from ._token_last_used import dormancy_rank, enrich_tokens
from .base import (
    BaseService,
    ResolvedProjectCredentials,
    default_client_factory,
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


class TokenService(BaseService):
    """Business logic for scoped Storage token create / delete / refresh.

    Extends :class:`BaseService` for its worker-pool sizing
    (``_resolve_max_workers``: env var > config.json > 10), which the
    ``--with-last-used`` fan-out shares with every other parallel operation in
    the CLI rather than inventing its own limit.
    """

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
            self._require_master_token(client, alias=alias, command="token create")
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

    def list_tokens(self, *, alias: str, with_last_used: bool = False) -> dict[str, Any]:
        """List every Storage token in ``alias``'s project.

        Returns ``{"alias", "count", "tokens"}``. Each entry is the raw API
        token dict **minus its ``token`` field**: a project with the
        ``force-decrypted-token`` feature has the Storage API embed live secret
        values in the listing, and echoing those would break the group's
        "the secret is revealed once, at mint" contract for every token at
        once. Everything else is passed through untouched.

        With ``with_last_used`` every entry additionally carries ``lastUsed``,
        ``lastUsedEvent`` and ``lastUsedStatus``, and the result grows an
        ``errors`` list; the rows are re-ordered dormant-first so reading order
        is cleanup order. See :meth:`_enrich_with_last_used`. The flag is
        opt-in because it costs one extra request per token -- a plain listing
        that only wants an id to hand to ``token delete`` must not pay for it,
        and machine consumers keep the exact response shape they parse today.

        The acting token needs ``canManageTokens`` -- unlike create/refresh it
        does **not** need to be a master token, so this listing is deliberately
        not behind :meth:`_require_master_token`.
        """
        creds = self._resolve_project(alias)
        client = self._client_factory(creds.stack_url, creds.token)
        try:
            tokens = [
                {key: value for key, value in token.items() if key != "token"}
                for token in client.list_tokens()
            ]
            result: dict[str, Any] = {"alias": alias, "count": len(tokens), "tokens": tokens}
            if with_last_used:
                errors = self._enrich_with_last_used(client, tokens)
                tokens.sort(key=dormancy_rank)
                result["errors"] = errors
            return result
        finally:
            client.close()

    def _enrich_with_last_used(
        self, client: KeboolaClient, tokens: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Fan out the last-used lookup over the shared worker pool.

        Sizing comes from :meth:`BaseService._resolve_max_workers` (env var >
        config.json > 10) so this obeys the same concurrency ceiling as every
        other parallel operation in the CLI instead of inventing its own.
        """
        return enrich_tokens(client, tokens, max_workers=self._resolve_max_workers())

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
            self._require_master_token(client, alias=alias, command="token refresh")
            result = client.refresh_token(token_id)
            return {"alias": alias, **result}
        finally:
            client.close()

    def _require_master_token(self, client: KeboolaClient, *, alias: str, command: str) -> None:
        """Fail fast unless the acting token is a master (admin) token.

        Pre-flight for the token-minting/rotating writes, mirroring the
        ``config oauth-url`` guard: ``POST /v2/storage/tokens`` authorizes via
        ``CreateTokenVoter``, which throws a ``LogicException`` ("Normal token
        cannot have manage tokens") for a non-admin token carrying
        ``canManageTokens`` -- surfaced to the caller as a vague 500
        "Application error." instead of a 403. Every token minted by
        ``org setup`` / ``project refresh`` is exactly that shape (issue #599),
        so without this check the operator gets a misleading server-side error
        for what is really a local credential problem.

        Raises:
            KeboolaApiError: ``MISSING_MASTER_TOKEN`` (403) when the acting
                token is not a master token.
        """
        info = client.get_project_info()
        if not info.get("isMasterToken", False):
            raise KeboolaApiError(
                status_code=403,
                error_code=ErrorCode.MISSING_MASTER_TOKEN,
                message=(
                    f"`{command}` requires a master Storage API token on "
                    f"project '{alias}'. The current token "
                    f"(id={info.get('id', '?')}, "
                    f"description='{info.get('description', '?')}') is not a "
                    f"master token -- `canManageTokens` alone is not enough: "
                    f"the Storage API rejects the request with a generic 500 "
                    f"'Application error.' (CreateTokenVoter LogicException, "
                    f"issue #599). Point the alias at a master token "
                    f"(`kbagent project edit --project {alias} --token <MASTER>`) "
                    f"-- master = the token from your own user account in the "
                    f"Keboola UI, `isMasterToken: true` in `kbagent token list`."
                ),
            )

    def _resolve_project(self, alias: str) -> ResolvedProjectCredentials:
        """Resolve ``alias`` to its stack URL + token (or raise ConfigError)."""
        return resolve_project_credentials(self._config_store, alias)
