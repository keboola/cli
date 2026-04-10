"""Storage service - business logic for bucket and table operations.

Provides direct access to Storage API data including sharing/linked bucket
metadata that MCP tools strip from responses.
"""

import csv
import logging
from pathlib import Path
from typing import Any

from ..constants import VALID_COLUMN_TYPES
from ..models import ProjectConfig
from .base import BaseService

logger = logging.getLogger(__name__)


def _read_csv_header(file_path: str, delimiter: str = ",") -> list[str]:
    """Return column names from the first row of a CSV file.

    Strips leading/trailing whitespace and skips empty fields. Handles
    UTF-8 BOM automatically (utf-8-sig encoding).

    Raises:
        ValueError: If the first row is empty or contains no non-empty fields.
    """
    with open(file_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        header = next(reader, [])
    columns = [col.strip() for col in header if col.strip()]
    if not columns:
        raise ValueError("CSV file has no column headers in the first row.")
    return columns


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
    # Write operations
    # ------------------------------------------------------------------

    def create_bucket(
        self,
        alias: str,
        stage: str,
        name: str,
        description: str | None = None,
        backend: str | None = None,
    ) -> dict[str, Any]:
        """Create a new storage bucket.

        Args:
            stage: Bucket stage — must be "in" or "out".

        Returns:
            Dict with created bucket details.

        Raises:
            ValueError: If stage is not "in" or "out".
        """
        stage = stage.lower()
        if stage not in ("in", "out"):
            raise ValueError(f"Invalid stage '{stage}'. Must be 'in' or 'out'.")

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            bucket = client.create_bucket(
                stage=stage, name=name, description=description, backend=backend
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "id": bucket.get("id", ""),
            "display_name": bucket.get("displayName", bucket.get("name", "")),
            "stage": bucket.get("stage", ""),
            "backend": bucket.get("backend", ""),
            "description": bucket.get("description", ""),
        }

    def create_table(
        self,
        alias: str,
        bucket_id: str,
        name: str,
        columns: list[str],
        primary_key: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new table with typed columns.

        Args:
            columns: List of "name:TYPE" strings (e.g. ["id:INTEGER", "name:STRING"]).

        Returns:
            Dict with created table details.
        """
        parsed_columns = []
        for col_spec in columns:
            if ":" in col_spec:
                col_name, col_type = col_spec.split(":", 1)
                col_type = col_type.upper()
            else:
                col_name, col_type = col_spec, "STRING"
            if col_type not in VALID_COLUMN_TYPES:
                raise ValueError(
                    f"Invalid column type '{col_type}' for column '{col_name}'. "
                    f"Valid types: {', '.join(sorted(VALID_COLUMN_TYPES))}"
                )
            parsed_columns.append({"name": col_name, "definition": {"type": col_type}})

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            results = client.create_table(
                bucket_id=bucket_id,
                name=name,
                columns=parsed_columns,
                primary_key=primary_key,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "table_id": results.get("id", f"{bucket_id}.{name}"),
            "name": name,
            "bucket_id": bucket_id,
            "primary_key": primary_key or [],
            "columns": [c["name"] for c in parsed_columns],
        }

    def upload_table(
        self,
        alias: str,
        table_id: str,
        file_path: str,
        incremental: bool = False,
        delimiter: str = ",",
        enclosure: str = '"',
        auto_create: bool = True,
    ) -> dict[str, Any]:
        """Upload a CSV file into a storage table.

        When auto_create is True (default), auto-creates the bucket and/or
        table if they don't exist. Columns are inferred as STRING from the CSV
        header row. Pass auto_create=False to require the table to exist.

        Returns:
            Dict with import results plus auto_created_bucket / auto_created_table flags.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        file_size_bytes = Path(file_path).stat().st_size

        auto_created_bucket = False
        auto_created_table = False

        client = self._client_factory(project.stack_url, project.token)
        try:
            if auto_create:
                parts = table_id.split(".")
                if len(parts) == 3:
                    stage, bucket_slug, table_name = parts
                    bucket_id = f"{stage}.{bucket_slug}"
                    bucket_name = bucket_slug[2:] if bucket_slug.startswith("c-") else bucket_slug

                    # Ensure bucket exists
                    try:
                        client.get_bucket_detail(bucket_id)
                    except KeboolaApiError as exc:
                        if exc.status_code == 404:
                            client.create_bucket(stage=stage, name=bucket_name)
                            auto_created_bucket = True
                            logger.info("Auto-created bucket %s", bucket_id)
                        else:
                            raise

                    # Ensure table exists
                    existing = client.list_tables(bucket_id=bucket_id)
                    if not any(t.get("name") == table_name for t in existing):
                        columns = _read_csv_header(file_path, delimiter=delimiter)
                        client.create_table(
                            bucket_id=bucket_id,
                            name=table_name,
                            columns=[
                                {"name": col, "definition": {"type": "STRING"}} for col in columns
                            ],
                            primary_key=None,
                        )
                        auto_created_table = True
                        logger.info("Auto-created table %s (%d columns)", table_id, len(columns))

            results = client.upload_table(
                table_id=table_id,
                file_path=file_path,
                incremental=incremental,
                delimiter=delimiter,
                enclosure=enclosure,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "table_id": table_id,
            "incremental": incremental,
            "file_size_bytes": file_size_bytes,
            "imported_rows": results.get("importedRowsCount"),
            "warnings": results.get("warnings", []),
            "auto_created_bucket": auto_created_bucket,
            "auto_created_table": auto_created_table,
        }

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
