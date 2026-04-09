"""Storage service - business logic for bucket and table operations.

Provides direct access to Storage API data including sharing/linked bucket
metadata that MCP tools strip from responses.
"""

import logging
from typing import Any

from ..models import ProjectConfig
from .base import BaseService

logger = logging.getLogger(__name__)


class StorageService(BaseService):
    """Business logic for storage bucket and table operations.

    Supports multi-project parallel queries for listing operations.
    """

    def list_buckets(
        self,
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """List storage buckets from one or more projects.

        Includes sharing/linked bucket metadata (sourceBucket, sourceProject)
        that is not available via MCP tools.

        Returns:
            Dict with 'buckets' list and 'errors' list.
        """
        projects = self.resolve_projects(aliases)
        successes, errors = self._run_parallel(projects, self._fetch_buckets)

        buckets: list[dict[str, Any]] = []
        for result in successes:
            alias = result[0]
            for bucket in result[1]:
                entry: dict[str, Any] = {
                    "project_alias": alias,
                    "id": bucket.get("id", ""),
                    "display_name": bucket.get("displayName", bucket.get("name", "")),
                    "stage": bucket.get("stage", ""),
                    "backend": bucket.get("backend", ""),
                    "rows_count": bucket.get("rowsCount", 0),
                    "data_size_bytes": bucket.get("dataSizeBytes", 0),
                    "description": bucket.get("description", ""),
                    "is_linked": False,
                    "source_project_id": None,
                    "source_project_name": "",
                    "source_bucket_id": "",
                }

                # Enrich with sharing info
                source = bucket.get("sourceBucket")
                if source:
                    entry["is_linked"] = True
                    src_project = source.get("project", {})
                    entry["source_project_id"] = src_project.get("id")
                    entry["source_project_name"] = src_project.get("name", "")
                    entry["source_bucket_id"] = source.get("id", "")

                buckets.append(entry)

        return {"buckets": buckets, "errors": errors}

    def get_bucket_detail(
        self,
        alias: str,
        bucket_id: str,
    ) -> dict[str, Any]:
        """Get detailed bucket info including tables and sharing metadata.

        For linked buckets, includes the Snowflake direct access path.

        Returns:
            Dict with bucket detail, tables, and resolved Snowflake paths.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            token_info = client.verify_token()
            bucket = client.get_bucket_detail(bucket_id)
        finally:
            client.close()

        project_id = token_info.project_id
        source = bucket.get("sourceBucket")

        result: dict[str, Any] = {
            "project_alias": alias,
            "project_id": project_id,
            "bucket_id": bucket.get("id", ""),
            "display_name": bucket.get("displayName", ""),
            "stage": bucket.get("stage", ""),
            "description": bucket.get("description", ""),
            "backend": bucket.get("backend", ""),
            "is_linked": source is not None,
        }

        # Resolve Snowflake paths using backendPath from API (preserves correct case).
        # backendPath is an array like ["SAPI_4254", "out.c-account-movements"].
        # We must NOT construct the DB name ourselves (f"sapi_{id}") because
        # Snowflake databases may be uppercase or lowercase depending on how
        # they were created, and double-quoted identifiers are case-sensitive.
        backend_path = bucket.get("backendPath", [])

        if source:
            src_project = source.get("project", {})
            src_project_id = src_project.get("id")
            src_bucket_id = source.get("id", "")
            result["source_project_id"] = src_project_id
            result["source_project_name"] = src_project.get("name", "")
            result["source_bucket_id"] = src_bucket_id
        else:
            result["source_project_id"] = None
            result["source_project_name"] = ""
            result["source_bucket_id"] = ""

        if len(backend_path) >= 2:
            result["snowflake_database"] = backend_path[0]
            result["snowflake_schema"] = backend_path[1]
        elif source:
            result["snowflake_database"] = f"sapi_{src_project_id}"
            result["snowflake_schema"] = src_bucket_id
        else:
            result["snowflake_database"] = f"sapi_{project_id}"
            result["snowflake_schema"] = bucket.get("id", "")

        # Build table list with Snowflake paths
        tables: list[dict[str, Any]] = []
        for table in bucket.get("tables", []):
            table_name = table.get("name", "")
            sf_db = result["snowflake_database"]
            sf_schema = result["snowflake_schema"]
            tables.append(
                {
                    "id": table.get("id", ""),
                    "name": table_name,
                    "display_name": table.get("displayName", table_name),
                    "is_alias": table.get("isAlias", False),
                    "snowflake_path": f'"{sf_db}"."{sf_schema}"."{table_name}"',
                }
            )

        result["tables"] = tables
        result["table_count"] = len(tables)

        return result

    def list_tables(
        self,
        alias: str,
        bucket_id: str | None = None,
    ) -> dict[str, Any]:
        """List tables from a project, optionally filtered by bucket.

        Returns:
            Dict with 'tables' list.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            raw_tables = client.list_tables(bucket_id=bucket_id)
        finally:
            client.close()

        tables = [
            {
                "project_alias": alias,
                "id": t.get("id", ""),
                "name": t.get("name", ""),
                "display_name": t.get("displayName", t.get("name", "")),
                "bucket_id": t.get("bucket", {}).get("id", "")
                if isinstance(t.get("bucket"), dict)
                else "",
                "rows_count": t.get("rowsCount", 0),
                "data_size_bytes": t.get("dataSizeBytes", 0),
                "is_alias": t.get("isAlias", False),
                "last_import_date": t.get("lastImportDate", ""),
            }
            for t in raw_tables
        ]

        return {"tables": tables, "project_alias": alias}

    # ------------------------------------------------------------------
    # Delete operations
    # ------------------------------------------------------------------

    def delete_tables(
        self,
        alias: str,
        table_ids: list[str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one or more storage tables.

        Batch-tolerant: accumulates errors per table, one failure does not
        stop other deletes.

        Returns:
            Dict with 'deleted', 'failed', 'dry_run', 'project_alias',
            and optionally 'would_delete'.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        if dry_run:
            return {
                "deleted": [],
                "failed": [],
                "would_delete": list(table_ids),
                "dry_run": True,
                "project_alias": alias,
            }

        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for tid in table_ids:
                try:
                    client.delete_table(tid)
                    deleted.append(tid)
                except KeboolaApiError as exc:
                    failed.append({"id": tid, "error": exc.message})
        finally:
            client.close()

        return {
            "deleted": deleted,
            "failed": failed,
            "dry_run": False,
            "project_alias": alias,
        }

    def delete_buckets(
        self,
        alias: str,
        bucket_ids: list[str],
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one or more storage buckets.

        Protections:
        - Linked buckets (sourceBucket set) are blocked with a helpful message.
        - Shared buckets (sharing field set) are blocked unless --force is used.
        - Without force, non-empty buckets fail at the API level.

        Batch-tolerant: accumulates errors per bucket.

        Returns:
            Dict with 'deleted', 'failed', 'dry_run', 'project_alias',
            and optionally 'would_delete'.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        would_delete: list[str] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for bid in bucket_ids:
                # Check bucket metadata for linked/shared protections
                try:
                    bucket = client.get_bucket_detail(bid)
                except KeboolaApiError as exc:
                    failed.append({"id": bid, "error": exc.message})
                    continue

                # Linked bucket protection
                if bucket.get("sourceBucket"):
                    failed.append(
                        {
                            "id": bid,
                            "error": (
                                f"Bucket '{bid}' is a linked bucket. "
                                "Use 'kbagent sharing unlink' to remove it."
                            ),
                        }
                    )
                    continue

                # Shared bucket protection (unless force)
                if bucket.get("sharing") and not force:
                    failed.append(
                        {
                            "id": bid,
                            "error": (
                                f"Bucket '{bid}' is shared to other projects. "
                                "Use --force to delete anyway, or 'kbagent sharing unshare' first."
                            ),
                        }
                    )
                    continue

                if dry_run:
                    would_delete.append(bid)
                    continue

                try:
                    client.delete_bucket(bid, force=force)
                    deleted.append(bid)
                except KeboolaApiError as exc:
                    failed.append({"id": bid, "error": exc.message})
        finally:
            client.close()

        result: dict[str, Any] = {
            "deleted": deleted,
            "failed": failed,
            "dry_run": dry_run,
            "project_alias": alias,
        }
        if dry_run:
            result["would_delete"] = would_delete
        return result

    # ------------------------------------------------------------------
    # Parallel workers
    # ------------------------------------------------------------------

    def _fetch_buckets(
        self, alias: str, project: ProjectConfig
    ) -> tuple[str, list[dict[str, Any]], bool]:
        """Fetch buckets for a single project (worker for _run_parallel)."""
        from ..errors import KeboolaApiError

        client = self._client_factory(project.stack_url, project.token)
        try:
            buckets = client.list_buckets(include="linkedBuckets")
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
