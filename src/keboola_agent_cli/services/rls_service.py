"""Row-level security service -- business logic for ``kbagent rls``.

Composes :class:`MetastoreClient` primitives into the ``rls-policy`` CRUD
operations exposed by the CLI, mirroring :class:`SemanticLayerService`'s
shape for the same kind of metastore-backed object type -- but scaled down
to one object type instead of seven.

The one structural rule every write path in this module enforces: an
``rls-policy`` object is **never** created at plain ``project`` scope (the
metastore schema supports only ``organization`` and ``targeted``). The
default is ``targeted`` -- the schema's own default: the policy governs the
owning project plus the target projects it is granted to. ``organization``
governs the table in EVERY project of the organization, so it is only ever
an explicit choice (RFC: "org-wide scope is a deliberate choice an admin
makes, not a default").

Who may write (metastore schema ACL): a project admin (master token with the
admin role) may create/update/delete ``targeted`` policies of its own project
WITHOUT target projects; granting target projects and ``organization`` scope
need the organization-admin role.

The enforcement (keboola-mcp-server ``query_data``) refuses EVERY query of a
project when one policy it loads is invalid -- a dialect other than the
project backend, or one principal with two rules on one table. Both are
refused here before any write.

On a stack whose metastore predates the ``rls-policy`` schema, a real project
answers with a schema-fetch/list/get/post failure (see ``fetch_schema`` and the
``gotchas.md`` entry). Every method here is unit-tested against a mocked
:class:`MetastoreClient`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema

from ..config_store import ConfigStore
from ..errors import ErrorCode, KeboolaApiError
from ..metastore_client import (
    MetastoreClient,
    ObjectScope,
    SemanticType,
    fetch_resolved_schema,
)
from ..models import ProjectConfig
from . import _rls_condition
from ._rls_condition import RLS_DIALECTS
from ._semantic_layer_scope import resolve_target_project_ids
from .base import BaseService, ClientFactory, make_session_aware_client_factory

logger = logging.getLogger(__name__)

RLS_ITEM_TYPE: SemanticType = "rls-policy"

__all__ = ["POLICY_SCOPES", "RLS_DIALECTS", "RLS_ITEM_TYPE", "RlsSchemaFetch", "RlsService"]

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


# The scopes a policy may be authored at (the schema's `x-metastore.scope.supported`).
POLICY_SCOPES: tuple[ObjectScope, ...] = ("targeted", "organization")


@dataclass(frozen=True)
class PolicyValidation:
    """Outcome of :meth:`RlsService._validate_policy`: blocking ``errors`` and non-blocking ``warnings``."""

    errors: list[str]
    warnings: list[str]


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
                resolved = fetch_resolved_schema(client, self.item_type)
                schema = resolved.schema
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
        if resolved.version is None and isinstance(schema.get("versions"), list):
            # `fetch_resolved_schema` hands back the bare version listing when it cannot pick a version
            # (e.g. an empty listing). It is a valid, constraint-free "schema", so validating against it
            # would silently skip every structural check -- treat it as unavailable instead.
            return RlsSchemaFetch(
                schema=None,
                reason=f"the metastore returned only a version listing, no schema, for {self.item_type}",
            )
        try:
            jsonschema.Draft7Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            # A non-empty but malformed schema would make the structural validation raise a traceback;
            # report it as unavailable instead (same as an empty one).
            return RlsSchemaFetch(
                schema=None,
                reason=f"the {self.item_type} schema from the metastore is malformed: {exc.message}",
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
            # The owning project; the metastore omits it for an organization-scope policy.
            "owner_project_id": meta.get("projectId"),
            "source_project_id": meta.get("sourceProjectId"),
            "target_project_ids": meta.get("targetProjectIds") or [],
        }

    def _project_dialect(self, project: ProjectConfig) -> str:
        """The project's backend -- the only dialect the enforcement accepts for its policies."""
        with self._client_factory(project.stack_url, project.token) as client:
            backend = (client.verify_token().default_backend or "").lower()
        if backend not in RLS_DIALECTS:
            self._raise_invalid(
                [
                    f"the project backend is {backend or 'unknown'!r}; policies support {RLS_DIALECTS} only"
                ]
            )
        return backend

    def _resolve_targets(self, alias: str, targets: Sequence[int | str] | None) -> list[int]:
        """``--target-project`` values (alias or project ID, comma lists) -> unique project IDs."""
        return resolve_target_project_ids(
            self._config_store, alias, [str(t) for t in targets or []]
        )

    def _taken_principals(
        self, client: MetastoreClient, table: str, dialect: str, exclude_id: str | None
    ) -> dict[str, str]:
        """Principals other policies on the same table already have a rule for (case-folded).

        Best effort: the metastore lists only the policies this token may read -- for a project admin,
        those its project owns -- so a clash with a policy owned elsewhere is not visible here.
        """
        key = _rls_condition.table_key(table, dialect)
        taken: dict[str, str] = {}
        for item in client.list_items(self.item_type):
            attrs = item.get("attributes") or {}
            if item.get("id") == exclude_id or not isinstance(attrs.get("table"), str):
                continue
            if _rls_condition.table_key(attrs["table"], dialect) != key:
                continue
            for rule in attrs.get("rules") or []:
                if isinstance(rule, dict):
                    for name in _rls_condition.rule_principals(rule):
                        taken.setdefault(str(name).casefold(), f"policy {item.get('id')}")
        return taken

    def _validate_policy(
        self,
        client: MetastoreClient,
        project: ProjectConfig,
        *,
        policy: dict[str, Any],
        backend: str,
        exclude_id: str | None = None,
    ) -> PolicyValidation:
        """Run every check this module can on the WHOLE policy, local checks first.

        Local (schema-independent) checks always run: the dialect matches the project backend, the
        rules are well-formed and no principal has two rules on the table (within the policy or across
        the other visible policies). Structural (Draft7, against the live schema) checks run when the
        fetch succeeds -- when it doesn't, the caller gets a warning, not a block (see
        :class:`RlsSchemaFetch`). The whole policy is validated even for a partial update, because the
        metastore's PATCH validates only the keys it receives.
        """
        table, dialect, rules = policy["table"], policy["dialect"], policy["rules"]
        errors: list[str] = []
        if dialect != backend:
            errors.append(
                f"dialect {dialect!r} does not match the project backend {backend!r} "
                "(the enforcement refuses every query of a project with such a policy)"
            )
        errors.extend(self._local_rule_errors(rules))
        if errors:
            return PolicyValidation(errors, [])
        errors.extend(
            _rls_condition.duplicate_principals(
                rules, self._taken_principals(client, table, dialect, exclude_id)
            )
        )
        if errors:
            return PolicyValidation(errors, [])

        fetch = self._fetch_schema_for_project(project)
        if not fetch.schema:
            return PolicyValidation([], [f"Live schema validation was skipped: {fetch.reason}"])
        return PolicyValidation(_rls_condition.validate_policy_structural(policy, fetch.schema), [])

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

    @staticmethod
    def _raise_usage(message: str) -> None:
        raise KeboolaApiError(
            message=message, status_code=400, error_code=ErrorCode.INVALID_ARGUMENT, retryable=False
        )

    def _already_exists(self, table: str, exc: KeboolaApiError) -> KeboolaApiError:
        """The client's generic 409 message talks about semantic models; policies are named by table."""
        return KeboolaApiError(
            message=(
                f"An {self.label} policy for table {table!r} already exists in this project. "
                f"Change it with `{self.label.lower()} update --policy-id ...`, "
                f"or `{self.label.lower()} delete` it first."
            ),
            status_code=exc.status_code,
            error_code=ErrorCode.ALREADY_EXISTS,
            retryable=False,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_policies(self, alias: str) -> dict[str, Any]:
        """List every policy object visible to ``alias``'s project."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            raw = client.list_items(self.item_type)
        return {"project": alias, "policies": [self._row_from_item(item) for item in raw]}

    def get_policy(self, alias: str, policy_id: str) -> dict[str, Any]:
        """Fetch one policy object's full attributes + meta."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            return self._detail_row(client.get_item(self.item_type, policy_id))

    def _detail_row(self, item: dict[str, Any]) -> dict[str, Any]:
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
        rules: list[dict[str, Any]],
        dialect: str | None = None,
        scope: str = "targeted",
        target_projects: Sequence[int | str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create one policy -- at ``targeted`` scope (default) or, explicitly, ``organization``.

        ``dialect`` defaults to the project backend and must equal it. ``target_projects`` (aliases or
        project IDs) only with ``targeted``; they go in the create request itself, which stores the
        grants in the same transaction (and needs the organization-admin role). ``dry_run=True``
        returns the preview without calling the write endpoint.
        """
        if scope not in POLICY_SCOPES:
            self._raise_usage(f"scope must be one of {POLICY_SCOPES}, got {scope!r}")
        if target_projects and scope != "targeted":
            self._raise_usage("target projects require scope 'targeted'")
        target_ids = self._resolve_targets(alias, target_projects)
        project = self._resolve_one_project(alias)
        backend = self._project_dialect(project)
        policy = {"table": table, "dialect": dialect or backend, "rules": rules}

        with self._new_metastore_client(project) as client:
            check = self._validate_policy(client, project, policy=policy, backend=backend)
            if check.errors:
                self._raise_invalid(check.errors)
            preview = self._preview_rules(rules, policy["dialect"])
            if dry_run:
                return self._with_warnings(
                    {
                        "project": alias,
                        **policy,
                        "scope": scope,
                        "target_project_ids": target_ids,
                        "preview": preview,
                        "dry_run": True,
                    },
                    check.warnings,
                )
            try:
                created = client.post_item(
                    self.item_type,
                    name=table,
                    data=policy,
                    scope="organization" if scope == "organization" else "targeted",
                    target_project_ids=target_ids or None,
                )
            except KeboolaApiError as exc:
                if exc.error_code == ErrorCode.ALREADY_EXISTS:
                    raise self._already_exists(table, exc) from exc
                raise
        row = self._row_from_item(created)
        row["preview"] = preview
        return self._with_warnings(row, check.warnings)

    def update_policy(
        self,
        alias: str,
        policy_id: str,
        *,
        table: str | None = None,
        dialect: str | None = None,
        rules: list[dict[str, Any]] | None = None,
        target_projects: Sequence[int | str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Update one policy: only the given fields change.

        Reads the policy to validate the MERGED result (the metastore's PATCH validates only the keys
        it receives) and for the ``--dry-run`` preview, then sends a ``PATCH`` with only the changed
        keys -- unknown keys and concurrent changes to other keys survive.

        ``target_projects``: ``None`` keeps the grants, a list replaces them, ``[]`` revokes them all.
        Grants change first (an organization-admin-only call): when that is refused, nothing is written.
        The result is re-read after the write, so it shows the grants as they now are.
        """
        changes = {
            key: value
            for key, value in (("table", table), ("dialect", dialect), ("rules", rules))
            if value is not None
        }
        if not changes and target_projects is None:
            self._raise_usage("nothing to update: pass a table, dialect, rules or target projects")
        target_ids = (
            self._resolve_targets(alias, target_projects) if target_projects is not None else None
        )
        project = self._resolve_one_project(alias)
        backend = self._project_dialect(project)

        with self._new_metastore_client(project) as client:
            current = client.get_item(self.item_type, policy_id)
            attrs = current.get("attributes") or {}
            meta = current.get("meta") or {}
            scope = meta.get("scope", "")
            if target_ids is not None and scope != "targeted":
                self._raise_invalid(
                    [
                        (
                            f"the policy has {scope!r} scope, which has no target projects "
                            "(organization scope already applies everywhere and cannot be narrowed)"
                        )
                    ]
                )
            policy = {
                "table": attrs.get("table", ""),
                "dialect": attrs.get("dialect", ""),
                "rules": attrs.get("rules", []),
                **changes,
            }
            check = self._validate_policy(
                client, project, policy=policy, backend=backend, exclude_id=policy_id
            )
            if check.errors:
                self._raise_invalid(check.errors)
            preview = self._preview_rules(policy["rules"], policy["dialect"])
            if dry_run:
                return self._with_warnings(
                    {
                        "project": alias,
                        "policy_id": policy_id,
                        **policy,
                        "scope": scope,
                        "target_project_ids": (
                            target_ids
                            if target_ids is not None
                            else meta.get("targetProjectIds") or []
                        ),
                        "preview": preview,
                        "dry_run": True,
                    },
                    check.warnings,
                )
            if target_ids is not None:
                client.put_target_projects(self.item_type, policy_id, target_ids)
            if changes:
                try:
                    client.patch_item(self.item_type, policy_id, name=table, data=changes)
                except KeboolaApiError as exc:
                    if exc.error_code == ErrorCode.ALREADY_EXISTS:
                        raise self._already_exists(policy["table"], exc) from exc
                    raise
            row = self._detail_row(client.get_item(self.item_type, policy_id))
        row["preview"] = preview
        return self._with_warnings(row, check.warnings)

    def delete_policy(self, alias: str, policy_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Delete one policy by id; ``dry_run`` shows what would be deleted (one read, no write)."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            if dry_run:
                return {
                    "project": alias,
                    "policy": self._detail_row(client.get_item(self.item_type, policy_id)),
                    "dry_run": True,
                }
            client.delete_item(self.item_type, policy_id)
        return {"project": alias, "policy_id": policy_id, "deleted": True}
