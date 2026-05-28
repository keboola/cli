"""Developer Portal business logic.

Identity CRUD + prepare/apply discipline for portal writes. Commands stay
thin; this module owns diff computation, publish pre-flight validation,
and the verify-on-add login probe.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config_store import ConfigStore
from ..dev_portal_client import DeveloperPortalClient
from ..errors import ConfigError
from ..models import DeveloperPortalIdentity

ClientFactory = Callable[[DeveloperPortalIdentity], DeveloperPortalClient]


class DeveloperPortalService:
    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory,
    ) -> None:
        self._store = config_store
        self._client_factory = client_factory

    # ----- Identity management -----

    def add_identity(self, alias: str, identity: DeveloperPortalIdentity) -> None:
        """Verify creds (login probe) BEFORE persisting.

        Same UX as `kbagent project add` (which calls verify_token first):
        bad creds fail fast and never land in config.json.
        """
        with self._client_factory(identity) as client:
            client._ensure_authenticated()  # raises on bad creds / MFA failure
        self._store.add_dev_portal_identity(alias, identity)

    def list_identities(self) -> dict[str, DeveloperPortalIdentity]:
        return dict(self._store.load().dev_portal_identities)

    def remove_identity(self, alias: str) -> None:
        self._store.remove_dev_portal_identity(alias)

    def edit_identity(self, alias: str, **fields: Any) -> None:
        self._store.edit_dev_portal_identity(alias, **fields)

    def rename_identity(self, old_alias: str, new_alias: str) -> None:
        self._store.rename_dev_portal_identity(old_alias, new_alias)

    def use_identity(self, alias: str) -> None:
        self._store.set_default_dev_portal_identity(alias)

    def current_identity(self) -> str:
        return self._store.load().default_dev_portal_identity

    def verify_identity(self, alias: str) -> dict[str, str]:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            client._ensure_authenticated()
        return {"alias": alias, "username": ident.username}

    # ----- Internal -----

    def _resolve_identity(self, alias: str) -> DeveloperPortalIdentity:
        ident = self._store.get_dev_portal_identity(alias)
        if ident is None:
            raise ConfigError(
                f"Developer Portal identity '{alias}' not found. "
                "Run `kbagent dev-portal identity list` to see configured identities."
            )
        return ident
