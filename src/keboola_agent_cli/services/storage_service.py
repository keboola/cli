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


def _write_columns_sidecar(output_dir: str, columns: list[str]) -> None:
    """Write a _columns.csv sidecar listing the table's column order.

    Storage exports slices without a header row; the column list comes from
    the table metadata. Writing a tiny sidecar here lets downstream tools
    (DuckDB read_csv, polars scan_csv) reconstruct the schema without
    querying Storage. Using the ``_`` prefix matches the _manifest.json
    convention that pyarrow/Spark/Hive use for "skip when reading as a
    dataset".
    """
    sidecar = Path(output_dir) / "_columns.csv"
    with sidecar.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(columns)


def _prepend_csv_header(file_path: str, columns: list[str]) -> None:
    """Prepend a CSV header row to an existing file.

    Streams the original body into a temp file so that multi-GB CSV exports
    never sit in RAM at once (issue #187: the old read_bytes() peaked at the
    full file size). Uses CSV quoting to match Keboola's RFC4180 format.
    """
    import io
    import shutil
    import tempfile

    writer_buf = io.StringIO()
    writer = csv.writer(writer_buf, quoting=csv.QUOTE_ALL)
    writer.writerow(columns)
    header_line = writer_buf.getvalue().encode("utf-8")

    p = Path(file_path)
    # Temp file sits next to the target so shutil.move is a cheap rename on
    # the same filesystem.
    with tempfile.NamedTemporaryFile(
        dir=p.parent, prefix=p.name + ".", suffix=".tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(header_line)
        with p.open("rb") as src:
            shutil.copyfileobj(src, tmp, length=1024 * 1024)
    tmp_path.replace(p)


class StorageService(BaseService):
    """Business logic for storage bucket and table operations.

    Supports multi-project parallel queries for listing operations.
    """

    def list_buckets(
        self,
        aliases: list[str] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List storage buckets from one or more projects.

        Includes sharing/linked bucket metadata (sourceBucket, sourceProject)
        that is not available via MCP tools.

        Args:
            aliases: Project aliases to query. If None, queries all.
            branch_id: If set, list buckets from a specific dev branch.

        Returns:
            Dict with 'buckets' list and 'errors' list.
        """
        projects = self.resolve_projects(aliases)

        def _worker(alias: str, project: ProjectConfig) -> tuple[str, list[dict[str, Any]], bool]:
            return self._fetch_buckets(alias, project, branch_id=branch_id)

        successes, errors = self._run_parallel(projects, _worker)

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
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed bucket info including tables and sharing metadata.

        For linked buckets, includes the Snowflake direct access path.

        Args:
            alias: Project alias.
            bucket_id: Bucket ID (e.g. 'in.c-db').
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with bucket detail, tables, and resolved Snowflake paths.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            token_info = client.verify_token()
            bucket = client.get_bucket_detail(bucket_id, branch_id=branch_id)
        finally:
            client.close()

        project_id = token_info.project_id
        source = bucket.get("sourceBucket")

        # KBC.description in the metadata array takes precedence over the
        # native description field (which can only be set at creation time
        # via the Storage API; user updates go through the metadata endpoint).
        raw_metadata: list[dict[str, Any]] = bucket.get("metadata", [])
        metadata_description = ""
        for m in raw_metadata:
            if m.get("key") == "KBC.description" and m.get("provider") == "user":
                metadata_description = m.get("value", "") or ""
                break
        description = metadata_description or bucket.get("description", "")

        result: dict[str, Any] = {
            "project_alias": alias,
            "project_id": project_id,
            "bucket_id": bucket.get("id", ""),
            "display_name": bucket.get("displayName", ""),
            "stage": bucket.get("stage", ""),
            "description": description,
            "backend": bucket.get("backend", ""),
            "is_linked": source is not None,
            "metadata": raw_metadata,
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

    def get_table_detail(
        self,
        alias: str,
        table_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed info about a storage table including columns.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. "in.c-bucket.table").
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with table metadata, columns, and size info.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            table = client.get_table_detail(table_id, branch_id=branch_id)
        finally:
            client.close()

        columns = table.get("columns", [])
        column_metadata = table.get("columnMetadata", {})
        raw_metadata: list[dict[str, Any]] = table.get("metadata", [])

        # Extract description and per-column descriptions from metadata list
        description = ""
        col_descriptions: dict[str, str] = {}
        for m in raw_metadata:
            key = m.get("key", "")
            if key == "KBC.description" and m.get("provider") == "user":
                description = m.get("value", "") or ""
            elif key.startswith("KBC.column.") and key.endswith(".description"):
                col_name = key[len("KBC.column.") : -len(".description")]
                col_descriptions[col_name] = m.get("value", "") or ""

        column_details = []
        for col in columns:
            col_info: dict[str, Any] = {"name": col}
            meta = column_metadata.get(col, [])
            for m in meta:
                if m.get("key") == "KBC.datatype.basetype":
                    col_info["type"] = m.get("value", "")
                elif m.get("key") == "KBC.datatype.nullable":
                    col_info["nullable"] = m.get("value", "") == "1"
            if col in col_descriptions:
                col_info["description"] = col_descriptions[col]
            column_details.append(col_info)

        return {
            "project_alias": alias,
            "table_id": table.get("id", table_id),
            "name": table.get("name", ""),
            "display_name": table.get("displayName", ""),
            "bucket_id": table.get("bucket", {}).get("id", ""),
            "description": description,
            "columns": columns,
            "column_details": column_details,
            "primary_key": table.get("primaryKey", []),
            "rows_count": table.get("rowsCount", 0),
            "data_size_bytes": table.get("dataSizeBytes", 0),
            "is_alias": table.get("isAlias", False),
            "last_import_date": table.get("lastImportDate", ""),
            "last_change_date": table.get("lastChangeDate", ""),
            "created": table.get("created", ""),
            "metadata": raw_metadata,
        }

    def list_tables(
        self,
        alias: str,
        bucket_id: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List tables from a project, optionally filtered by bucket.

        Args:
            alias: Project alias.
            bucket_id: Optional bucket ID filter.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with 'tables' list.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            raw_tables = client.list_tables(bucket_id=bucket_id, branch_id=branch_id)
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
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new storage bucket.

        Args:
            alias: Project alias.
            stage: Bucket stage — must be "in" or "out".
            description: Optional bucket description.
            backend: Optional backend type.
            branch_id: If set, create bucket in a specific dev branch.

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
                stage=stage,
                name=name,
                description=description,
                backend=backend,
                branch_id=branch_id,
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
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new table with typed columns.

        Args:
            alias: Project alias.
            bucket_id: Target bucket ID.
            name: Table name.
            columns: List of "name:TYPE" strings (e.g. ["id:INTEGER", "name:STRING"]).
            primary_key: Optional list of primary key column names.
            branch_id: If set, create table in a specific dev branch.

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
                branch_id=branch_id,
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
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a CSV file into a storage table.

        When auto_create is True (default), auto-creates the bucket and/or
        table if they don't exist. Columns are inferred as STRING from the CSV
        header row. Pass auto_create=False to require the table to exist.

        Args:
            alias: Project alias.
            table_id: Target table ID.
            file_path: Local path to the CSV file.
            incremental: Append rows (True) or full load (False).
            delimiter: CSV column delimiter.
            enclosure: CSV value enclosure character.
            auto_create: Auto-create bucket and table if missing.
            branch_id: If set, target a specific dev branch.

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
                        client.get_bucket_detail(bucket_id, branch_id=branch_id)
                    except KeboolaApiError as exc:
                        if exc.status_code == 404:
                            client.create_bucket(
                                stage=stage,
                                name=bucket_name,
                                branch_id=branch_id,
                            )
                            auto_created_bucket = True
                            logger.info("Auto-created bucket %s", bucket_id)
                        else:
                            raise

                    # Ensure table exists
                    existing = client.list_tables(
                        bucket_id=bucket_id,
                        branch_id=branch_id,
                    )
                    if not any(t.get("name") == table_name for t in existing):
                        columns = _read_csv_header(file_path, delimiter=delimiter)
                        client.create_table(
                            bucket_id=bucket_id,
                            name=table_name,
                            columns=[
                                {"name": col, "definition": {"type": "STRING"}} for col in columns
                            ],
                            primary_key=None,
                            branch_id=branch_id,
                        )
                        auto_created_table = True
                        logger.info("Auto-created table %s (%d columns)", table_id, len(columns))

            results = client.upload_table(
                table_id=table_id,
                file_path=file_path,
                incremental=incremental,
                delimiter=delimiter,
                enclosure=enclosure,
                branch_id=branch_id,
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
    # Download / export operations
    # ------------------------------------------------------------------

    def download_table(
        self,
        alias: str,
        table_id: str,
        output_path: str | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
        branch_id: int | None = None,
        keep_slices: bool = False,
    ) -> dict[str, Any]:
        """Export a storage table to a local CSV file.

        Uses the async export flow: export-async -> poll job -> get file
        info -> download from cloud URL. Handles gzip decompression
        transparently.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. "in.c-bucket.table").
            output_path: Local file path (default mode) or directory
                (``keep_slices=True``) to write to. Defaults to
                ``<table>.csv`` / ``<alias>/<table_id>.csv/`` respectively.
            columns: Optional list of column names to export.
            limit: Optional max number of rows to export.
            branch_id: If set, target a specific dev branch.
            keep_slices: If True, write each slice as its own file inside
                the output directory and preserve the manifest. The CSV
                header is written to a sidecar ``_columns.csv`` rather than
                prepended to any slice so no slice has to be rewritten
                end-to-end (which would defeat the memory story). For most
                analytical tools the slices themselves are header-less
                parts of a single logical CSV; headers come from catalog
                metadata, not the slice bodies.

        Returns:
            Dict with export metadata. With ``keep_slices`` the result also
            contains ``slice_count`` and ``slices`` (list of {path, size}).
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        table_name = table_id.rsplit(".", 1)[-1] if "." in table_id else table_id
        if not output_path:
            output_path = f"{alias}/{table_id}.csv" if keep_slices else f"{table_name}.csv"

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Step 0: Get table columns for CSV header
            table_detail = client.list_tables(include="columns", branch_id=branch_id)
            table_columns = columns  # Use explicit columns if specified
            if not table_columns:
                for t in table_detail:
                    if t.get("id") == table_id:
                        table_columns = t.get("columns", [])
                        break

            # Step 1: Start async export and wait for completion
            job = client.export_table_async(
                table_id=table_id,
                columns=columns,
                limit=limit,
                branch_id=branch_id,
            )

            # Step 2: Get file info from job results
            file_info = job.get("results", {}).get("file", {})
            file_id = file_info.get("id")
            if not file_id:
                raise KeboolaApiError(
                    message="Export job completed but no file ID in results",
                    status_code=500,
                    error_code="EXPORT_NO_FILE",
                    retryable=False,
                )

            # Step 3: Get download URL (branch-scoped if exporting from dev branch)
            file_detail = client.get_file_info(file_id, branch_id=branch_id)
            download_url = file_detail.get("url")
            if not download_url:
                raise KeboolaApiError(
                    message=f"No download URL for file {file_id}",
                    status_code=500,
                    error_code="EXPORT_NO_URL",
                    retryable=False,
                )

            # Step 4a: Sliced-directory mode -- each slice is its own file.
            # This is the OOM-safe-by-default path for analytical workflows
            # (DuckDB, polars, Spark read a directory natively).
            if keep_slices:
                if not file_detail.get("isSliced"):
                    raise KeboolaApiError(
                        message=(
                            "--keep-slices is only meaningful for tables that "
                            "Storage exports as multiple slices; this export "
                            "produced a single file. Re-run without --keep-slices."
                        ),
                        status_code=400,
                        error_code="NOT_SLICED",
                        retryable=False,
                    )
                slice_info = client.download_sliced_file_to_dir(file_detail, output_path)
                # Sidecar with the column order so downstream readers can
                # reconstruct the header without hitting Storage.
                _write_columns_sidecar(slice_info["output_dir"], table_columns or [])
                return {
                    "project_alias": alias,
                    "table_id": table_id,
                    "output_path": slice_info["output_dir"],
                    "file_size_bytes": slice_info["total_bytes"],
                    "columns": table_columns or [],
                    "limit": limit,
                    "keep_slices": True,
                    "slice_count": slice_info["slice_count"],
                    "slices": slice_info["slices"],
                }

            # Step 4b: Default mode -- concat into a single CSV file.
            if file_detail.get("isSliced"):
                bytes_written = client.download_sliced_file(file_detail, output_path)
            else:
                bytes_written = client.download_file(download_url, output_path)

            # Step 5: Prepend CSV header row
            if table_columns:
                _prepend_csv_header(output_path, table_columns)
                # Recalculate size after adding header
                bytes_written = Path(output_path).stat().st_size
        finally:
            client.close()

        return {
            "project_alias": alias,
            "table_id": table_id,
            "output_path": str(Path(output_path).resolve()),
            "file_size_bytes": bytes_written,
            "columns": table_columns or [],
            "limit": limit,
            "keep_slices": False,
        }

    # ------------------------------------------------------------------
    # Delete operations
    # ------------------------------------------------------------------

    def delete_tables(
        self,
        alias: str,
        table_ids: list[str],
        dry_run: bool = False,
        force: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete one or more storage tables.

        Batch-tolerant: accumulates errors per table, one failure does not
        stop other deletes.

        Args:
            alias: Project alias.
            table_ids: List of table IDs to delete.
            dry_run: If True, only report what would be deleted.
            force: If True, cascade-delete tables and all their aliases.
            branch_id: If set, target a specific dev branch.

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
                    client.delete_table(tid, branch_id=branch_id, force=force)
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

    def delete_columns(
        self,
        alias: str,
        table_id: str,
        columns: list[str],
        dry_run: bool = False,
        force: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete one or more columns from a storage table.

        Batch-tolerant: accumulates errors per column, one failure does not
        stop other deletes. Each delete is async and waits for completion.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. "in.c-bucket.table").
            columns: List of column names to delete.
            dry_run: If True, only report what would be deleted.
            force: If True, also delete from aliased tables.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with 'deleted', 'failed', 'dry_run', 'project_alias',
            'table_id', and optionally 'would_delete'.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        if dry_run:
            return {
                "deleted": [],
                "failed": [],
                "would_delete": list(columns),
                "dry_run": True,
                "project_alias": alias,
                "table_id": table_id,
            }

        deleted: list[str] = []
        failed: list[dict[str, str]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for col in columns:
                try:
                    client.delete_column(table_id, col, branch_id=branch_id, force=force)
                    deleted.append(col)
                except KeboolaApiError as exc:
                    failed.append({"column": col, "error": exc.message})
        finally:
            client.close()

        return {
            "deleted": deleted,
            "failed": failed,
            "dry_run": False,
            "project_alias": alias,
            "table_id": table_id,
        }

    def delete_buckets(
        self,
        alias: str,
        bucket_ids: list[str],
        force: bool = False,
        dry_run: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Delete one or more storage buckets.

        Protections:
        - Linked buckets (sourceBucket set) are blocked with a helpful message.
        - Shared buckets (sharing field set) are blocked unless --force is used.
        - Without force, non-empty buckets fail at the API level.

        Batch-tolerant: accumulates errors per bucket.

        Args:
            alias: Project alias.
            bucket_ids: List of bucket IDs to delete.
            force: Force delete even if bucket has tables or is shared.
            dry_run: If True, only report what would be deleted.
            branch_id: If set, target a specific dev branch.

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
                    bucket = client.get_bucket_detail(bid, branch_id=branch_id)
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
                    client.delete_bucket(bid, force=force, branch_id=branch_id)
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
    # File operations
    # ------------------------------------------------------------------

    def list_files(
        self,
        alias: str,
        limit: int = 20,
        offset: int = 0,
        tags: list[str] | None = None,
        since_id: int | None = None,
        query: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List Storage Files from a project.

        Args:
            alias: Project alias.
            limit: Max number of files.
            offset: Pagination offset.
            tags: Filter by tags (AND logic).
            since_id: Return only files newer than this ID.
            query: Full-text search on file name.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias and list of files.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            files = client.list_files(
                limit=limit,
                offset=offset,
                tags=tags,
                since_id=since_id,
                query=query,
                branch_id=branch_id,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "files": files,
            "count": len(files),
        }

    def upload_file(
        self,
        alias: str,
        file_path: str,
        name: str | None = None,
        tags: list[str] | None = None,
        is_permanent: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to Storage Files.

        Args:
            alias: Project alias.
            file_path: Local path to the file.
            name: Custom filename (defaults to local basename).
            tags: Optional list of tags.
            is_permanent: If True, file is not auto-deleted.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with file metadata.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        file_size_bytes = Path(file_path).stat().st_size

        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.upload_file(
                file_path=file_path,
                name=name,
                tags=tags,
                is_permanent=is_permanent,
                branch_id=branch_id,
            )
        finally:
            client.close()

        result["project_alias"] = alias
        result["file_size_bytes"] = file_size_bytes
        return result

    def get_file_info(
        self,
        alias: str,
        file_id: int,
    ) -> dict[str, Any]:
        """Get Storage File metadata.

        Args:
            alias: Project alias.
            file_id: Storage file ID.

        Returns:
            File resource dict.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            file_info = client.get_file_info(file_id)
        finally:
            client.close()

        file_info["project_alias"] = alias
        return file_info

    def download_file(
        self,
        alias: str,
        file_id: int | None = None,
        tags: list[str] | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Download a Storage File to local disk.

        Supports download by file ID or by tags (downloads latest matching file).
        Handles both sliced and non-sliced files.

        Args:
            alias: Project alias.
            file_id: Storage file ID (mutually exclusive with tags).
            tags: Download latest file matching these tags.
            output_path: Local output path (defaults to file's name).

        Returns:
            Dict with download metadata.
        """
        from ..errors import KeboolaApiError

        if not file_id and not tags:
            raise ValueError("Either --file-id or --tag must be provided")

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Resolve file ID from tags if needed
            if not file_id:
                files = client.list_files(limit=1, tags=tags)
                if not files:
                    tag_str = ", ".join(tags or [])
                    raise KeboolaApiError(
                        message=f"No files found matching tags: {tag_str}",
                        status_code=404,
                        error_code="FILE_NOT_FOUND",
                        retryable=False,
                    )
                file_id = files[0]["id"]

            file_detail = client.get_file_info(file_id)
            file_name = file_detail.get("name", f"file_{file_id}")

            # Auto-detect parquet: sliced + filename ends in .parquet. Such
            # files must be saved as individual slices, not concatenated --
            # each parquet slice has its own footer and concatenation produces
            # an invalid file.
            is_sliced = bool(file_detail.get("isSliced"))
            is_parquet = is_sliced and file_name.endswith(".parquet")

            if is_parquet:
                effective_output = output_path or f"{file_name}.d"
                slice_info = client.download_sliced_file_to_dir(file_detail, effective_output)
                result: dict[str, Any] = {
                    "project_alias": alias,
                    "file_id": file_id,
                    "file_name": file_name,
                    "output_path": slice_info["output_dir"],
                    "file_size_bytes": slice_info["total_bytes"],
                    "is_sliced": True,
                    "slice_count": slice_info["slice_count"],
                    "slices": slice_info["slices"],
                }
                return result

            effective_output = output_path or file_name
            if is_sliced:
                bytes_written = client.download_sliced_file(file_detail, effective_output)
            else:
                download_url = file_detail.get("url")
                if not download_url:
                    raise KeboolaApiError(
                        message=f"No download URL for file {file_id}",
                        status_code=500,
                        error_code="FILE_NO_URL",
                        retryable=False,
                    )
                bytes_written = client.download_file(download_url, effective_output)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "file_id": file_id,
            "file_name": file_name,
            "output_path": str(Path(effective_output).resolve()),
            "file_size_bytes": bytes_written,
            "is_sliced": is_sliced,
        }

    def delete_files(
        self,
        alias: str,
        file_ids: list[int],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one or more Storage Files.

        Batch-tolerant: accumulates errors per file.

        Args:
            alias: Project alias.
            file_ids: List of file IDs to delete.
            dry_run: If True, only report what would be deleted.

        Returns:
            Dict with deleted, failed, dry_run lists.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        deleted: list[int] = []
        failed: list[dict[str, Any]] = []
        would_delete: list[int] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for fid in file_ids:
                if dry_run:
                    would_delete.append(fid)
                    continue
                try:
                    client.delete_file(fid)
                    deleted.append(fid)
                except KeboolaApiError as exc:
                    failed.append({"id": fid, "error": exc.message})
        finally:
            client.close()

        result: dict[str, Any] = {
            "project_alias": alias,
            "deleted": deleted,
            "failed": failed,
            "dry_run": dry_run,
        }
        if dry_run:
            result["would_delete"] = would_delete
        return result

    def tag_file(
        self,
        alias: str,
        file_id: int,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add and/or remove tags on a Storage File.

        Args:
            alias: Project alias.
            file_id: Storage file ID.
            add_tags: Tags to add.
            remove_tags: Tags to remove.

        Returns:
            Dict with operation results.
        """
        from ..errors import KeboolaApiError

        projects = self.resolve_projects([alias])
        project = projects[alias]

        added: list[str] = []
        removed: list[str] = []
        errors: list[dict[str, str]] = []

        client = self._client_factory(project.stack_url, project.token)
        try:
            for tag in add_tags or []:
                try:
                    client.tag_file(file_id, tag)
                    added.append(tag)
                except KeboolaApiError as exc:
                    errors.append({"tag": tag, "action": "add", "error": exc.message})

            for tag in remove_tags or []:
                try:
                    client.untag_file(file_id, tag)
                    removed.append(tag)
                except KeboolaApiError as exc:
                    errors.append({"tag": tag, "action": "remove", "error": exc.message})
        finally:
            client.close()

        return {
            "project_alias": alias,
            "file_id": file_id,
            "added": added,
            "removed": removed,
            "errors": errors,
        }

    def load_file_to_table(
        self,
        alias: str,
        file_id: int,
        table_id: str,
        incremental: bool = False,
        delimiter: str = ",",
        enclosure: str = '"',
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Load an existing Storage File into a table.

        Triggers import-async with dataFileId. Useful for importing files
        that are already in Storage (uploaded by components or file-upload).

        Args:
            alias: Project alias.
            file_id: Storage file ID to import.
            table_id: Target table ID.
            incremental: Append rows (True) or full load (False).
            delimiter: CSV column delimiter.
            enclosure: CSV value enclosure character.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with import results.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            job = client.import_table_async(
                table_id=table_id,
                file_id=file_id,
                incremental=incremental,
                delimiter=delimiter,
                enclosure=enclosure,
                branch_id=branch_id,
            )
        finally:
            client.close()

        results = job.get("results", {})
        return {
            "project_alias": alias,
            "file_id": file_id,
            "table_id": table_id,
            "incremental": incremental,
            "imported_rows": results.get("importedRowsCount"),
            "warnings": results.get("warnings", []),
        }

    def unload_table_to_file(
        self,
        alias: str,
        table_id: str,
        columns: list[str] | None = None,
        limit: int | None = None,
        tags: list[str] | None = None,
        download: bool = False,
        output_path: str | None = None,
        branch_id: int | None = None,
        file_type: str = "csv",
        keep_slices: bool = False,
    ) -> dict[str, Any]:
        """Export a table to a Storage File.

        Creates a file in Storage that can be downloaded or used by other
        components. Optionally tags the output file and downloads it locally.

        Args:
            alias: Project alias.
            table_id: Table ID to export.
            columns: Optional list of column names.
            limit: Optional max rows.
            tags: Tags to apply to the exported file.
            download: If True, also download the file locally (CSV only for now).
            output_path: Local output path (only used when download=True).
            branch_id: If set, target a specific dev branch.
            file_type: "csv" (default) or "parquet". Parquet output is sliced;
                use the returned file_id with 'kbagent storage file-detail' to
                work with the slices directly. Parquet + download is not
                supported yet (slices cannot be concatenated into a single
                valid parquet file).

        Returns:
            Dict with export metadata and file info.
        """
        from ..errors import KeboolaApiError

        if file_type not in ("csv", "parquet"):
            raise KeboolaApiError(
                message=f"file_type must be 'csv' or 'parquet', got {file_type!r}",
                status_code=400,
                error_code="VALIDATION_ERROR",
                retryable=False,
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Step 1: Export table async
            job = client.export_table_async(
                table_id=table_id,
                columns=columns,
                limit=limit,
                branch_id=branch_id,
                file_type=file_type,
            )

            # Step 2: Get file ID from job results
            file_info = job.get("results", {}).get("file", {})
            file_id = file_info.get("id")
            if not file_id:
                raise KeboolaApiError(
                    message="Export job completed but no file ID in results",
                    status_code=500,
                    error_code="EXPORT_NO_FILE",
                    retryable=False,
                )

            # Step 3: Tag the exported file (branch-scoped if on dev branch)
            for tag in tags or []:
                client.tag_file(file_id, tag, branch_id=branch_id)

            # Step 4: Get full file detail (branch-scoped if on dev branch)
            file_detail = client.get_file_info(file_id, branch_id=branch_id)

            result: dict[str, Any] = {
                "project_alias": alias,
                "table_id": table_id,
                "file_id": file_id,
                "file_name": file_detail.get("name"),
                "file_size_bytes": file_detail.get("sizeBytes"),
                "is_sliced": file_detail.get("isSliced", False),
                "tags": file_detail.get("tags", []),
                "file_type": file_type,
            }

            # Step 5: Download if requested
            if download:
                table_short = table_id.rsplit(".", 1)[-1]

                if file_type == "parquet":
                    # Parquet slices cannot be concatenated -- save each as its
                    # own file in a directory (manifest.json is also preserved).
                    # Default layout mirrors Keboola's project+table addressing:
                    #   ./{project_alias}/{table_id}.parquet/
                    # which stays unambiguous across multiple exports and reads
                    # natively as a Parquet dataset in pyarrow/Spark/DuckDB.
                    effective_output = output_path or f"{alias}/{table_id}.parquet"
                    slice_info = client.download_sliced_file_to_dir(file_detail, effective_output)
                    result["downloaded"] = True
                    result["output_path"] = slice_info["output_dir"]
                    result["downloaded_bytes"] = slice_info["total_bytes"]
                    result["slice_count"] = slice_info["slice_count"]
                    result["slices"] = slice_info["slices"]
                elif keep_slices and file_detail.get("isSliced"):
                    # Preserve slices as a directory (parallel to parquet layout)
                    effective_output = output_path or f"{alias}/{table_id}.csv"
                    slice_info = client.download_sliced_file_to_dir(file_detail, effective_output)
                    result["downloaded"] = True
                    result["output_path"] = slice_info["output_dir"]
                    result["downloaded_bytes"] = slice_info["total_bytes"]
                    result["slice_count"] = slice_info["slice_count"]
                    result["slices"] = slice_info["slices"]
                    result["keep_slices"] = True
                else:
                    if keep_slices:
                        raise KeboolaApiError(
                            message=(
                                "--keep-slices requires a sliced export; this "
                                "file is a single non-sliced CSV. Drop the flag."
                            ),
                            status_code=400,
                            error_code="NOT_SLICED",
                            retryable=False,
                        )
                    effective_output = output_path or f"{table_short}.csv"

                    if file_detail.get("isSliced"):
                        bytes_written = client.download_sliced_file(file_detail, effective_output)
                    else:
                        download_url = file_detail.get("url")
                        if not download_url:
                            raise KeboolaApiError(
                                message=f"No download URL for file {file_id}",
                                status_code=500,
                                error_code="FILE_NO_URL",
                                retryable=False,
                            )
                        bytes_written = client.download_file(download_url, effective_output)

                    result["downloaded"] = True
                    result["output_path"] = str(Path(effective_output).resolve())
                    result["downloaded_bytes"] = bytes_written
            else:
                result["downloaded"] = False
        finally:
            client.close()

        return result

    # ------------------------------------------------------------------
    # Describe (metadata write) methods
    # ------------------------------------------------------------------

    def describe_bucket(
        self,
        alias: str,
        bucket_id: str,
        description: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set the KBC.description metadata on a storage bucket.

        Idempotent upsert: re-running with a different value overwrites the
        existing entry (Keboola metadata POST is upsert-by-key).

        Args:
            alias: Project alias.
            bucket_id: Bucket ID (e.g. 'in.c-my-bucket').
            description: Human-readable description text.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias, bucket_id, description, result, message.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.set_bucket_metadata(
                bucket_id=bucket_id,
                entries=[("KBC.description", description)],
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "bucket_id": bucket_id,
            "description": description,
            "result": result,
            "message": f"Description set on bucket '{bucket_id}' in project '{alias}'.",
        }

    def describe_table(
        self,
        alias: str,
        table_id: str,
        description: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set the KBC.description metadata on a storage table.

        Idempotent upsert: re-running with a different value overwrites.

        Args:
            alias: Project alias.
            table_id: Full table ID (e.g. 'in.c-bucket.table').
            description: Human-readable description text.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias, table_id, description, result, message.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.set_table_metadata(
                table_id=table_id,
                entries=[("KBC.description", description)],
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "table_id": table_id,
            "description": description,
            "result": result,
            "message": f"Description set on table '{table_id}' in project '{alias}'.",
        }

    def describe_columns(
        self,
        alias: str,
        table_id: str,
        columns: dict[str, str],
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Set per-column descriptions on a storage table.

        Column descriptions are stored as namespaced table metadata using the
        key convention ``KBC.column.{colname}.description``.  Keboola's
        Storage API does not provide a user-writable column-level metadata
        endpoint (``columnMetadata`` is populated exclusively by processing
        components); this convention is the supported alternative for
        annotating columns from the CLI.

        Args:
            alias: Project alias.
            table_id: Full table ID.
            columns: Mapping of column name -> description text.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias, table_id, columns dict, result, message.
        """
        if not columns:
            raise ValueError("At least one column description must be provided.")
        projects = self.resolve_projects([alias])
        project = projects[alias]
        entries = [(f"KBC.column.{name}.description", desc) for name, desc in columns.items()]
        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.set_table_metadata(
                table_id=table_id,
                entries=entries,
                branch_id=branch_id,
            )
        finally:
            client.close()
        return {
            "project_alias": alias,
            "table_id": table_id,
            "columns": columns,
            "result": result,
            "message": (
                f"Descriptions set for {len(columns)} column(s) on table '{table_id}' "
                f"in project '{alias}'."
            ),
        }

    def describe_batch(
        self,
        alias: str,
        from_file: Path,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Apply bucket, table, and column descriptions from a YAML file.

        YAML schema::

            buckets:
              in.c-my-bucket: "Bucket description"
            tables:
              in.c-my-bucket.my-table: "Table description"
            columns:
              in.c-my-bucket.my-table:
                col1: "Column 1 description"
                col2: "Column 2 description"

        All sections are optional; empty or absent sections are silently
        skipped.  Within each section the operations are applied in order.
        A failure in one item does not skip remaining items — all results
        (success and error) are collected and returned.

        Args:
            alias: Project alias.
            from_file: Path to a YAML file with the schema above.
            branch_id: If set, target a specific dev branch.

        Returns:
            Dict with project_alias, applied, errors, applied_count, error_count.
        """
        import yaml

        from ..errors import KeboolaApiError

        if not from_file.is_file():
            raise ValueError(f"Batch file not found: {from_file}")
        raw = yaml.safe_load(from_file.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("Batch file must be a YAML mapping.")

        applied: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        buckets: dict[str, str] = raw.get("buckets") or {}
        tables: dict[str, str] = raw.get("tables") or {}
        columns: dict[str, dict[str, str]] = raw.get("columns") or {}

        for bucket_id, desc in buckets.items():
            try:
                self.describe_bucket(alias, bucket_id, str(desc), branch_id=branch_id)
                applied.append({"type": "bucket", "id": bucket_id, "description": desc})
                logger.debug("describe_batch bucket %s: ok", bucket_id)
            except Exception as exc:
                msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
                errors.append({"type": "bucket", "id": bucket_id, "error": msg})

        for table_id, desc in tables.items():
            try:
                self.describe_table(alias, table_id, str(desc), branch_id=branch_id)
                applied.append({"type": "table", "id": table_id, "description": desc})
            except Exception as exc:
                msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
                errors.append({"type": "table", "id": table_id, "error": msg})

        for table_id, col_map in columns.items():
            if not isinstance(col_map, dict):
                errors.append(
                    {"type": "columns", "id": table_id, "error": "columns entry must be a mapping"}
                )
                continue
            try:
                self.describe_columns(
                    alias, table_id, {k: str(v) for k, v in col_map.items()}, branch_id=branch_id
                )
                applied.append(
                    {
                        "type": "columns",
                        "id": table_id,
                        "columns": {k: str(v) for k, v in col_map.items()},
                    }
                )
            except Exception as exc:
                msg = exc.message if isinstance(exc, KeboolaApiError) else str(exc)
                errors.append({"type": "columns", "id": table_id, "error": msg})

        return {
            "project_alias": alias,
            "applied": applied,
            "errors": errors,
            "applied_count": len(applied),
            "error_count": len(errors),
        }

    # ------------------------------------------------------------------
    # Parallel workers
    # ------------------------------------------------------------------

    def _fetch_buckets(
        self,
        alias: str,
        project: ProjectConfig,
        branch_id: int | None = None,
    ) -> tuple[str, list[dict[str, Any]], bool]:
        """Fetch buckets for a single project (worker for _run_parallel)."""
        from ..errors import KeboolaApiError

        client = self._client_factory(project.stack_url, project.token)
        try:
            buckets = client.list_buckets(include="linkedBuckets", branch_id=branch_id)
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
