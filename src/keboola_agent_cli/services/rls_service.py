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

Policy schema 1.2.0 (the metastore default where it is deployed): a rule selects identities by
``subjects`` (``<idp>|<sub>``) or IdP ``groups``, never by email (AJDA-3680); the legacy 1.0.0/1.1.0
``principal`` / ``principals`` of stored policies are read, not authored; within a policy every rule
matching one identity applies and the conditions combine with OR (so one
identity in several rules is fine); several policies on one table combine
with AND, so a policy added to an already readable table narrows access; an RLS ``default``
covers an identified reader no rule matches (absent = an ordinary reader gets no rows). A dialect other than the project
backend is refused here before any write; ``dialect`` is optional from schema 1.1.0 on and is
left out unless ``--dialect`` is passed, so a policy (an ``organization`` one above all) does not
pin its table to one backend (the engine compiles for the workspace's own).

On a stack whose metastore predates the ``rls-policy`` schema, a real project
answers with a schema-fetch/list/get/post failure (see ``fetch_schema`` and the
``gotchas.md`` entry).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

import jsonschema

from ..config_store import ConfigStore
from ..errors import ErrorCode, KeboolaApiError
from ..json_utils import draft7_errors
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

RLS_ITEM_TYPE: SemanticType = "rls-policy"

__all__ = ["RLS_ITEM_TYPE", "PolicyScope", "RlsSchemaFetch", "RlsService"]

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


class PolicyScope(StrEnum):
    """The scopes a policy may be authored at (the schema's ``x-metastore.scope.supported``)."""

    TARGETED = "targeted"
    ORGANIZATION = "organization"


def _schema_requires_dialect(schema: dict[str, Any] | None) -> bool:
    """Whether a policy must carry ``dialect``: the schema says so (1.0.0) or is unknown."""
    return schema is None or "dialect" in (schema.get("required") or [])


@dataclass(frozen=True)
class PolicyValidation:
    """Outcome of :meth:`RlsService._validate_policy`: blocking ``errors`` and non-blocking ``warnings``."""

    errors: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class RlsSchemaFetch:
    """The live policy schema, or ``schema=None`` + ``reason`` when it is unavailable (never blocks)."""

    schema: dict[str, Any] | None
    reason: str | None


class RlsService(BaseService):
    """Business logic for the ``rls`` command group.

    :class:`ClsService` overrides only ``item_type`` / ``label`` / ``invalid_error_code`` and the two
    rule hooks (:meth:`_local_rule_errors`, :meth:`_preview_rules`).
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
        """The live schema (``rls schema``); writes reuse their open client instead."""
        project = self._resolve_one_project(alias)
        with self._new_metastore_client(project) as client:
            return self._fetch_schema(client)

    def _fetch_schema(self, client: MetastoreClient, version: str | None = None) -> RlsSchemaFetch:
        """Fetch the live policy schema (``version``, else the default), degrading when it is merely *unavailable*.

        A network error, an empty/malformed schema or a non-auth ``KeboolaApiError`` (most likely: the
        object type isn't registered on the stack's metastore) degrades to ``schema=None`` + ``reason``
        and never blocks a caller. An authentication/authorization failure (``INVALID_TOKEN``,
        ``MISSING_MASTER_TOKEN``, access denied) is a real problem the user must see, so it is
        re-raised instead of being reported as a missing schema.
        """
        try:
            resolved = fetch_resolved_schema(client, self.item_type, version)
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

    def _own_project_id(self, project: ProjectConfig) -> int | None:
        """The project's ID: the registered one, else one token verification; ``None`` when neither names it."""
        if project.project_id is not None:
            return project.project_id
        with self._client_factory(project.stack_url, project.token) as client:
            return client.verify_token().project_id

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

    def _validate_policy(
        self,
        fetch: RlsSchemaFetch,
        *,
        policy: dict[str, Any],
        backend: str,
        given_dialect: str | None = None,
        stored_version: str | None = None,
    ) -> PolicyValidation:
        """Run every check this module can on the WHOLE policy, local checks first.

        Local (schema-independent) checks always run: a ``--dialect`` the caller passes (``given_dialect``;
        a dialect already stored on the policy is never checked, the engine does not read it) matches the
        project backend, and the rules (and an RLS ``default``) are well-formed. Structural (Draft7, against
        the live schema) checks run when the fetch succeeds -- when it doesn't, the caller gets a warning,
        not a block (see :class:`RlsSchemaFetch`). The whole policy is validated even for a partial update,
        because the metastore's PATCH validates only the keys it receives. ``stored_version`` is the schema
        version an updated policy is stamped with: the metastore validates a PATCH against that version, not
        the default one, and the version checks below name it.
        """
        errors: list[str] = []
        if given_dialect is not None and given_dialect != backend:
            errors.append(
                f"dialect {given_dialect!r} does not match the project backend {backend!r} "
                "(a policy is written for the backend of its project; omit --dialect to leave it out)"
            )
        errors.extend(self._local_rule_errors(policy["rules"]))
        errors.extend(self._default_errors(policy.get("default")))
        if errors:
            return PolicyValidation(errors, [])

        if not fetch.schema:
            return PolicyValidation([], [f"Live schema validation was skipped: {fetch.reason}"])
        errors = draft7_errors(policy, fetch.schema)
        if _rls_condition.rules_use_subjects(policy["rules"]) and (
            old := _rls_condition.schema_older_than(
                fetch.schema, _rls_condition.SUBJECTS_MIN_SCHEMA_VERSION
            )
        ):
            errors.append(
                f"'subjects' needs {self.item_type} schema 1.2.0 or later; "
                + (
                    f"this policy is stored at {old} and keeps that version (recreate it to use subjects)"
                    if stored_version
                    else f"this metastore serves {old}, which selects by email only (the Query Service "
                    "release with subject rules and the Metastore migration are not deployed on this "
                    "stack yet)"
                )
            )
        if _rls_condition.rules_use_wildcard(policy["rules"]) and (
            old := _rls_condition.schema_lacks_wildcard(fetch.schema)
        ):
            errors.append(
                f"the wildcard {_rls_condition.WILDCARD!r} needs {self.item_type} schema 1.1.0 or later; "
                f"this {'policy is stored at' if stored_version else 'metastore serves'} {old}, "
                "where it is a literal that matches nobody"
            )
        return PolicyValidation(errors, [])

    def _refuse_legacy_selectors(self, rules: Any) -> None:
        """A usage error (exit 2) for ``rules`` that select by email: the rules a caller supplies are written to a
        new policy version, which selects by ``subjects`` or ``groups`` only. The stored rules of an update that
        keeps them are not checked here: they may be legacy and stay as they are."""
        if errors := _rls_condition.legacy_selector_errors(rules):
            self._raise_usage("; ".join(errors))

    def _local_rule_errors(self, rules: Any) -> list[str]:
        """Schema-independent checks on ``rules`` (hook for :class:`ClsService`)."""
        return _rls_condition.validate_rules_local(rules)

    @staticmethod
    def _default_errors(default: Any) -> list[str]:
        """Checks on the policy-level ``default`` condition (hook: :class:`ClsService` has none)."""
        if default is None:
            return []
        return [f"default.{err}" for err in _rls_condition.validate_condition_ops(default)]

    def _preview_rules(self, rules: list[dict[str, Any]], dialect: str) -> list[dict[str, Any]]:
        """Per-rule display preview shown by ``--dry-run`` (hook for :class:`ClsService`)."""
        return [
            {
                **_rls_condition.rule_selector(rule),
                "condition": _rls_condition.compile_condition_preview(rule["condition"], dialect),
            }
            for rule in rules
        ]

    def _preview(self, policy: dict[str, Any], backend: str) -> dict[str, Any]:
        """The ``preview`` (and, for RLS, ``default_preview``) keys of a write/dry-run result.

        Rendered for the policy's ``dialect``, or the project backend when it has none.
        """
        dialect = policy.get("dialect") or backend
        preview: dict[str, Any] = {"preview": self._preview_rules(policy["rules"], dialect)}
        if policy.get("default") is not None:
            preview["default_preview"] = _rls_condition.compile_condition_preview(
                policy["default"], dialect
            )
        return preview

    @staticmethod
    def _with_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        """Attach validation warnings (e.g. the live schema was unavailable) only when there are any."""
        if warnings:
            result["warnings"] = warnings
        return result

    def _raise_invalid(self, errors: list[str]) -> NoReturn:
        raise KeboolaApiError(
            message=f"{self.label} policy is invalid: " + "; ".join(errors),
            status_code=400,
            error_code=self.invalid_error_code,
            retryable=False,
        )

    @staticmethod
    def _raise_usage(message: str) -> NoReturn:
        raise KeboolaApiError(
            message=message, status_code=400, error_code=ErrorCode.INVALID_ARGUMENT, retryable=False
        )

    @property
    def _conflict_hint(self) -> str:
        """The 409 remedy: policies are named by their table, one per table and project."""
        group = self.label.lower()
        return (
            f"This project already has an {self.label} policy for that table: change it with "
            f"`{group} update --policy-id ...`, or `{group} delete` it first."
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
        attrs = item.get("attributes") or {}
        row = self._row_from_item(item)
        row["rules"] = attrs.get("rules", [])
        if attrs.get("default") is not None:
            row["default"] = attrs["default"]
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
        default: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create one policy -- at ``targeted`` scope (default) or, explicitly, ``organization``.

        ``dialect`` is left out of the policy unless given (it is optional from schema 1.1.0 on and
        the engine compiles for the workspace's own backend); given, it must equal the project
        backend. A metastore whose schema still requires it (1.0.0), or whose schema could not be
        read, gets the project backend. ``default`` (RLS only) is the
        condition for identities no rule matches; omitted, an ordinary reader gets no rows and an administrator the 403. ``target_projects`` (aliases or
        project IDs) only with ``targeted``; they go in the create request itself, which stores the
        grants in the same transaction (and needs the organization-admin role). ``dry_run=True``
        returns the preview without calling the write endpoint.
        """
        self._refuse_legacy_selectors(rules)
        if scope not in tuple(PolicyScope):
            self._raise_usage(f"scope must be one of {tuple(PolicyScope)}, got {scope!r}")
        if target_projects and scope != "targeted":
            self._raise_usage("target projects require scope 'targeted'")
        target_ids = self._resolve_targets(alias, target_projects)
        project = self._resolve_one_project(alias)
        backend = self._project_dialect(project)
        policy: dict[str, Any] = {"table": table, "rules": rules}
        if default is not None:
            policy["default"] = default

        with self._new_metastore_client(project) as client:
            fetch = self._fetch_schema(client)
            if dialect is not None or _schema_requires_dialect(fetch.schema):
                policy["dialect"] = dialect or backend
            check = self._validate_policy(
                fetch, policy=policy, backend=backend, given_dialect=dialect
            )
            if check.errors:
                self._raise_invalid(check.errors)
            preview = self._preview(policy, backend)
            if dry_run:
                return self._with_warnings(
                    {
                        "project": alias,
                        **policy,
                        "scope": scope,
                        "target_project_ids": target_ids,
                        **preview,
                        "dry_run": True,
                    },
                    check.warnings,
                )
            created = client.post_item(
                self.item_type,
                name=table,
                data=policy,
                scope=cast(ObjectScope, str(scope)),  # checked against PolicyScope above
                target_project_ids=target_ids or None,
                conflict_hint=self._conflict_hint,
            )
        return self._with_warnings({**self._detail_row(created), **preview}, check.warnings)

    def update_policy(
        self,
        alias: str,
        policy_id: str,
        *,
        table: str | None = None,
        dialect: str | None = None,
        rules: list[dict[str, Any]] | None = None,
        target_projects: Sequence[int | str] | None = None,
        default: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Update one policy: only the given fields change.

        Reads the policy to validate the MERGED result (the metastore's PATCH validates only the keys
        it receives) and for the ``--dry-run`` preview, then sends a ``PATCH`` with only the changed
        keys -- unknown keys and concurrent changes to other keys survive.

        ``target_projects``: ``None`` keeps the grants, a list replaces them, ``[]`` revokes them all.
        Grants change first (an organization-admin-only call): when that is refused, nothing is written;
        when the rules PATCH then fails, the old grants are put back (and the error says whether that worked).
        The result is re-read after the write, so it shows the grants as they now are. ``default``
        replaces the RLS default condition; a PATCH cannot remove it (recreate the policy for that).
        """
        self._refuse_legacy_selectors(rules)
        changes = {
            key: value
            for key, value in (
                ("table", table),
                ("dialect", dialect),
                ("rules", rules),
                ("default", default),
            )
            if value is not None
        }
        if not changes and target_projects is None:
            self._raise_usage(
                "nothing to update: pass a table, dialect, rules, default or target projects"
            )
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
            owner_id = meta.get("projectId")
            # A project whose ID cannot be resolved is treated as not owning the policy.
            owned = owner_id is None or owner_id == self._own_project_id(project)
            if target_ids is not None and changes and not owned:
                # The grants are readable (and a rules PATCH is found) only through the owning project, so a
                # failed PATCH could not put the old grants back.
                self._raise_invalid(
                    [
                        (
                            f"the policy is owned by project {owner_id}: change its target projects from that "
                            "project, or in a separate `rls update` without rules (an organization admin)"
                        )
                    ]
                )
            if target_ids is not None and scope != "targeted":
                self._raise_invalid(
                    [
                        (
                            f"the policy has {scope!r} scope, which has no target projects "
                            "(organization scope already applies everywhere and cannot be narrowed)"
                        )
                    ]
                )
            # The metastore validates a PATCH against the schema version the policy is stamped with (it never
            # re-stamps on update), so the merged policy is checked against that version, not the default one.
            stored_version = meta.get("schemaVersion")
            fetch = self._fetch_schema(client, stored_version)
            policy = {
                "table": attrs.get("table", ""),
                "rules": attrs.get("rules", []),
                **({"default": attrs["default"]} if attrs.get("default") is not None else {}),
                **changes,
            }
            # A policy of schema 1.1.0 or later may omit `dialect` (the engine does not read it): keep it omitted unless it is
            # stored, being changed, or the schema requires it.
            if "dialect" not in policy and (
                attrs.get("dialect") or _schema_requires_dialect(fetch.schema)
            ):
                policy["dialect"] = attrs.get("dialect") or backend
            check = self._validate_policy(
                fetch,
                policy=policy,
                backend=backend,
                given_dialect=dialect,
                stored_version=stored_version,
            )
            if check.errors:
                self._raise_invalid(check.errors)
            preview = self._preview(policy, backend)
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
                            else (meta.get("targetProjectIds") or [])
                            if owned
                            else None
                        ),
                        **preview,
                        "dry_run": True,
                    },
                    check.warnings,
                )
            previous_ids = meta.get("targetProjectIds") or []
            if target_ids is not None:
                client.put_target_projects(self.item_type, policy_id, target_ids)
            if changes:
                try:
                    client.patch_item(
                        self.item_type,
                        policy_id,
                        name=table,
                        data=changes,
                        conflict_hint=self._conflict_hint,
                    )
                except KeboolaApiError as exc:
                    if target_ids is None:
                        raise
                    raise self._grants_after_failed_patch(
                        client, policy_id, previous_ids, exc
                    ) from exc
            row = self._detail_row(client.get_item(self.item_type, policy_id))
        return self._with_warnings({**row, **preview}, check.warnings)

    def _grants_after_failed_patch(
        self,
        client: MetastoreClient,
        policy_id: str,
        previous_ids: list[int],
        failure: KeboolaApiError,
    ) -> KeboolaApiError:
        """Put the target projects back after the rules PATCH failed, and say what state is left.

        The grants are written before the rules (an organization-admin-only call, so a refusal writes
        nothing), which leaves the NEW grants on the OLD rules when the PATCH fails: the new target
        projects would read the old rules. The old grants are restored; the returned error carries the
        original failure and tells whether that worked.
        """
        try:
            client.put_target_projects(self.item_type, policy_id, previous_ids)
            note = "The target projects were put back as they were; nothing was changed."
        except KeboolaApiError as restore:
            note = (
                "The target projects had already been changed and could not be put back "
                f"({restore.message}); they were {sorted(previous_ids)} before. Set them again with "
                "`--target-project`."
            )
        return KeboolaApiError(
            message=f"{failure.message} {note}",
            status_code=failure.status_code,
            error_code=failure.error_code,
            retryable=failure.retryable,
            details=failure.details,
        )

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
            try:
                client.delete_item(self.item_type, policy_id)
            except KeboolaApiError as exc:
                if exc.status_code != 409:
                    raise
                # A policy-binding lists it (references are onDelete: restrict).
                raise KeboolaApiError(
                    message=(
                        f"The metastore refused to delete {self.label} policy {policy_id}: a "
                        "policy-binding still lists it. Find it with `rls bindings`, then rebind "
                        "the workspace without it (`rls bind`) or remove the binding "
                        f"(`rls unbind`). {exc.message}"
                    ),
                    status_code=409,
                    error_code=ErrorCode.API_ERROR,
                    retryable=False,
                ) from exc
        return {"project": alias, "policy_id": policy_id, "deleted": True}
