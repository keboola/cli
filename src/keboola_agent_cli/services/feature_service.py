"""Feature-flag management service (super-admin Manage API).

Wraps the stack feature catalogue (``GET /manage/features``) and the
project/user feature assignment endpoints behind a layer that:

- resolves a kbagent project alias to its ``(stack_url, project_id)`` via
  :class:`ConfigStore` (the alias is the only handle a caller needs -- the
  numeric project ID and stack URL are looked up, never typed);
- normalises the ``features`` array on a project/user object, which the
  Manage API may return either as a list of objects or a list of bare
  strings, into a uniform list of :class:`Feature` dicts;
- supports ``dry_run`` previews for the write paths so an agent can show the
  user exactly what would change before a super-admin token touches the stack.

The Manage API token is never persisted -- it is passed in per call from the
interactive prompt resolved by the command layer (see ``resolve_manage_token``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..config_store import ConfigStore
from ..errors import ConfigError
from ..manage_client import ManageClient
from ..models import Feature

logger = logging.getLogger(__name__)

ManageClientFactory = Callable[[str, str], ManageClient]


def default_manage_client_factory(stack_url: str, manage_token: str) -> ManageClient:
    """Construct a :class:`ManageClient` bound to ``stack_url``."""
    return ManageClient(stack_url=stack_url, manage_token=manage_token)


def _normalise_features(raw: Any) -> list[dict[str, Any]]:
    """Normalise a ``features`` payload into a list of Feature dicts.

    The Manage API returns features as either a list of objects or a list of
    bare strings depending on the endpoint/stack version. Bare strings are
    wrapped as ``{"name": <string>}`` so downstream rendering is uniform.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            out.append(Feature(name=item).model_dump(by_alias=False))
        elif isinstance(item, dict):
            out.append(Feature.model_validate(item).model_dump(by_alias=False))
    return out


class FeatureService:
    """Business logic for stack, project, and user feature flags."""

    def __init__(
        self,
        config_store: ConfigStore,
        manage_client_factory: ManageClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._manage_client_factory = manage_client_factory or default_manage_client_factory

    # ------------------------------------------------------------------
    # Stack catalogue
    # ------------------------------------------------------------------

    def list_stack_features(self, *, manage_token: str, alias: str) -> dict[str, Any]:
        """List every feature defined on the stack the alias points at.

        The alias is used only to resolve the stack URL -- the catalogue is
        stack-wide, not project-scoped.
        """
        stack_url = self._resolve_stack_url(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            raw = manage_client.list_features()
            return {
                "alias": alias,
                "stack_url": stack_url,
                "features": _normalise_features(raw),
            }
        finally:
            manage_client.close()

    # ------------------------------------------------------------------
    # Project features
    # ------------------------------------------------------------------

    def list_project_features(self, *, manage_token: str, alias: str) -> dict[str, Any]:
        """List features assigned to the project registered under ``alias``."""
        stack_url, project_id = self._resolve_alias(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            project = manage_client.get_project(project_id)
            return {
                "alias": alias,
                "project_id": project_id,
                "project_name": project.get("name", ""),
                "features": _normalise_features(project.get("features")),
            }
        finally:
            manage_client.close()

    def add_project_feature(
        self, *, manage_token: str, alias: str, feature: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Enable ``feature`` on the project registered under ``alias``."""
        stack_url, project_id = self._resolve_alias(alias)
        if dry_run:
            return {
                "status": "dry_run",
                "action": "add",
                "alias": alias,
                "project_id": project_id,
                "feature": feature,
            }
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            manage_client.add_project_feature(project_id, feature)
            return {
                "status": "added",
                "alias": alias,
                "project_id": project_id,
                "feature": feature,
            }
        finally:
            manage_client.close()

    def remove_project_feature(
        self, *, manage_token: str, alias: str, feature: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Disable ``feature`` on the project registered under ``alias``."""
        stack_url, project_id = self._resolve_alias(alias)
        if dry_run:
            return {
                "status": "dry_run",
                "action": "remove",
                "alias": alias,
                "project_id": project_id,
                "feature": feature,
            }
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            manage_client.remove_project_feature(project_id, feature)
            return {
                "status": "removed",
                "alias": alias,
                "project_id": project_id,
                "feature": feature,
            }
        finally:
            manage_client.close()

    # ------------------------------------------------------------------
    # User features
    # ------------------------------------------------------------------

    def list_user_features(self, *, manage_token: str, alias: str, email: str) -> dict[str, Any]:
        """List features assigned to ``email`` on the alias's stack."""
        stack_url = self._resolve_stack_url(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            user = manage_client.get_user(email)
            return {
                "alias": alias,
                "stack_url": stack_url,
                "email": email,
                "features": _normalise_features(user.get("features")),
            }
        finally:
            manage_client.close()

    def add_user_feature(
        self, *, manage_token: str, alias: str, email: str, feature: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Enable ``feature`` on the user ``email``."""
        stack_url = self._resolve_stack_url(alias)
        if dry_run:
            return {
                "status": "dry_run",
                "action": "add",
                "alias": alias,
                "email": email,
                "feature": feature,
            }
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            manage_client.add_user_feature(email, feature)
            return {
                "status": "added",
                "alias": alias,
                "email": email,
                "feature": feature,
            }
        finally:
            manage_client.close()

    def remove_user_feature(
        self, *, manage_token: str, alias: str, email: str, feature: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Disable ``feature`` on the user ``email``."""
        stack_url = self._resolve_stack_url(alias)
        if dry_run:
            return {
                "status": "dry_run",
                "action": "remove",
                "alias": alias,
                "email": email,
                "feature": feature,
            }
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            manage_client.remove_user_feature(email, feature)
            return {
                "status": "removed",
                "alias": alias,
                "email": email,
                "feature": feature,
            }
        finally:
            manage_client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_alias(self, alias: str) -> tuple[str, int]:
        """Resolve ``alias`` to ``(stack_url, project_id)`` for project ops."""
        project = self._config_store.get_project(alias)
        if project is None:
            raise ConfigError(
                f"Project alias '{alias}' is not registered. Run `kbagent project list`."
            )
        if project.project_id is None:
            raise ConfigError(
                f"Project alias '{alias}' has no numeric project_id; "
                "re-add it via `kbagent project add` to populate it."
            )
        return project.stack_url, project.project_id

    def _resolve_stack_url(self, alias: str) -> str:
        """Resolve ``alias`` to its stack URL for stack/user ops.

        Unlike :meth:`_resolve_alias`, this does not require a numeric
        project_id -- the stack catalogue and user features are not
        project-scoped, the alias is only a handle to the stack URL.
        """
        project = self._config_store.get_project(alias)
        if project is None:
            raise ConfigError(
                f"Project alias '{alias}' is not registered. Run `kbagent project list`."
            )
        return project.stack_url
