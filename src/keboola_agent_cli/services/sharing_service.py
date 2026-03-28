"""Sharing service - business logic for bucket sharing and linking.

Enables cross-project data sharing via Storage API:
- Share buckets (requires master token with org membership)
- Link shared buckets into target projects
- List available shared buckets
- Unshare / unlink operations

Master token resolution: KBC_MASTER_TOKEN_{ALIAS} > KBC_MASTER_TOKEN > project token.
"""

import logging
import os
from typing import Any

from ..constants import ENV_KBC_MASTER_TOKEN
from ..errors import KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService

logger = logging.getLogger(__name__)

# Valid sharing types for the share command
VALID_SHARING_TYPES: list[str] = [
    "organization",
    "organization-project",
    "selected-projects",
    "selected-users",
]


class SharingService(BaseService):
    """Business logic for bucket sharing and linking across projects."""

    def resolve_master_token(self, alias: str, project: ProjectConfig) -> str:
        """Resolve the master token for a project.

        Resolution order:
        1. KBC_MASTER_TOKEN_{ALIAS_UPPER} (project-specific env var)
        2. KBC_MASTER_TOKEN (global env var)
        3. Falls back to the project's configured token (may lack permissions)

        Args:
            alias: Project alias (e.g. "padak").
            project: ProjectConfig with stack_url and token.

        Returns:
            Token string to use for sharing operations.
        """
        # Project-specific: KBC_MASTER_TOKEN_PADAK
        alias_key = f"{ENV_KBC_MASTER_TOKEN}_{alias.upper().replace('-', '_')}"
        token = os.environ.get(alias_key)
        if token:
            logger.debug("Using project-specific master token from %s", alias_key)
            return token

        # Global fallback: KBC_MASTER_TOKEN
        token = os.environ.get(ENV_KBC_MASTER_TOKEN)
        if token:
            logger.debug("Using global master token from %s", ENV_KBC_MASTER_TOKEN)
            return token

        # Last resort: project's configured token
        logger.debug("No master token found, using project token for '%s'", alias)
        return project.token

    def list_shared(
        self,
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """List shared buckets available for linking.

        Queries each project for shared buckets visible to it (via org membership).
        Uses the regular project token (no master token needed).

        Returns:
            Dict with 'shared_buckets' list and 'errors' list.
        """
        projects = self.resolve_projects(aliases)
        successes, errors = self._run_parallel(projects, self._fetch_shared_buckets)

        # Deduplicate: same bucket can appear from multiple projects
        seen: set[tuple[int, str]] = set()
        shared_buckets: list[dict[str, Any]] = []
        for result in successes:
            for bucket in result[1]:
                key = (bucket.get("source_project_id", 0), bucket.get("source_bucket_id", ""))
                if key not in seen:
                    seen.add(key)
                    shared_buckets.append(bucket)

        return {"shared_buckets": shared_buckets, "errors": errors}

    def share(
        self,
        alias: str,
        bucket_id: str,
        sharing_type: str,
        target_project_ids: list[int] | None = None,
        target_users: list[str] | None = None,
    ) -> dict[str, Any]:
        """Enable sharing on a bucket.

        Requires a master token with organization membership.
        Set KBC_MASTER_TOKEN_{ALIAS} or KBC_MASTER_TOKEN env var.

        Returns:
            Dict with operation result.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        token = self.resolve_master_token(alias, project)

        client = self._client_factory(project.stack_url, token)
        try:
            job = client.share_bucket(
                bucket_id=bucket_id,
                sharing_type=sharing_type,
                target_project_ids=target_project_ids,
                target_users=target_users,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "bucket_id": bucket_id,
            "sharing_type": sharing_type,
            "job_status": job.get("status", "unknown"),
            "message": f"Bucket '{bucket_id}' shared as '{sharing_type}' in project '{alias}'.",
        }

    def unshare(
        self,
        alias: str,
        bucket_id: str,
    ) -> dict[str, Any]:
        """Disable sharing on a bucket.

        Requires a master token. Fails if other projects still have linked buckets.

        Returns:
            Dict with operation result.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        token = self.resolve_master_token(alias, project)

        client = self._client_factory(project.stack_url, token)
        try:
            job = client.unshare_bucket(bucket_id=bucket_id)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "bucket_id": bucket_id,
            "job_status": job.get("status", "unknown"),
            "message": f"Sharing disabled for bucket '{bucket_id}' in project '{alias}'.",
        }

    def link(
        self,
        alias: str,
        source_project_id: int,
        source_bucket_id: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Link a shared bucket into the target project.

        Uses the regular project token (no master token needed, just canManageBuckets).

        Args:
            alias: Target project alias.
            source_project_id: ID of the project that owns the shared bucket.
            source_bucket_id: Bucket ID in the source project.
            name: Display name for the linked bucket. Defaults to source bucket name.

        Returns:
            Dict with linked bucket info.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        # Default name: derive from source bucket ID (e.g. "out.c-data" -> "shared-data")
        if not name:
            parts = source_bucket_id.split(".")
            bucket_name = parts[-1] if len(parts) > 1 else source_bucket_id
            # Strip "c-" prefix if present
            if bucket_name.startswith("c-"):
                bucket_name = bucket_name[2:]
            name = f"shared-{bucket_name}"

        client = self._client_factory(project.stack_url, project.token)
        try:
            job = client.link_bucket(
                source_project_id=source_project_id,
                source_bucket_id=source_bucket_id,
                name=name,
            )
        finally:
            client.close()

        results = job.get("results", {})
        linked_bucket_id = results.get("id", f"in.c-{name}")

        return {
            "project_alias": alias,
            "linked_bucket_id": linked_bucket_id,
            "source_project_id": source_project_id,
            "source_bucket_id": source_bucket_id,
            "name": name,
            "job_status": job.get("status", "unknown"),
            "message": (
                f"Linked bucket '{linked_bucket_id}' created in project '{alias}' "
                f"from '{source_bucket_id}' (project {source_project_id})."
            ),
        }

    def unlink(
        self,
        alias: str,
        bucket_id: str,
    ) -> dict[str, Any]:
        """Delete a linked bucket from the target project.

        Uses the regular project token.

        Returns:
            Dict with operation result.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Verify it's actually a linked bucket before deleting
            bucket = client.get_bucket_detail(bucket_id)
            if not bucket.get("sourceBucket"):
                raise KeboolaApiError(
                    message=f"Bucket '{bucket_id}' is not a linked bucket. "
                    "Use this command only for linked (shared) buckets.",
                    status_code=400,
                    error_code="NOT_LINKED_BUCKET",
                    retryable=False,
                )
            client.delete_bucket(bucket_id=bucket_id, force=True)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "bucket_id": bucket_id,
            "message": f"Linked bucket '{bucket_id}' deleted from project '{alias}'.",
        }

    # ------------------------------------------------------------------
    # Parallel workers
    # ------------------------------------------------------------------

    def _fetch_shared_buckets(
        self, alias: str, project: ProjectConfig
    ) -> tuple[str, list[dict[str, Any]], bool]:
        """Fetch shared buckets for a single project (worker for _run_parallel)."""
        client = self._client_factory(project.stack_url, project.token)
        try:
            raw_buckets = client.list_shared_buckets()
            buckets = [
                {
                    "source_project_id": b.get("project", {}).get("id"),
                    "source_project_name": b.get("project", {}).get("name", ""),
                    "source_bucket_id": b.get("id", ""),
                    "display_name": b.get("displayName", b.get("name", "")),
                    "description": b.get("description", ""),
                    "sharing": b.get("sharing", ""),
                    "backend": b.get("backend", ""),
                    "rows_count": b.get("rowsCount", 0),
                    "data_size_bytes": b.get("dataSizeBytes", 0),
                    "tables": [
                        {
                            "id": t.get("id", ""),
                            "name": t.get("name", ""),
                            "display_name": t.get("displayName", t.get("name", "")),
                        }
                        for t in b.get("tables", [])
                    ],
                    "shared_by": b.get("sharedBy"),
                }
                for b in raw_buckets
            ]
            return (alias, buckets, True)
        except KeboolaApiError as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        finally:
            client.close()
