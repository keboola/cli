"""Policy bindings -- business logic for ``kbagent rls bind|unbind|bindings|bindings-sweep``.

A ``policy-binding`` metastore object restricts one governed workspace (or data app) to a subset of
the project's ``rls-policy`` / ``cls-policy`` objects. It is named by the canonical KID of the
workspace, ``KID:<stack>:project-<id>:workspace-<id>``, and holds ``kid`` plus ``rlsPolicyUUIDs``
and/or ``clsPolicyUUIDs``. Query Service reads it on every governed query:

- a KID with no binding gets EVERY policy of the project;
- a KID with a binding gets only the listed policies -- a binding narrows, it never grants.

The stack segment is not exposed by any API kbagent can call (Query Service takes it from its own
configuration), so the caller passes it. The metastore exposes no reverse-reference lookup: whether
something still references a binding is known only when a delete is refused (HTTP 409).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, NoReturn

from ..config_store import ConfigStore
from ..constants import ENV_STACK_ID, GOVERNED_WORKSPACE_LOGIN_TYPE, STACK_ID_EXAMPLE
from ..errors import ErrorCode, KeboolaApiError
from ..metastore_client import MetastoreClient, SemanticType
from ..models import ProjectConfig
from .base import BaseService, ClientFactory, make_session_aware_client_factory

BINDING_TYPE: SemanticType = "policy-binding"
POLICY_TYPES: dict[str, SemanticType] = {"rls": "rls-policy", "cls": "cls-policy"}
MAX_POLICIES_PER_LIST = 100  # the schema's maxItems
_SWEEP_STATUSES = ("orphaned", "deleted", "kept", "failed")

STACK_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
WORKSPACE_KID_RE = re.compile(
    r"^KID:(?P<stack>[a-z0-9]+(?:-[a-z0-9]+)*):project-(?P<project>0|[1-9][0-9]*)"
    r":workspace-(?P<workspace>0|[1-9][0-9]*)$"
)
_APP_SEGMENT_RE = re.compile(r":app-[A-Za-z0-9._%-]+")

REFERENCES_NOTE = (
    "The metastore exposes no reverse-reference lookup, so references to a binding are not "
    "checked before a delete; the metastore refuses (HTTP 409) a delete that a reference blocks."
)

__all__ = [
    "BINDING_TYPE",
    "REFERENCES_NOTE",
    "KidKind",
    "PolicyBindingService",
    "parse_kid",
    "workspace_kid",
]

MetastoreClientFactory = Callable[[str, str], MetastoreClient]


class KidKind(StrEnum):
    WORKSPACE = "workspace"
    APP = "app"
    OTHER = "other"


class WorkspaceState(StrEnum):
    EXISTS = "exists"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParsedKid:
    kind: KidKind
    project_id: int | None = None
    workspace_id: int | None = None


@dataclass(frozen=True)
class WorkspaceCheck:
    """Whether a workspace exists; ``error`` is set when the check itself failed (``UNKNOWN``)."""

    state: WorkspaceState
    workspace: dict[str, Any] | None = None
    error: KeboolaApiError | None = None


def _usage(message: str) -> NoReturn:
    raise KeboolaApiError(
        message=message, status_code=400, error_code=ErrorCode.INVALID_ARGUMENT, retryable=False
    )


def workspace_kid(stack_id: str | None, project_id: int, workspace_id: int) -> str:
    """The canonical KID of a workspace; INVALID_ARGUMENT when the stack id is missing or malformed."""
    if not stack_id:
        _usage(
            f"--stack-id (or {ENV_STACK_ID}) is required: the stack segment of the workspace KID, "
            f"the same value Query Service is configured with (QUERY_RLS_STACK_ID), for example "
            f"{STACK_ID_EXAMPLE!r} on the AWS dev stack. No API exposes it, so kbagent cannot "
            "derive it."
        )
    if not STACK_ID_RE.match(stack_id):
        _usage(
            f"--stack-id {stack_id!r} is not a stack id: lowercase letters and digits in "
            f"dash-separated words, for example {STACK_ID_EXAMPLE!r}."
        )
    if workspace_id < 0:
        _usage(f"--workspace-id must not be negative, got {workspace_id}.")
    kid = f"KID:{stack_id}:project-{project_id}:workspace-{workspace_id}"
    if not WORKSPACE_KID_RE.match(kid):
        _usage(f"{kid!r} is not a canonical workspace KID.")
    return kid


def parse_kid(kid: str) -> ParsedKid:
    """Classify a stored KID: a canonical workspace KID, an app KID, or anything else."""
    match = WORKSPACE_KID_RE.match(kid)
    if match:
        return ParsedKid(
            KidKind.WORKSPACE, int(match.group("project")), int(match.group("workspace"))
        )
    if _APP_SEGMENT_RE.search(kid):
        return ParsedKid(KidKind.APP)
    return ParsedKid(KidKind.OTHER)


def _policy_ids(raw: Sequence[str] | None, flag: str) -> list[str]:
    """Deduplicated, lower-cased UUIDs in input order; INVALID_ARGUMENT on a malformed one."""
    seen: list[str] = []
    for value in raw or []:
        try:
            normalized = str(uuid.UUID(value))
        except ValueError:
            _usage(f"{flag} {value!r} is not a UUID (a policy id from `rls list` / `cls list`).")
        if normalized not in seen:
            seen.append(normalized)
    if len(seen) > MAX_POLICIES_PER_LIST:
        _usage(f"{flag}: at most {MAX_POLICIES_PER_LIST} policies per binding, got {len(seen)}.")
    return seen


def _binding_row(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") or {}
    meta = item.get("meta") or {}
    kid = str(attrs.get("kid") or meta.get("name") or "")
    parsed = parse_kid(kid)
    return {
        "id": item.get("id", ""),
        "kid": kid,
        "kind": parsed.kind.value,
        "workspace_id": parsed.workspace_id,
        "kid_project_id": parsed.project_id,
        "rls_policy_ids": list(attrs.get("rlsPolicyUUIDs") or []),
        "cls_policy_ids": list(attrs.get("clsPolicyUUIDs") or []),
        "updated": meta.get("lastUpdated"),
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class PolicyBindingService(BaseService):
    """Business logic for the policy-binding commands of the ``rls`` group."""

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        metastore_client_factory: MetastoreClientFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._metastore_factory: MetastoreClientFactory = (
            metastore_client_factory
            or make_session_aware_client_factory(config_store, MetastoreClient)
        )
        self._now = now or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _project(self, alias: str) -> ProjectConfig:
        return self.resolve_projects([alias])[alias]

    def _project_id(self, project: ProjectConfig, storage: Any) -> int:
        if project.project_id is not None:
            return project.project_id
        project_id = storage.verify_token().project_id
        if project_id is None:
            raise KeboolaApiError(
                message="The token does not name its project, so the workspace KID cannot be built.",
                status_code=400,
                error_code=ErrorCode.API_ERROR,
                retryable=False,
            )
        return int(project_id)

    @staticmethod
    def _find_binding(client: MetastoreClient, kid: str) -> dict[str, Any] | None:
        for item in client.list_items(BINDING_TYPE):
            if _binding_row(item)["kid"] == kid:
                return item
        return None

    @staticmethod
    def _check_workspace(storage: Any, workspace_id: int) -> WorkspaceCheck:
        """Production first; on a 404 every dev branch too. A failed check is ``UNKNOWN``, never gone."""
        try:
            return WorkspaceCheck(WorkspaceState.EXISTS, storage.get_workspace(workspace_id))
        except KeboolaApiError as exc:
            if exc.status_code != 404:
                return WorkspaceCheck(WorkspaceState.UNKNOWN, error=exc)
        try:
            branches = [b for b in storage.list_dev_branches() if not b.get("isDefault")]
            for branch in branches:
                try:
                    found = storage.get_workspace(workspace_id, branch_id=int(branch["id"]))
                except KeboolaApiError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                return WorkspaceCheck(WorkspaceState.EXISTS, found)
        except KeboolaApiError as exc:
            return WorkspaceCheck(WorkspaceState.UNKNOWN, error=exc)
        return WorkspaceCheck(WorkspaceState.GONE)

    @staticmethod
    def _missing_policies(client: MetastoreClient, kind: str, ids: list[str]) -> list[str]:
        missing: list[str] = []
        for policy_id in ids:
            try:
                client.get_item(POLICY_TYPES[kind], policy_id)
            except KeboolaApiError as exc:
                if exc.status_code != 404:
                    raise
                missing.append(f"{kind}-policy {policy_id}")
        return missing

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def bind(
        self,
        alias: str,
        workspace_id: int,
        *,
        stack_id: str | None,
        rls_policies: Sequence[str] | None = None,
        cls_policies: Sequence[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or replace the binding of one workspace (idempotent upsert)."""
        rls_ids = _policy_ids(rls_policies, "--rls-policy")
        cls_ids = _policy_ids(cls_policies, "--cls-policy")
        if not rls_ids and not cls_ids:
            _usage("Pass at least one --rls-policy or --cls-policy: a binding lists policies.")
        project = self._project(alias)
        warnings: list[str] = []
        with self._client_factory(project.stack_url, project.token) as storage:
            kid = workspace_kid(stack_id, self._project_id(project, storage), workspace_id)
            check = self._check_workspace(storage, workspace_id)
        if check.state is WorkspaceState.UNKNOWN and check.error is not None:
            raise check.error
        if check.state is WorkspaceState.GONE:
            raise KeboolaApiError(
                message=f"Workspace {workspace_id} does not exist in project '{alias}' "
                "(production or any dev branch).",
                status_code=404,
                error_code=ErrorCode.NOT_FOUND,
                retryable=False,
            )
        login_type = ((check.workspace or {}).get("connection") or {}).get("loginType", "")
        if login_type != GOVERNED_WORKSPACE_LOGIN_TYPE:
            warnings.append(
                f"Workspace {workspace_id} has login type {login_type or 'unknown'!r}, not "
                f"{GOVERNED_WORKSPACE_LOGIN_TYPE!r}: Query Service applies a binding only to a "
                "governed workspace."
            )

        data: dict[str, Any] = {"kid": kid}
        if rls_ids:
            data["rlsPolicyUUIDs"] = rls_ids
        if cls_ids:
            data["clsPolicyUUIDs"] = cls_ids
        with self._metastore_factory(project.stack_url, project.token) as client:
            missing = self._missing_policies(client, "rls", rls_ids)
            missing += self._missing_policies(client, "cls", cls_ids)
            if missing:
                raise KeboolaApiError(
                    message=(
                        f"Not found in project '{alias}': {', '.join(missing)}. A binding lists "
                        "existing policies only (`rls list` / `cls list`); a project admin reads "
                        "only the policies its own project owns."
                    ),
                    status_code=404,
                    error_code=ErrorCode.NOT_FOUND,
                    retryable=False,
                )
            existing = self._find_binding(client, kid)
            result: dict[str, Any] = {
                "project": alias,
                "workspace_id": workspace_id,
                "kid": kid,
                "rls_policy_ids": rls_ids,
                "cls_policy_ids": cls_ids,
                "login_type": login_type,
            }
            if existing is not None:
                previous = _binding_row(existing)
                result["previous"] = {
                    "rls_policy_ids": previous["rls_policy_ids"],
                    "cls_policy_ids": previous["cls_policy_ids"],
                }
            if dry_run:
                result |= {
                    "action": "replace" if existing else "create",
                    "binding_id": existing.get("id") if existing else None,
                    "dry_run": True,
                }
                return self._with_warnings(result, warnings)
            stored, action = self._upsert(client, kid, data, existing)
        result |= {"action": action, "binding_id": stored.get("id", "")}
        return self._with_warnings(result, warnings)

    def _upsert(
        self,
        client: MetastoreClient,
        kid: str,
        data: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        """PUT (replaces both lists) when bound, else POST; a POST that lost a race is PUT once."""
        if existing is None:
            try:
                return client.post_item(BINDING_TYPE, name=kid, data=data), "created"
            except KeboolaApiError as exc:
                if exc.error_code != ErrorCode.ALREADY_EXISTS:
                    raise
                existing = self._find_binding(client, kid)
                if existing is None:
                    raise
        return client.put_item(BINDING_TYPE, existing["id"], name=kid, data=data), "replaced"

    def unbind(
        self, alias: str, workspace_id: int, *, stack_id: str | None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Delete the binding of one workspace; ``dry_run`` only looks it up."""
        project = self._project(alias)
        with self._client_factory(project.stack_url, project.token) as storage:
            kid = workspace_kid(stack_id, self._project_id(project, storage), workspace_id)
        result: dict[str, Any] = {"project": alias, "workspace_id": workspace_id, "kid": kid}
        with self._metastore_factory(project.stack_url, project.token) as client:
            existing = self._find_binding(client, kid)
            if existing is None:
                return result | {"bound": False, "deleted": False}
            row = _binding_row(existing)
            result |= {"bound": True, "binding_id": row["id"], "binding": row}
            if dry_run:
                return result | {"dry_run": True}
            client.delete_item(BINDING_TYPE, row["id"])
        return result | {"deleted": True}

    def list_bindings(self, alias: str, workspace_id: int | None = None) -> dict[str, Any]:
        """Every binding of the project, with whether the workspace of its KID still exists."""
        project = self._project(alias)
        with self._metastore_factory(project.stack_url, project.token) as client:
            rows = [_binding_row(item) for item in client.list_items(BINDING_TYPE)]
        if workspace_id is not None:
            rows = [r for r in rows if r["workspace_id"] == workspace_id]
        with self._client_factory(project.stack_url, project.token) as storage:
            project_id = self._project_id(project, storage)
            for row in rows:
                row["workspace_exists"] = None
                if row["kind"] != KidKind.WORKSPACE or row["workspace_id"] is None:
                    continue
                if row["kid_project_id"] != project_id:
                    row["workspace_check_error"] = (
                        f"the KID names project {row['kid_project_id']}, not this one"
                    )
                    continue
                check = self._check_workspace(storage, row["workspace_id"])
                if check.state is WorkspaceState.UNKNOWN:
                    row["workspace_check_error"] = check.error.message if check.error else ""
                else:
                    row["workspace_exists"] = check.state is WorkspaceState.EXISTS
        return {"project": alias, "bindings": rows}

    def sweep(self, alias: str, *, older_than_days: int, delete: bool) -> dict[str, Any]:
        """Find (and with ``delete``, remove) the bindings whose workspace is gone.

        A binding is orphaned when its KID is a canonical workspace KID of this project, it was last
        updated more than ``older_than_days`` ago, and the workspace returns 404 in production and
        in every dev branch. Any other outcome of that check is a failed item, never a delete. App
        KIDs are listed and never swept.
        """
        if older_than_days < 0:
            _usage(f"--older-than must not be negative, got {older_than_days}.")
        project = self._project(alias)
        cutoff = self._now() - timedelta(days=older_than_days)
        with (
            self._metastore_factory(project.stack_url, project.token) as client,
            self._client_factory(project.stack_url, project.token) as storage,
        ):
            project_id = self._project_id(project, storage)
            rows = [_binding_row(item) for item in client.list_items(BINDING_TYPE)]
            for row in rows:
                row |= self._sweep_decision(storage, row, project_id, cutoff)
                if row["status"] != "orphaned" or not delete:
                    continue
                try:
                    client.delete_item(BINDING_TYPE, row["id"])
                    row["status"] = "deleted"
                except KeboolaApiError as exc:
                    if exc.status_code == 409:
                        row |= {"status": "kept", "reason": f"the metastore refused: {exc.message}"}
                    else:
                        row |= {"status": "failed", "reason": exc.message}
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in _SWEEP_STATUSES}
        return {
            "project": alias,
            "older_than_days": older_than_days,
            "dry_run": not delete,
            "references_checked": False,
            "references_note": REFERENCES_NOTE,
            "bindings": rows,
            "summary": counts,
        }

    def _sweep_decision(
        self, storage: Any, row: dict[str, Any], project_id: int, cutoff: datetime
    ) -> dict[str, str]:
        if row["kind"] == KidKind.APP:
            return {"status": "kept", "reason": "app KID: never swept"}
        if row["kind"] != KidKind.WORKSPACE:
            return {"status": "kept", "reason": "not a canonical workspace KID"}
        if row["kid_project_id"] != project_id:
            return {"status": "kept", "reason": f"the KID names project {row['kid_project_id']}"}
        updated = _parse_time(row["updated"])
        if updated is None:
            return {"status": "kept", "reason": "no last-update timestamp"}
        if updated > cutoff:
            return {"status": "kept", "reason": "updated within --older-than"}
        check = self._check_workspace(storage, row["workspace_id"])
        if check.state is WorkspaceState.EXISTS:
            return {"status": "kept", "reason": "the workspace exists"}
        if check.state is WorkspaceState.UNKNOWN:
            message = check.error.message if check.error else "unknown error"
            return {"status": "failed", "reason": f"workspace check failed: {message}"}
        return {"status": "orphaned", "reason": "the workspace no longer exists"}

    @staticmethod
    def _with_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        if warnings:
            result["warnings"] = warnings
        return result
