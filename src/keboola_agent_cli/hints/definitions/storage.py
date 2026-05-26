"""Hint definitions for storage commands (buckets, tables, files)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── storage buckets ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.buckets",
        description="List storage buckets",
        steps=[
            HintStep(
                comment="List all buckets",
                client=ClientCall(
                    method="list_buckets",
                    args={"include": '"linkedBuckets"', "branch_id": "{branch}"},
                    result_var="buckets",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="list_buckets",
                    args={"aliases": "{project}", "branch_id": "{branch}"},
                ),
            ),
        ],
    )
)

# ── storage bucket-detail ────────���─────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.bucket-detail",
        description="Show bucket detail with tables",
        steps=[
            HintStep(
                comment="Get bucket detail",
                client=ClientCall(
                    method="get_bucket_detail",
                    args={"bucket_id": "{bucket_id}", "branch_id": "{branch}"},
                    result_var="bucket",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="get_bucket_detail",
                    args={
                        "alias": "{project}",
                        "bucket_id": "{bucket_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ── storage create-bucket ──────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.create-bucket",
        description="Create a new storage bucket",
        steps=[
            HintStep(
                comment="Create bucket",
                client=ClientCall(
                    method="create_bucket",
                    args={
                        "stage": "{stage}",
                        "name": "{name}",
                        "description": "{description}",
                        "backend": "{backend}",
                        "branch_id": "{branch}",
                    },
                    result_var="bucket",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="create_bucket",
                    args={
                        "alias": "{project}",
                        "stage": "{stage}",
                        "name": "{name}",
                        "description": "{description}",
                        "backend": "{backend}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ─��� storage delete-bucket ──────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.delete-bucket",
        description="Delete one or more storage buckets",
        steps=[
            HintStep(
                comment="Delete bucket(s)",
                client=ClientCall(
                    method="delete_bucket",
                    args={
                        "bucket_id": "{bucket_id}",
                        "force": "{force}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="delete_buckets",
                    args={
                        "alias": "{project}",
                        "bucket_ids": "{bucket_id}",
                        "force": "{force}",
                        "dry_run": "{dry_run}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=["Client layer deletes one bucket at a time. Loop for batch."],
    )
)

# ── storage tables ─────────────────────────────────���───────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.tables",
        description="List tables in one or more projects",
        steps=[
            HintStep(
                comment="List tables",
                client=ClientCall(
                    method="list_tables",
                    args={
                        "bucket_id": "{bucket_id}",
                        "include": '"columns"',
                        "branch_id": "{branch}",
                    },
                    result_var="tables",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="list_tables",
                    args={
                        "aliases": "{project}",
                        "bucket_id": "{bucket_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ── storage table-detail ─────��─────────────────────────────────���───

HintRegistry.register(
    CommandHint(
        cli_command="storage.table-detail",
        description="Show detailed table information",
        steps=[
            HintStep(
                comment="Get table detail",
                client=ClientCall(
                    method="get_table_detail",
                    args={"table_id": "{table_id}", "branch_id": "{branch}"},
                    result_var="table",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="get_table_detail",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ── storage create-table ─────────────────────────────────────���─────

HintRegistry.register(
    CommandHint(
        cli_command="storage.create-table",
        description="Create a new table with typed columns",
        steps=[
            HintStep(
                comment="Create table (optionally with native types like VARCHAR(40), NUMBER(18,2))",
                client=ClientCall(
                    method="create_table",
                    args={
                        "bucket_id": "{bucket_id}",
                        "name": "{name}",
                        "columns": "{column}",
                        "primary_key": "{primary_key}",
                        "branch_id": "{branch}",
                    },
                    result_var="table",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="create_table",
                    args={
                        "alias": "{project}",
                        "bucket_id": "{bucket_id}",
                        "name": "{name}",
                        "columns": "{column}",
                        "primary_key": "{primary_key}",
                        "branch_id": "{branch}",
                        "not_null_columns": "{not_null}",
                        "defaults": "{default}",
                        "if_not_exists": "{if_not_exists}",
                    },
                ),
            ),
        ],
        notes=[
            "Column specs accept 'name', 'name:TYPE', or 'name:TYPE(length)'.",
            "Native types (VARCHAR, NUMBER, TIMESTAMP_TZ, VARIANT, ...) pass through to the Storage API.",
            "Service mode: --not-null and --default flags add nullable/default to column definitions.",
            "Client mode: build column dicts directly as [{'name': 'pk', 'definition': {'type': 'VARCHAR', 'length': '40', 'nullable': False}}].",
            "In a dev branch, service layer auto-materializes the bucket on 404 (mirrors Keboola Go CLI's EnsureBucketExists). Client mode does not -- call get_bucket_detail + create_bucket first.",
            "if_not_exists=True (0.47.0+) returns {action: 'skipped'} on a duplicate-display-name failure when the table really exists at the expected id. Safe for parallel workers.",
        ],
    )
)

# ── storage upload-table ─────────────────────────────────────────��─

HintRegistry.register(
    CommandHint(
        cli_command="storage.upload-table",
        description="Upload a CSV file into a table",
        steps=[
            HintStep(
                comment="Upload CSV file to table (handles file upload + async import)",
                client=ClientCall(
                    method="upload_table",
                    args={
                        "table_id": "{table_id}",
                        "file_path": "{file}",
                        "incremental": "{incremental}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="upload_table",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "file_path": "{file}",
                        "incremental": "{incremental}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Internally: prepare upload -> upload to cloud -> async import job.",
            "With --auto-create, bucket and table are created if missing.",
        ],
    )
)

# ��─ storage download-table ──���──────────────────────────────────���───

HintRegistry.register(
    CommandHint(
        cli_command="storage.download-table",
        description="Download a table to a local CSV file",
        steps=[
            HintStep(
                comment="Export and download table data",
                client=ClientCall(
                    method="export_table_async",
                    args={
                        "table_id": "{table_id}",
                        "columns": "{columns}",
                        "limit": "{limit}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="download_table",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "output_path": "{output}",
                        "columns": "{columns}",
                        "limit": "{limit}",
                        "branch_id": "{branch}",
                        "keep_slices": "{keep_slices}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer: export_table_async -> get_file_info -> download_file.",
            "Service layer handles the full flow including CSV header prepending.",
            "keep_slices=True writes per-slice files into a directory (DuckDB/polars-friendly).",
        ],
    )
)

# ─�� storage delete-table ───────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.delete-table",
        description="Delete one or more tables",
        steps=[
            HintStep(
                comment="Delete table(s)",
                client=ClientCall(
                    method="delete_table",
                    args={
                        "table_id": "{table_id}",
                        "branch_id": "{branch}",
                        "force": "{force}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="delete_tables",
                    args={
                        "alias": "{project}",
                        "table_ids": "{table_id}",
                        "force": "{force}",
                        "dry_run": "{dry_run}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer deletes one table at a time. Loop for batch.",
            "Use force=True to cascade-delete aliased tables in downstream projects.",
        ],
    )
)

# ── storage truncate-table ────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.truncate-table",
        description="Truncate (delete all rows from) one or more tables; preserves schema",
        steps=[
            HintStep(
                comment="Capture rows_before for the receipt",
                client=ClientCall(
                    method="get_table_detail",
                    args={
                        "table_id": "{table_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="table",
                ),
                service=None,
            ),
            HintStep(
                comment="Truncate the table (preserves columns, PK, descriptions, dependents)",
                client=ClientCall(
                    method="truncate_table",
                    args={
                        "table_id": "{table_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="truncate_tables",
                    args={
                        "alias": "{project}",
                        "table_ids": "{table_id}",
                        "dry_run": "{dry_run}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer truncates one table at a time. Loop for batch.",
            "Endpoint: DELETE /v2/storage/[branch/{id}/]tables/{id}/rows?allowTruncate=1.",
            "Endpoint is uniformly async on every branch -- returns a queued job that _wait_for_storage_job polls to completion. Do NOT pass async=true (the API rejects it).",
            "Table schema, primary key, descriptions, and dependents are preserved.",
        ],
    )
)

# ── storage delete-column ─────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.delete-column",
        description="Delete one or more columns from a table",
        steps=[
            HintStep(
                comment="Delete column(s)",
                client=ClientCall(
                    method="delete_column",
                    args={
                        "table_id": "{table_id}",
                        "column_name": "{column}",
                        "branch_id": "{branch}",
                    },
                    result_var="_",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="delete_columns",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "columns": "{column}",
                        "dry_run": "{dry_run}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer deletes one column at a time. Loop for batch.",
            "Synchronous API — no async job polling needed.",
        ],
    )
)

# ── storage swap-tables ───────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.swap-tables",
        description="Swap two storage tables in a dev branch",
        steps=[
            HintStep(
                comment="Swap two tables (dev branch only; aliases not transferred)",
                client=ClientCall(
                    method="swap_tables",
                    args={
                        "table_id": "{table_id}",
                        "target_table_id": "{target_table_id}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="swap_tables",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "target_table_id": "{target_table_id}",
                        "branch_id": "{branch}",
                        "dry_run": "{dry_run}",
                    },
                ),
            ),
        ],
        notes=[
            "Storage API rejects swaps on production: branch_id is mandatory.",
            "Returns a completed storage job dict (operationName=tableSwap); the client polls the async job to completion before returning.",
            "Aliases keep pointing at the same physical position, exposing the OTHER table's data after the swap.",
        ],
    )
)

# ── storage files ──────��──────────────────────────────────���────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.files",
        description="List files in Storage",
        steps=[
            HintStep(
                comment="List files",
                client=ClientCall(
                    method="list_files",
                    args={
                        "limit": "{limit}",
                        "offset": "{offset}",
                        "tags": "{tag}",
                        "query": "{query}",
                        "branch_id": "{branch}",
                    },
                    result_var="files",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="list_files",
                    args={
                        "alias": "{project}",
                        "limit": "{limit}",
                        "offset": "{offset}",
                        "tags": "{tag}",
                        "query": "{query}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ── storage file-detail ────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.file-detail",
        description="Show file detail",
        steps=[
            HintStep(
                comment="Get file info",
                client=ClientCall(
                    method="get_file_info",
                    args={"file_id": "{file_id}"},
                    result_var="file_info",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="get_file_info",
                    args={"alias": "{project}", "file_id": "{file_id}"},
                ),
            ),
        ],
    )
)

# ── storage file-upload ────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.file-upload",
        description="Upload a file to Storage",
        steps=[
            HintStep(
                comment="Upload file to Storage",
                client=ClientCall(
                    method="upload_file",
                    args={
                        "file_path": "{file}",
                        "tags": "{tag}",
                        "is_permanent": "{permanent}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="upload_file",
                    args={
                        "alias": "{project}",
                        "file_path": "{file}",
                        "name": "{name}",
                        "tags": "{tag}",
                        "is_permanent": "{permanent}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=["Internally: prepare upload -> upload to cloud storage (S3/GCS/Azure)."],
    )
)

# ── storage file-download ──────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.file-download",
        description="Download a file from Storage",
        steps=[
            HintStep(
                comment="Download file from Storage",
                client=ClientCall(
                    method="get_file_info",
                    args={"file_id": "{file_id}"},
                    result_var="file_info",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="download_file",
                    args={
                        "alias": "{project}",
                        "file_id": "{file_id}",
                        "tags": "{tag}",
                        "output_path": "{output}",
                    },
                ),
            ),
        ],
        notes=[
            "Client layer: get_file_info -> download from cloud URL.",
            "Service layer handles tag-based lookup and sliced file assembly.",
        ],
    )
)

# ── storage file-delete ─────────────────────────────────────────��──

HintRegistry.register(
    CommandHint(
        cli_command="storage.file-delete",
        description="Delete one or more files",
        steps=[
            HintStep(
                comment="Delete file(s)",
                client=ClientCall(
                    method="delete_file",
                    args={"file_id": "{file_id}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="delete_files",
                    args={
                        "alias": "{project}",
                        "file_ids": "{file_id}",
                        "dry_run": "{dry_run}",
                    },
                ),
            ),
        ],
    )
)

# ── storage file-tag ────────���──────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.file-tag",
        description="Add or remove tags on a file",
        steps=[
            HintStep(
                comment="Manage file tags",
                client=ClientCall(
                    method="tag_file",
                    args={"file_id": "{file_id}", "tag": "{add}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="tag_file",
                    args={
                        "alias": "{project}",
                        "file_id": "{file_id}",
                        "add_tags": "{add}",
                        "remove_tags": "{remove}",
                    },
                ),
            ),
        ],
        notes=["Client layer: call tag_file() per tag. Service handles add + remove."],
    )
)

# ── storage load-file ─────────────────────────────────────���────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.load-file",
        description="Load an existing Storage file into a table",
        steps=[
            HintStep(
                comment="Import file into table (async)",
                client=ClientCall(
                    method="import_table_async",
                    args={
                        "table_id": "{table_id}",
                        "file_id": "{file_id}",
                        "incremental": "{incremental}",
                        "delimiter": "{delimiter}",
                        "enclosure": "{enclosure}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="load_file_to_table",
                    args={
                        "alias": "{project}",
                        "file_id": "{file_id}",
                        "table_id": "{table_id}",
                        "incremental": "{incremental}",
                        "delimiter": "{delimiter}",
                        "enclosure": "{enclosure}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
    )
)

# ─�� storage unload-table ───────────────────────────────────────��───

HintRegistry.register(
    CommandHint(
        cli_command="storage.unload-table",
        description="Export a table to a Storage file",
        steps=[
            HintStep(
                comment="Export table to Storage file (async)",
                client=ClientCall(
                    method="export_table_async",
                    args={
                        "table_id": "{table_id}",
                        "columns": "{columns}",
                        "limit": "{limit}",
                        "branch_id": "{branch}",
                        "file_type": "{file_type}",
                    },
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="unload_table_to_file",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "columns": "{columns}",
                        "limit": "{limit}",
                        "tags": "{tag}",
                        "download": "{download}",
                        "output_path": "{output}",
                        "branch_id": "{branch}",
                        "file_type": "{file_type}",
                        "keep_slices": "{keep_slices}",
                    },
                ),
            ),
        ],
        notes=[
            "Service layer handles optional tagging and local download.",
            "keep_slices=True (CSV only) preserves slices in a directory.",
        ],
    )
)

# ── storage describe-bucket ────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.describe-bucket",
        description="Set the description on a storage bucket",
        steps=[
            HintStep(
                comment="Upsert KBC.description in bucket metadata (provider='user')",
                client=ClientCall(
                    method="set_bucket_metadata",
                    args={
                        "bucket_id": "{bucket_id}",
                        "entries": '[("KBC.description", "{description}")]',
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="describe_bucket",
                    args={
                        "alias": "{project}",
                        "bucket_id": "{bucket_id}",
                        "description": "{description}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "POST /v2/storage/buckets/{id}/metadata with provider='user' is an upsert-by-key.",
            "Description is readable via 'storage bucket-detail --json .data.description'.",
        ],
    )
)

# ── storage describe-table ────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.describe-table",
        description="Set the description on a storage table",
        steps=[
            HintStep(
                comment="Upsert KBC.description in table metadata (provider='user')",
                client=ClientCall(
                    method="set_table_metadata",
                    args={
                        "table_id": "{table_id}",
                        "entries": '[("KBC.description", "{description}")]',
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="describe_table",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "description": "{description}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Description is readable via 'storage table-detail --json | .data.description'.",
        ],
    )
)

# ── storage describe-column ───────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.describe-column",
        description="Set per-column descriptions on a storage table",
        steps=[
            HintStep(
                comment="Store column descriptions as KBC.column.{name}.description in table metadata",
                client=ClientCall(
                    method="set_table_metadata",
                    args={
                        "table_id": "{table_id}",
                        "entries": '[("KBC.column.{col}.description", "{description}")]',
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="describe_columns",
                    args={
                        "alias": "{project}",
                        "table_id": "{table_id}",
                        "columns": '{"{col}": "{description}"}',
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "Column descriptions use key KBC.column.{name}.description in table metadata.",
            "They appear under column_details[].description in 'storage table-detail --json'.",
            "Keboola does not provide a user-writable column-metadata endpoint; this is the supported convention.",
        ],
    )
)

# ── storage describe-batch ────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="storage.describe-batch",
        description="Apply bucket/table/column descriptions from a YAML file",
        steps=[
            HintStep(
                comment="Load YAML and apply descriptions to all listed assets",
                client=ClientCall(
                    method="set_bucket_metadata / set_table_metadata",
                    args={
                        "from_file": "{from_file}",
                        "branch_id": "{branch}",
                    },
                    result_var="result",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="describe_batch",
                    args={
                        "alias": "{project}",
                        "from_file": "Path('{from_file}')",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "YAML sections: 'buckets', 'tables', 'columns' (all optional).",
            "Failures are collected -- one error does not abort the rest.",
        ],
    )
)
