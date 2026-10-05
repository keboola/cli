"""Row-level security service -- business logic for ``kbagent rls``.

Composes :class:`MetastoreClient` primitives into the ``rls-policy`` CRUD
operations exposed by the CLI, mirroring :class:`SemanticLayerService`'s
shape for the same kind of metastore-backed object type -- but scaled down
to one object type instead of seven.

The one structural rule every write path in this module enforces: an
``rls-policy`` object is **never** created or updated at plain ``project``
scope. Authorship is centralized at ``organization`` (default) or
``targeted`` scope only -- this command surface does not even have a
``--scope`` flag that could select ``project``, matching the backend's own
restriction (RFC "Scope is organization or targeted -- never plain
project, by design, not merely by convention").

On a stack whose metastore predates the ``rls-policy`` schema, a real project
answers with a schema-fetch/list/get/post failure (see ``fetch_schema`` and the
``gotchas.md`` entry). Every method here is unit-tested against a mocked
:class:`MetastoreClient`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config_store import ConfigStore
from ..errors import ErrorCode, KeboolaApiError
from ..metastore_client import (
    MetastoreClient,
    MetastoreScope,
    SemanticType,
    fetch_resolved_schema,
)
from ..models import ProjectConfig
from . import _rls_condition
from ._rls_condition import RLS_DIALECTS
from .base import BaseService, ClientFactory, make_session_aware_client_factory

logger = logging.getLogger(__name__)

RLS_ITEM_TYPE: SemanticType = "rls-policy"

__all__ = ["RLS_DIALECTS", "RLS_ITEM_TYPE", "RlsSchemaFetch", "RlsService"]

MetastoreClientFactory = Callable[[str, str], MetastoreClient]

# Failures of the caller's own credentials/permissions: never to be mistaken for "schema not available".
_AUTH_ERROR_CODES = frozenset(
    {
        ErrorCode.INVALID_TOKEN,
        ErrorCode.MISSING_MASTER_TOKEN,
        ErrorCode.ACCESS_DENIED,
        ErrorCode.PERMISSION_DENIED,
    }
)


@dataclass(frozen=True)
class RlsSchemaFetch:
    """Result of fetching the live ``rls-policy`` JSON Schema.

    ``schema`` is ``None`` on any failure (network error, ``KeboolaApiError``,
    malformed/empty schema -- most likely: the object type isn't registered on
    that stack's metastore). A ``None`` schema must NOT block a write;
    callers degrade to the schema-independent checks in ``_rls_condition``
    and surface ``reason`` as a warning -- mirrors
    ``FlowService._fetch_flow_schema`` / ``FlowSchemaFetch`` exactly.
    """

    schema: dict[str, Any] | None
    reason: str | None


class RlsService(BaseService):
    """Business logic for the ``rls`` command group.

    Inherits multi-project resolution (``resolve_projects``) from
    :class:`BaseService`. Adds a dedicated :class:`MetastoreClient` factory,
    same pattern as :class:`SemanticLayerService`, so command-layer
    operations can target the metastore without polluting the Storage API
    client.

    ``item_type`` / ``label`` / ``invalid_error_code`` and the two rule hooks
    (:meth:`_local_rule_errors`, :meth:`_preview_rules`) are all that differ
    for the column-level sibling, :class:`ClsService`, which subclasses this:
    the scope rule, fetch-then-merge update and schema degradation are the
    same for both policy types.
    """

    item_type: SemanticType = RLS_ITEM_TYPE
    label = "RLS"
    invalid_error_code = ErrorCode.INVALID_RLS_POLICY

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        metastore_client_factory: MetastoreClientFactory | None = None,
    ) -> None:
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._metastore_factory: MetastoreClientFactory = (
            metastore_client_factory
            or make_session_aware_client_factory(config_store, MetastoreClient)
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_one_project(self, alias: str) -> ProjectConfig:
        return self.resolve_projects([alias])[alias]

    def _new_metastore_client(self, project: ProjectConfig) -> MetastoreClient:
        """Build a fresh metastore client. Caller is responsible for ``close()``."""
        return self._metastore_factory(project.stack_url, project.token)

    def fetch_schema(self, alias: str) -> RlsSchemaFetch:
        """Public schema fetch (used by ``rls schema`` and every write path)."""
        project = self._resolve_one_project(alias)
        return self._fetch_schema_for_project(project)

    def _fetch_schema_for_project(self, project: ProjectConfig) -> RlsSchemaFetch:
        """Fetch the live policy schema, degrading when it is merely *unavailable*.

        A network error, an empty/malformed schema or a non-auth ``KeboolaApiError`` (most likely: the
        object type isn't registered on the stack's metastore) degrades to ``schema=None`` + ``reason``
        and never blocks a caller. An authentication/authorization failure (``INVALID_TOKEN``,
        ``MISSING_MASTER_TOKEN``, access denied) is a real problem the user must see, so it is
        re-raised instead of being reported as a missing schema.
        """
        with self._new_metastore_client(project) as client:
            try:
                schema, _version = fetch_resolved_schema(client, self.item_type)
            except KeboolaApiError as exc:
                if exc.error_code in _AUTH_ERROR_CODES or exc.status_code in (401, 403):
                    raise
                return RlsSchemaFetch(schema=None, reason=exc.message)
            except Exception as exc:  # any fetch failure must degrade, never block a write
                return RlsSchemaFetch(schema=None, reason=str(exc))
        if not schema:
            return RlsSchemaFetch(
                schema=None, reason=f"metastore returned no schema for {self.item_type}"
            )
        return RlsSchemaFetch(schema=schema, reason=None)

    @staticmethod
    def _row_from_item(item: dict[str, Any]) -> dict[str, Any]:
        attrs = item.get("attributes") or {}
        meta = item.get("meta") or {}
        return {
            "id": item.get("id", ""),
            "table": attrs.get("table", ""),
            "dialect": attrs.get("dialect", ""),
            "rule_count": len(attrs.get("rules") or []),
            "scope": meta.get("scope", ""),
            "source_project_id": meta.get("sourceProjectId"),
            "target_project_ids": meta.get("targetProjectIds") or [],
        }

    def _resolve_scope(self, target_project_ids: list[str] | None) -> MetastoreScope:
        """Never ``"project"`` -- ``targeted`` iff target projects were given."""
        return "targeted" if target_project_ids else "organization"

    def _validate_policy(
        self, *, table: str, dialect: str, rules: list[dict[str, Any]], project: ProjectConfig
    ) -> tuple[list[str], list[str]]:
        """Run every validation this module can, local checks first; returns ``(errors, warnings)``.

        Local (schema-independent) checks always run. Structural (Draft7,
        against the live-fetched schema) checks run too when the fetch
        succeeds -- when it doesn't, the caller gets a warning (the reduced
        validation is reported, not hidden), not a block (see :class:`RlsSchemaFetch`).
        """
        errors: list[str] = []
        if dialect not in _rls_condition.RLS_DIALECTS:
            errors.append(f"dialect must be one of {_rls_condition.RLS_DIALECTS}, got {dialect!r}")
        errors.extend(self._local_rule_errors(rules))
        if errors:
            return errors, []

        warnings: list[str] = []
        fetch = self._fetch_schema_for_project(project)
        if fetch.schema:
            policy_body = {"table": table, "dialect": dialect, "rules": rules}
            errors.extend(_rls_condition.validate_policy_structural(policy_body, fetch.schema))
        else:
            warnings.append(f"Live schema validation was skipped: {fetch.reason}")
        return errors, warnings

    def _local_rule_errors(self, rules: Any) -> list[str]:
        """Schema-independent checks on ``rules`` (hook for :class:`ClsService`)."""
        return _rls_condition.validate_rules_local(rules)

    def _preview_rules(self, rules: list[dict[str, Any]], dialect: str) -> list[dict[str, Any]]:
        """Per-rule display preview shown by ``--dry-run`` (hook for :class:`ClsService`)."""
        return [
            {
                "principal": rule.get("principal") or rule.get("principals"),
                "condition": _rls_condition.compile_condition_preview(rule["condition"], dialect),
            }
            for rule in rules
        ]

    @staticmethod
    def _with_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        """Attach validation warnings (e.g. the live schema was unavailable) only when there are any."""
        if warnings:
            result["warnings"] = warnings
        return result

    def _raise_invalid(self, errors: list[str]) -> None:
        raise KeboolaApiError(
            message=f"{self.label} policy is invalid: " + "; ".join(errors),
            status_code=400,
            error_code=self.invalid_error_code,
            retryable=False,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_policies(self, alias: str) -> dict[str, Any]:
        """List every ``rls-policy`` object visible to ``alias``'s project."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            raw = client.list_items(self.item_type)
        return {"project": alias, "policies": [self._row_from_item(item) for item in raw]}

    def get_policy(self, alias: str, policy_id: str) -> dict[str, Any]:
        """Fetch one ``rls-policy`` object's full attributes + meta."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            item = client.get_item(self.item_type, policy_id)
        row = self._row_from_item(item)
        row["rules"] = (item.get("attributes") or {}).get("rules", [])
        row["revision"] = (item.get("meta") or {}).get("revision")
        return row

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_policy(
        self,
        alias: str,
        *,
        table: str,
        dialect: str,
        rules: list[dict[str, Any]],
        target_project_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create one ``rls-policy`` object -- always at ``organization``/``targeted`` scope.

        ``dry_run=True`` returns the same preview shape a real write would
        produce (compiled condition previews per rule) without calling the
        metastore's write endpoint at all -- only the (read-only) schema
        fetch used for validation happens.
        """
        project = self._resolve_one_project(alias)
        errors, warnings = self._validate_policy(
            table=table, dialect=dialect, rules=rules, project=project
        )
        if errors:
            self._raise_invalid(errors)

        scope = self._resolve_scope(target_project_ids)
        preview = self._preview_rules(rules, dialect)
        if dry_run:
            return self._with_warnings(
                {
                    "project": alias,
                    "table": table,
                    "dialect": dialect,
                    "scope": scope,
                    "target_project_ids": target_project_ids or [],
                    "preview": preview,
                    "dry_run": True,
                },
                warnings,
            )

        with self._new_metastore_client(project) as client:
            created = client.post_item(
                self.item_type,
                name=table,
                data={"table": table, "dialect": dialect, "rules": rules},
                scope=scope,
                target_project_ids=target_project_ids,
            )
            if scope == "targeted" and target_project_ids:
                client.put_target_projects(
                    self.item_type, created.get("id", ""), target_project_ids
                )
        row = self._row_from_item(created)
        row["preview"] = preview
        return self._with_warnings(row, warnings)

    def update_policy(
        self,
        alias: str,
        policy_id: str,
        *,
        table: str | None = None,
        dialect: str | None = None,
        rules: list[dict[str, Any]] | None = None,
        target_project_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Update one policy object.

        ``put_item`` is a whole-record replace -- fetch the current item
        first and merge only the given overrides onto it, so an ``update``
        call that only changes ``rules`` never silently wipes ``table`` or
        ``dialect`` (the failure mode this repo's own ``merge-request
        resolve`` docs warn about for exactly this kind of PUT-based API).

        ``target_project_ids``: ``None`` keeps the current grants, a non-empty list replaces them, and
        ``[]`` revokes them all (through the dedicated grants call). A policy keeps its current scope
        when its grants are revoked -- scope changes are not done through ``PUT``.
        """
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            current = client.get_item(self.item_type, policy_id)
        attrs = current.get("attributes") or {}
        meta = current.get("meta") or {}

        merged_table = table if table is not None else attrs.get("table", "")
        merged_dialect = dialect if dialect is not None else attrs.get("dialect", "")
        merged_rules = rules if rules is not None else attrs.get("rules", [])
        merged_targets = (
            target_project_ids if target_project_ids is not None else meta.get("targetProjectIds")
        )

        errors, warnings = self._validate_policy(
            table=merged_table, dialect=merged_dialect, rules=merged_rules, project=project
        )
        if errors:
            self._raise_invalid(errors)

        revoking = target_project_ids == [] and bool(meta.get("targetProjectIds"))
        scope = (
            meta.get("scope") or "organization"
            if target_project_ids == []
            else self._resolve_scope(merged_targets)
        )
        preview = self._preview_rules(merged_rules, merged_dialect)
        if dry_run:
            return self._with_warnings(
                {
                    "project": alias,
                    "policy_id": policy_id,
                    "table": merged_table,
                    "dialect": merged_dialect,
                    "scope": scope,
                    "target_project_ids": merged_targets or [],
                    "preview": preview,
                    "dry_run": True,
                },
                warnings,
            )

        with self._new_metastore_client(project) as client:
            updated = client.put_item(
                self.item_type,
                policy_id,
                name=merged_table,
                data={"table": merged_table, "dialect": merged_dialect, "rules": merged_rules},
                scope=scope,
                target_project_ids=merged_targets,
            )
            if revoking:
                client.put_target_projects(self.item_type, policy_id, [])
            elif scope == "targeted" and merged_targets:
                client.put_target_projects(self.item_type, policy_id, merged_targets)
        row = self._row_from_item(updated)
        row["preview"] = preview
        return self._with_warnings(row, warnings)

    def delete_policy(self, alias: str, policy_id: str) -> dict[str, Any]:
        """Delete one ``rls-policy`` object by id."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            client.delete_item(self.item_type, policy_id)
        return {"project": alias, "policy_id": policy_id, "deleted": True}
