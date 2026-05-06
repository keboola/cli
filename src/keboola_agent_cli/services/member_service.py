"""Project membership and invitation lifecycle service.

Wraps the Manage API endpoints under ``/manage/projects/{id}/{users,invitations}``
behind a layer that:

- resolves a project alias to its numeric ID via ``ConfigStore``;
- looks up members + invitations by email (the public-facing key) so callers
  never need to deal with raw user/invitation IDs;
- treats the Manage API's "already invited / already a member" 400 response as
  a no-op rather than an error (mirrors the heuristic from the orchestrator
  scripts, but typed to status_code + message substring rather than guessed
  HTTP code);
- parallelises bulk invitation via :class:`ThreadPoolExecutor`, accumulating
  per-row results so one bad row never aborts the rest.

Endpoints + payload shapes were verified empirically on 2026-05-01 against
``connection.us-east4.gcp.keboola.com``; see the plan-of-record for the full
verification log.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..config_store import ConfigStore
from ..constants import DEFAULT_INVITE_WORKERS, PROJECT_ROLES
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..manage_client import ManageClient
from ..models import (
    BulkInviteResult,
    MemberInviteRow,
    ProjectInvitation,
    ProjectMember,
)

logger = logging.getLogger(__name__)

ManageClientFactory = Callable[[str, str], ManageClient]


def default_manage_client_factory(stack_url: str, manage_token: str) -> ManageClient:
    """Construct a :class:`ManageClient` bound to ``stack_url``."""
    return ManageClient(stack_url=stack_url, manage_token=manage_token)


# The Manage API returns HTTP 400 (not 422) with one of these substrings when
# a duplicate invitation/member is created. Treated as success-with-note in
# the service layer so bulk imports don't fail on idempotent re-runs.
_ALREADY_INVITED_MARKER = "already been invited"
_ALREADY_MEMBER_MARKER = "already a member"


class MemberService:
    """Business logic for project members and invitations."""

    def __init__(
        self,
        config_store: ConfigStore,
        manage_client_factory: ManageClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._manage_client_factory = manage_client_factory or default_manage_client_factory

    # ------------------------------------------------------------------
    # Public API: single-shot operations
    # ------------------------------------------------------------------

    def invite(
        self,
        *,
        manage_token: str,
        alias: str,
        email: str,
        role: str,
        reason: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Invite ``email`` to the project registered under ``alias``."""
        self._validate_role(role)
        stack_url, project_id = self._resolve_alias(alias)

        if dry_run:
            return {
                "status": "dry_run",
                "alias": alias,
                "project_id": project_id,
                "email": email,
                "role": role,
                "reason": reason or "",
            }

        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            return self._invite_one(manage_client, alias, project_id, email, role, reason)
        finally:
            manage_client.close()

    def invite_bulk(
        self,
        *,
        manage_token: str,
        csv_path: Path,
        default_role: str | None = None,
        workers: int = DEFAULT_INVITE_WORKERS,
        dry_run: bool = False,
    ) -> BulkInviteResult:
        """Invite every row of ``csv_path`` in parallel.

        CSV must have a header row. Recognised columns (case-insensitive):
        ``email`` (required), ``project`` or ``project_id`` (one required),
        ``role`` (optional if ``default_role`` is given), ``reason`` (optional).
        Extra columns are ignored. ``project`` values that are all-digits are
        resolved as numeric project IDs without an alias lookup.
        """
        if default_role is not None:
            self._validate_role(default_role)
        rows = self._parse_invite_csv(csv_path, default_role)
        if not rows:
            return BulkInviteResult(total=0, succeeded=0, noop=0, failed=0, dry_run=dry_run)

        if dry_run:
            return self._bulk_dry_run(rows)

        # Resolve every row's (stack_url, project_id) up front. A row that
        # fails resolution (unknown alias, unregistered project_id) becomes a
        # per-row "failed" entry; the rest of the batch still runs. Mirrors
        # the partial-success contract enforced by `OrgService.refresh_tokens`.
        resolved: list[tuple[dict[str, Any], str, int]] = []
        upfront_failures: list[MemberInviteRow] = []
        for row in rows:
            try:
                stack_url, project_id = self._stack_for_row(row)
                resolved.append((row, stack_url, project_id))
            except ConfigError as exc:
                upfront_failures.append(
                    MemberInviteRow(
                        email=row["email"],
                        project=str(row["project"]),
                        role=row["role"],
                        status="failed",
                        note=str(getattr(exc, "message", exc)),
                    )
                )

        if not resolved:
            return BulkInviteResult(
                total=len(upfront_failures),
                succeeded=0,
                noop=0,
                failed=len(upfront_failures),
                rows=upfront_failures,
                dry_run=False,
            )

        # All resolved rows must share a single stack URL; sending invitations
        # for project A on stack X via a client bound to stack Y is a security
        # bug, not a "partial-success" path.
        resolved_stacks = {t[1] for t in resolved}
        if len(resolved_stacks) != 1:
            raise ConfigError(
                f"CSV references multiple stack URLs ({sorted(resolved_stacks)}); "
                "split the file by stack and run --from-csv per stack."
            )
        stack_url = resolved_stacks.pop()

        manage_client = self._manage_client_factory(stack_url, manage_token)
        results: list[MemberInviteRow] = list(upfront_failures)
        try:
            worker_count = max(1, min(workers, len(resolved)))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [
                    pool.submit(self._invoke_resolved_row, manage_client, row, project_id)
                    for row, _, project_id in resolved
                ]
                for fut in as_completed(futures):
                    results.append(fut.result())
        finally:
            manage_client.close()

        return BulkInviteResult(
            total=len(results),
            succeeded=sum(1 for r in results if r.status == "ok"),
            noop=sum(1 for r in results if r.status == "noop"),
            failed=sum(1 for r in results if r.status == "failed"),
            rows=results,
            dry_run=False,
        )

    def list_members(
        self,
        *,
        manage_token: str,
        alias: str,
        include_pending: bool = False,
    ) -> dict[str, Any]:
        """Return active members (and, optionally, pending invitations)."""
        stack_url, project_id = self._resolve_alias(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            members_raw = manage_client.list_project_members(project_id)
            members = [ProjectMember.model_validate(m) for m in members_raw]
            payload: dict[str, Any] = {
                "alias": alias,
                "project_id": project_id,
                "members": [m.model_dump(by_alias=False) for m in members],
            }
            if include_pending:
                inv_raw = manage_client.list_project_invitations(project_id)
                payload["pending_invitations"] = [
                    ProjectInvitation.model_validate(i).model_dump(by_alias=False) for i in inv_raw
                ]
            return payload
        finally:
            manage_client.close()

    def list_invitations(
        self,
        *,
        manage_token: str,
        alias: str,
    ) -> dict[str, Any]:
        """Return pending invitations for ``alias``."""
        stack_url, project_id = self._resolve_alias(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            raw = manage_client.list_project_invitations(project_id)
            return {
                "alias": alias,
                "project_id": project_id,
                "invitations": [
                    ProjectInvitation.model_validate(i).model_dump(by_alias=False) for i in raw
                ],
            }
        finally:
            manage_client.close()

    def cancel_invitation(
        self,
        *,
        manage_token: str,
        alias: str,
        email: str,
        invitation_id: int | None = None,
    ) -> dict[str, Any]:
        """Cancel a pending invitation. Resolves by email if no ID is supplied."""
        stack_url, project_id = self._resolve_alias(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            if invitation_id is None:
                invitation_id = self._resolve_invitation_id(manage_client, project_id, email)
            manage_client.cancel_project_invitation(project_id, invitation_id)
            return {
                "status": "cancelled",
                "alias": alias,
                "project_id": project_id,
                "email": email,
                "invitation_id": invitation_id,
            }
        finally:
            manage_client.close()

    def remove_member(
        self,
        *,
        manage_token: str,
        alias: str,
        email: str,
    ) -> dict[str, Any]:
        """Remove an active member from a project."""
        stack_url, project_id = self._resolve_alias(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            user_id = self._resolve_member_id(manage_client, project_id, email)
            manage_client.remove_project_member(project_id, user_id)
            return {
                "status": "removed",
                "alias": alias,
                "project_id": project_id,
                "email": email,
                "user_id": user_id,
            }
        finally:
            manage_client.close()

    def set_member_role(
        self,
        *,
        manage_token: str,
        alias: str,
        email: str,
        role: str,
    ) -> dict[str, Any]:
        """Change an existing member's role via PATCH."""
        self._validate_role(role)
        stack_url, project_id = self._resolve_alias(alias)
        manage_client = self._manage_client_factory(stack_url, manage_token)
        try:
            user_id = self._resolve_member_id(manage_client, project_id, email)
            updated = manage_client.update_project_member_role(project_id, user_id, role)
            return {
                "status": "updated",
                "alias": alias,
                "project_id": project_id,
                "email": email,
                "user_id": user_id,
                "role": updated.get("role", role),
            }
        finally:
            manage_client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_role(role: str) -> None:
        """Defence-in-depth: command layer enforces the same whitelist via Choice."""
        if role not in PROJECT_ROLES:
            raise ValueError(
                f"Invalid role {role!r}. Allowed roles are: {', '.join(PROJECT_ROLES)}."
            )

    def _resolve_alias(self, alias: str) -> tuple[str, int]:
        """Look up ``alias`` in the config store and return ``(stack_url, project_id)``."""
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

    def _stack_for_row(self, row: dict[str, Any]) -> tuple[str, int]:
        """Resolve a CSV row's project field to ``(stack_url, project_id)``."""
        project_field = str(row["project"]).strip()
        if project_field.isdigit():
            # Numeric project_id rows still need a stack_url; we infer from the
            # currently-registered projects sharing that ID, falling back to
            # any default.
            project_id = int(project_field)
            for cfg in self._config_store.load().projects.values():
                if cfg.project_id == project_id:
                    return cfg.stack_url, project_id
            raise ConfigError(
                f"CSV row references project_id={project_id}, which is not registered "
                "in this kbagent config; add it via `kbagent project add` so we know "
                "which stack URL to use."
            )
        return self._resolve_alias(project_field)

    @staticmethod
    def _resolve_member_id(manage_client: ManageClient, project_id: int, email: str) -> int:
        """Find an active member's numeric ID by email (case-insensitive match)."""
        members = manage_client.list_project_members(project_id)
        normalised = email.casefold()
        for member in members:
            if str(member.get("email", "")).casefold() == normalised:
                return int(member["id"])
        raise KeboolaApiError(
            message=f"No active member with email {email!r} on project {project_id}.",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )

    @staticmethod
    def _resolve_invitation_id(manage_client: ManageClient, project_id: int, email: str) -> int:
        """Find a pending invitation by email."""
        invitations = manage_client.list_project_invitations(project_id)
        normalised = email.casefold()
        for inv in invitations:
            if str(inv.get("user", {}).get("email", "")).casefold() == normalised:
                return int(inv["id"])
        raise KeboolaApiError(
            message=f"No pending invitation for email {email!r} on project {project_id}.",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )

    def _invite_one(
        self,
        manage_client: ManageClient,
        project_label: str,
        project_id: int,
        email: str,
        role: str,
        reason: str | None,
    ) -> dict[str, Any]:
        """Single-row invitation logic shared by ``invite`` and ``invite_bulk``.

        ``project_label`` is the human-readable project identifier surfaced in
        the result dict's ``alias`` field. Single-shot mode passes the
        registered alias; bulk mode passes the raw CSV ``project`` cell, which
        may be either an alias or a numeric project ID string -- whatever the
        user wrote.
        """
        try:
            invitation = manage_client.create_project_invitation(
                project_id=project_id,
                email=email,
                role=role,
                reason=reason,
            )
            return {
                "status": "ok",
                "alias": project_label,
                "project_id": project_id,
                "email": email,
                "role": role,
                "invitation_id": invitation.get("id"),
            }
        except KeboolaApiError as exc:
            note = self._noop_note_for(exc)
            if note is None:
                raise
            return {
                "status": "noop",
                "alias": project_label,
                "project_id": project_id,
                "email": email,
                "role": role,
                "note": note,
            }

    def _invoke_resolved_row(
        self,
        manage_client: ManageClient,
        row: dict[str, Any],
        project_id: int,
    ) -> MemberInviteRow:
        """Execute one CSV row inside the bulk-invite executor.

        Called only on rows whose (stack_url, project_id) was already resolved
        by ``invite_bulk`` -- so the only failure path here is the API call
        itself (e.g. invalid email, network error, role rejection).
        """
        email = row["email"]
        role = row["role"]
        reason = row.get("reason")
        project_field = str(row["project"]).strip()
        try:
            outcome = self._invite_one(
                manage_client, project_field, project_id, email, role, reason
            )
            return MemberInviteRow(
                email=email,
                project=project_field,
                project_id=project_id,
                role=role,
                status=outcome["status"],
                note=outcome.get("note", ""),
                invitation_id=outcome.get("invitation_id"),
            )
        except KeboolaApiError as exc:
            return MemberInviteRow(
                email=email,
                project=project_field,
                project_id=project_id,
                role=role,
                status="failed",
                note=str(getattr(exc, "message", exc)),
            )

    @staticmethod
    def _noop_note_for(exc: KeboolaApiError) -> str | None:
        """Return a noop reason if ``exc`` is the "already invited / member" 400."""
        if exc.status_code != 400:
            return None
        message = exc.message or ""
        if _ALREADY_INVITED_MARKER in message:
            return "already_invited"
        if _ALREADY_MEMBER_MARKER in message:
            return "already_member"
        return None

    def _parse_invite_csv(self, csv_path: Path, default_role: str | None) -> list[dict[str, Any]]:
        """Parse + validate a bulk-invite CSV. Returns a list of normalised dicts."""
        if not csv_path.exists():
            raise ConfigError(f"CSV file not found: {csv_path}")

        # `utf-8-sig` strips a leading BOM if Excel produced the CSV (otherwise
        # the first header reads as `﻿email`, which fails the email-column
        # check with a misleading message).
        with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ConfigError(f"CSV file {csv_path} has no header row.")
            headers = {h.strip().lower(): h for h in reader.fieldnames if h}
            if "email" not in headers:
                raise ConfigError(
                    f"CSV file {csv_path} is missing an 'email' column. "
                    f"Found columns: {list(reader.fieldnames)}."
                )
            project_key = (
                "project"
                if "project" in headers
                else ("project_id" if "project_id" in headers else None)
            )
            if project_key is None:
                raise ConfigError(
                    f"CSV file {csv_path} must have a 'project' or 'project_id' column. "
                    f"Found columns: {list(reader.fieldnames)}."
                )
            has_role = "role" in headers
            if not has_role and default_role is None:
                raise ConfigError(
                    f"CSV file {csv_path} has no 'role' column and --default-role was not given."
                )

            rows: list[dict[str, Any]] = []
            for line_no, raw in enumerate(reader, start=2):  # header is line 1
                email = (raw.get(headers["email"]) or "").strip()
                project = (raw.get(headers[project_key]) or "").strip()
                role = (raw.get(headers["role"]) if has_role else None) or default_role or ""
                role = role.strip()
                reason = (raw.get(headers["reason"]) or "").strip() if "reason" in headers else ""
                if not email or not project:
                    raise ConfigError(
                        f"CSV {csv_path} line {line_no}: 'email' and '{project_key}' are both required."
                    )
                if not role:
                    raise ConfigError(
                        f"CSV {csv_path} line {line_no}: missing role and no --default-role."
                    )
                self._validate_role(role)
                rows.append(
                    {
                        "email": email,
                        "project": project,
                        "role": role,
                        "reason": reason or None,
                    }
                )
        return rows

    def _bulk_dry_run(self, rows: list[dict[str, Any]]) -> BulkInviteResult:
        """Render a dry-run result without hitting the network.

        Mirrors the live path: per-row resolution failures become per-row
        failed entries; multi-stack-URL CSVs raise (matches the real-run
        invariant so users don't get a "preview said ok, real run aborted"
        surprise).
        """
        previewed: list[MemberInviteRow] = []
        resolved_stacks: set[str] = set()
        for row in rows:
            try:
                stack_url, project_id = self._stack_for_row(row)
                resolved_stacks.add(stack_url)
            except ConfigError as exc:
                previewed.append(
                    MemberInviteRow(
                        email=row["email"],
                        project=str(row["project"]),
                        role=row["role"],
                        status="failed",
                        note=str(getattr(exc, "message", exc)),
                    )
                )
                continue
            previewed.append(
                MemberInviteRow(
                    email=row["email"],
                    project=str(row["project"]),
                    project_id=project_id,
                    role=row["role"],
                    status="ok",
                    note="dry_run",
                )
            )
        if len(resolved_stacks) > 1:
            raise ConfigError(
                f"CSV references multiple stack URLs ({sorted(resolved_stacks)}); "
                "split the file by stack and run --from-csv per stack."
            )
        return BulkInviteResult(
            total=len(previewed),
            succeeded=sum(1 for r in previewed if r.status == "ok"),
            noop=0,
            failed=sum(1 for r in previewed if r.status == "failed"),
            rows=previewed,
            dry_run=True,
        )
