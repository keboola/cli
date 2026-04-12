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
        description="List tables in a project",
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
                        "alias": "{project}",
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
                comment="Create table",
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
                    },
                ),
            ),
        ],
        notes=["Columns format: 'name:TYPE' (e.g. 'id:INTEGER', 'name:STRING')."],
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
                    },
                ),
            ),
        ],
        notes=[
            "Client layer: export_table_async -> get_file_info -> download_file.",
            "Service layer handles the full flow including CSV header prepending.",
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
                    args={"table_id": "{table_id}", "branch_id": "{branch}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="StorageService",
                    service_module="storage_service",
                    method="delete_tables",
                    args={
                        "alias": "{project}",
                        "table_ids": "{table_id}",
                        "dry_run": "{dry_run}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=["Client layer deletes one table at a time. Loop for batch."],
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
                    },
                ),
            ),
        ],
        notes=["Service layer handles optional tagging and local download."],
    )
)
