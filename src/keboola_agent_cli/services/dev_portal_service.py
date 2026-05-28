"""Developer Portal business logic.

Identity CRUD + prepare/apply discipline for portal writes. Commands stay
thin; this module owns diff computation, publish pre-flight validation,
and the verify-on-add login probe.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config_store import ConfigStore
from ..dev_portal_client import DeveloperPortalClient
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import DeveloperPortalIdentity

ClientFactory = Callable[[DeveloperPortalIdentity], DeveloperPortalClient]

_log = logging.getLogger(__name__)

_BANNED_NAME_WORDS = ("extractor", "writer")
_REQUIRED_PUBLISH_FIELDS = (
    "icon",
    "name",
    "type",
    "repository",
    "shortDescription",
    "longDescription",
    "licenseUrl",
    "documentationUrl",
)


@dataclass(frozen=True)
class FieldDiff:
    key: str
    current: Any
    new: Any


@dataclass(frozen=True)
class PendingWrite:
    """Base for any prepared portal write. apply() in the service dispatches on the subclass."""

    alias: str
    vendor: str


@dataclass(frozen=True)
class PendingCreate(PendingWrite):
    payload: dict[str, Any]


@dataclass(frozen=True)
class PendingPatch(PendingWrite):
    app_id: str
    payload: dict[str, Any]
    current: dict[str, Any]
    diff: list[FieldDiff] = field(default_factory=list)


@dataclass(frozen=True)
class PendingIconUpload(PendingWrite):
    app_id: str
    png_path: Path
    png_bytes: bytes


@dataclass(frozen=True)
class PendingPublish(PendingWrite):
    app_id: str
    current: dict[str, Any]


@dataclass(frozen=True)
class PendingDeprecate(PendingWrite):
    app_id: str


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

    # ----- Reads -----

    def list_apps(self, alias: str, vendor: str) -> list[dict[str, Any]]:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            return client.list_apps(vendor)

    def get_app(self, alias: str, vendor: str, app_id: str) -> dict[str, Any]:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            return client.get_app(vendor, app_id)

    # ----- Prepare (no portal write yet) -----

    def prepare_create(self, alias: str, vendor: str, payload: dict[str, Any]) -> PendingCreate:
        for required in ("id", "name", "type"):
            if required not in payload:
                raise KeboolaApiError(
                    message=f"create payload must include '{required}'",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
        name_lower = str(payload["name"]).lower()
        for banned in _BANNED_NAME_WORDS:
            if banned in name_lower:
                raise KeboolaApiError(
                    message=(
                        f"App name must not contain {_BANNED_NAME_WORDS!r}; got {payload['name']!r}"
                    ),
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
        # Confirm identity exists; defer login until apply().
        self._resolve_identity(alias)
        return PendingCreate(alias=alias, vendor=vendor, payload=payload)

    def prepare_patch(
        self,
        alias: str,
        vendor: str,
        app_id: str,
        payload: dict[str, Any],
    ) -> PendingPatch:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            current = client.get_app(vendor, app_id)
        diff = [
            FieldDiff(key=k, current=current.get(k), new=v)
            for k, v in payload.items()
            if current.get(k) != v
        ]
        return PendingPatch(
            alias=alias,
            vendor=vendor,
            app_id=app_id,
            payload=payload,
            current=current,
            diff=diff,
        )

    def prepare_upload_icon(
        self, alias: str, vendor: str, app_id: str, path: str | Path
    ) -> PendingIconUpload:
        import struct

        p = Path(path)
        if not p.is_file():
            raise KeboolaApiError(
                message=f"Icon file not found: {p}",
                error_code=ErrorCode.FILE_NOT_FOUND,
            )
        data = p.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise KeboolaApiError(
                message=f"Icon file is not a PNG: {p}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        if len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            if (width, height) != (128, 128):
                _log.warning(
                    "Icon is %dx%d, not 128x128 — portal may reject it.",
                    width,
                    height,
                )
        self._resolve_identity(alias)
        return PendingIconUpload(
            alias=alias,
            vendor=vendor,
            app_id=app_id,
            png_path=p,
            png_bytes=data,
        )

    def prepare_publish(self, alias: str, vendor: str, app_id: str) -> PendingPublish:
        ident = self._resolve_identity(alias)
        with self._client_factory(ident) as client:
            current = client.get_app(vendor, app_id)
        missing = [f for f in _REQUIRED_PUBLISH_FIELDS if not current.get(f)]
        if missing:
            raise KeboolaApiError(
                message=(
                    f"Cannot publish {app_id}: missing required fields "
                    f"{missing}. Fix them via `kbagent dev-portal patch` first."
                ),
                error_code=ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING,
            )
        return PendingPublish(alias=alias, vendor=vendor, app_id=app_id, current=current)

    def prepare_deprecate(self, alias: str, vendor: str, app_id: str) -> PendingDeprecate:
        self._resolve_identity(alias)
        return PendingDeprecate(alias=alias, vendor=vendor, app_id=app_id)

    # ----- Apply (calls the portal write) -----

    def apply(self, pending: PendingWrite) -> dict[str, Any]:
        ident = self._resolve_identity(pending.alias)
        with self._client_factory(ident) as client:
            if isinstance(pending, PendingCreate):
                return client.create_app(pending.vendor, pending.payload)
            if isinstance(pending, PendingPatch):
                return client.patch_app(pending.vendor, pending.app_id, pending.payload)
            if isinstance(pending, PendingIconUpload):
                client.upload_icon(pending.vendor, pending.app_id, pending.png_bytes)
                return {"status": "uploaded", "app": pending.app_id}
            if isinstance(pending, PendingPublish):
                return client.publish_app(pending.vendor, pending.app_id)
            if isinstance(pending, PendingDeprecate):
                return client.deprecate_app(pending.vendor, pending.app_id)
        raise KeboolaApiError(
            message=f"Unknown pending write type: {type(pending).__name__}",
            error_code=ErrorCode.INTERNAL_ERROR,
        )
