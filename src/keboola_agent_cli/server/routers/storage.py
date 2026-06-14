"""Storage endpoints (buckets, tables, files, describe)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/storage", tags=["storage"])


class CreateBucket(BaseModel):
    stage: str
    name: str
    description: str | None = None
    backend: str | None = None
    branch_id: int | None = None


class CreateTable(BaseModel):
    bucket_id: str
    name: str
    columns: list[str]
    primary_key: list[str] | None = None
    not_null_columns: list[str] | None = None
    defaults: list[str] | None = None
    branch_id: int | None = None
    if_not_exists: bool = False


class DescribeBucket(BaseModel):
    description: str
    branch_id: int | None = None


class DescribeTable(BaseModel):
    description: str
    branch_id: int | None = None


class DescribeColumns(BaseModel):
    columns: dict[str, str]
    branch_id: int | None = None


class TagFile(BaseModel):
    add: list[str] | None = None
    remove: list[str] | None = None


class LoadFileToTable(BaseModel):
    file_id: int
    table_id: str
    incremental: bool = False
    delimiter: str = ","
    enclosure: str = '"'
    branch_id: int | None = None


class SwapTables(BaseModel):
    target_table_id: str
    branch_id: int


class CloneTable(BaseModel):
    branch_id: int


@router.get("/buckets", summary="List storage buckets")
def list_buckets(
    project: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List storage buckets in one or more projects. Mirrors `kbagent storage buckets`."""
    aliases = [project] if project else None
    return registry.storage.list_buckets(aliases=aliases, branch_id=branch_id)


@router.get("/buckets/{project}/{bucket_id:path}", summary="Get bucket detail")
def bucket_detail(
    project: str,
    bucket_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch detail for a single bucket. Mirrors `kbagent storage bucket-detail`."""
    return registry.storage.get_bucket_detail(
        alias=project, bucket_id=bucket_id, branch_id=branch_id
    )


@router.post("/buckets/{project}", summary="Create a bucket")
def create_bucket(
    project: str, body: CreateBucket, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a new storage bucket. Mirrors `kbagent storage create-bucket`."""
    return registry.storage.create_bucket(
        alias=project,
        stage=body.stage,
        name=body.name,
        description=body.description,
        backend=body.backend,
        branch_id=body.branch_id,
    )


@router.delete("/buckets/{project}", summary="Delete buckets")
def delete_buckets(
    project: str,
    bucket_id: list[str] = Query(...),
    force: bool = False,
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete one or more storage buckets. Mirrors `kbagent storage delete-bucket`."""
    return registry.storage.delete_buckets(
        alias=project,
        bucket_ids=bucket_id,
        force=force,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.post("/buckets/{project}/{bucket_id:path}/describe", summary="Set bucket description")
def describe_bucket(
    project: str,
    bucket_id: str,
    body: DescribeBucket,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Set or update a bucket's description. Mirrors `kbagent storage describe-bucket`."""
    return registry.storage.describe_bucket(
        alias=project,
        bucket_id=bucket_id,
        description=body.description,
        branch_id=body.branch_id,
    )


@router.get("/tables", summary="List storage tables")
def list_tables(
    project: list[str] | None = Query(None),
    bucket_id: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List tables across one or more projects. Mirrors `kbagent storage tables`."""
    return registry.storage.list_tables(aliases=project, bucket_id=bucket_id, branch_id=branch_id)


@router.get("/table-detail/{project}/{table_id:path}", summary="Get table detail")
def table_detail(
    project: str,
    table_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch detail for a single table. Mirrors `kbagent storage table-detail`."""
    return registry.storage.get_table_detail(alias=project, table_id=table_id, branch_id=branch_id)


@router.get("/table-preview/{project}/{table_id:path}", summary="Preview table rows")
def preview_table_v2(
    project: str,
    table_id: str,
    limit: int = 100,
    columns: list[str] | None = Query(None),
    where_column: str | None = None,
    where_operator: str = "eq",
    where_value: list[str] | None = Query(None),
    changed_since: str | None = None,
    changed_until: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Return up to ``limit`` rows via the synchronous data-preview endpoint.

    Uses ``/v2/storage/tables/{id}/data-preview`` -- synchronous, capped at
    a few hundred rows, no async export job. Storage API caps sync preview
    at 30 columns max.

    Lives under ``/table-preview`` (not ``/tables/.../preview``) because
    ``{table_id:path}`` is greedy and would conflict with sibling routes.
    """
    import csv as _csv
    import io as _io

    projects = registry.storage.resolve_projects([project])
    proj = projects[project]
    client = registry.storage._client_factory(proj.stack_url, proj.token)
    try:
        text = client.get_table_data_preview(
            table_id=table_id,
            limit=limit,
            columns=columns,
            where_column=where_column,
            where_operator=where_operator,
            where_values=where_value,
            changed_since=changed_since,
            changed_until=changed_until,
        )
    finally:
        client.close()
    reader = _csv.reader(_io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"header": [], "rows": [], "row_count": 0}
    return {"header": rows[0], "rows": rows[1:], "row_count": len(rows) - 1}


@router.get("/table-download/{project}/{table_id:path}", summary="Download table as CSV")
def download_table_v2(
    project: str,
    table_id: str,
    columns: list[str] | None = Query(None),
    limit: int | None = None,
    branch_id: int | None = None,
    where_column: str | None = None,
    where_operator: str = "eq",
    where_value: list[str] | None = Query(None),
    changed_since: str | None = None,
    changed_until: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> FileResponse:
    """Download the table as CSV (uses async export). Optional where/changed filters."""
    out_path = Path(tempfile.mkstemp(suffix=".csv", prefix="kbagent-")[1])
    registry.storage.download_table(
        alias=project,
        table_id=table_id,
        output_path=str(out_path),
        columns=columns,
        limit=limit,
        branch_id=branch_id,
        where_column=where_column,
        where_operator=where_operator,
        where_values=where_value,
        changed_since=changed_since,
        changed_until=changed_until,
    )
    return FileResponse(
        path=str(out_path),
        media_type="text/csv",
        filename=f"{table_id.replace('.', '_')}.csv",
    )


@router.post("/tables/{project}", summary="Create a table")
def create_table(
    project: str, body: CreateTable, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a typed storage table. Mirrors `kbagent storage create-table`."""
    return registry.storage.create_table(
        alias=project,
        bucket_id=body.bucket_id,
        name=body.name,
        columns=body.columns,
        primary_key=body.primary_key,
        branch_id=body.branch_id,
        not_null_columns=body.not_null_columns,
        defaults=body.defaults,
        if_not_exists=body.if_not_exists,
    )


@router.post("/tables/{project}/upload", summary="Upload data into a table")
async def upload_table(
    project: str,
    table_id: str = Form(...),
    incremental: bool = Form(False),
    branch_id: int | None = Form(None),
    file: UploadFile = File(...),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Upload a CSV file into an existing table. Mirrors `kbagent storage upload-table`."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "x").suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return registry.storage.upload_table(
            alias=project,
            table_id=table_id,
            file_path=str(tmp_path),
            incremental=incremental,
            branch_id=branch_id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.delete("/tables/{project}", summary="Delete tables")
def delete_tables(
    project: str,
    table_id: list[str] = Query(...),
    force: bool = False,
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete one or more storage tables. Mirrors `kbagent storage delete-table`."""
    return registry.storage.delete_tables(
        alias=project,
        table_ids=table_id,
        force=force,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.post("/tables/{project}/truncate", summary="Truncate tables")
def truncate_tables(
    project: str,
    table_id: list[str] = Query(...),
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Truncate (empty) one or more storage tables. Mirrors `kbagent storage truncate-table`."""
    return registry.storage.truncate_tables(
        alias=project,
        table_ids=table_id,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.delete("/columns/{project}/{table_id:path}", summary="Delete table columns")
def delete_columns(
    project: str,
    table_id: str,
    column: list[str] = Query(...),
    force: bool = False,
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete columns from a table. Mirrors `kbagent storage delete-column`."""
    return registry.storage.delete_columns(
        alias=project,
        table_id=table_id,
        columns=column,
        force=force,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.post("/tables/{project}/{table_id:path}/swap", summary="Swap two tables")
def swap_tables(
    project: str,
    table_id: str,
    body: SwapTables,
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Atomically swap two storage tables. Mirrors `kbagent storage swap-tables`."""
    return registry.storage.swap_tables(
        alias=project,
        table_id=table_id,
        target_table_id=body.target_table_id,
        branch_id=body.branch_id,
        dry_run=dry_run,
    )


@router.post(
    "/tables/{project}/{table_id:path}/pull",
    summary="Clone a table into a dev branch",
)
def clone_table(
    project: str,
    table_id: str,
    body: CloneTable,
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Pull (clone) a production table into a dev branch.

    Mirrors `kbagent storage clone-table`.
    """
    return registry.storage.clone_table(
        alias=project,
        table_id=table_id,
        branch_id=body.branch_id,
        dry_run=dry_run,
    )


@router.post("/tables/{project}/{table_id:path}/describe", summary="Set table description")
def describe_table(
    project: str,
    table_id: str,
    body: DescribeTable,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Set or update a table's description. Mirrors `kbagent storage describe-table`."""
    return registry.storage.describe_table(
        alias=project,
        table_id=table_id,
        description=body.description,
        branch_id=body.branch_id,
    )


@router.post("/columns/{project}/{table_id:path}/describe", summary="Set column descriptions")
def describe_columns(
    project: str,
    table_id: str,
    body: DescribeColumns,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Set descriptions for table columns. Mirrors `kbagent storage describe-column`."""
    return registry.storage.describe_columns(
        alias=project,
        table_id=table_id,
        columns=body.columns,
        branch_id=body.branch_id,
    )


# Registered AFTER the more specific /columns/.../describe route above: the
# greedy {table_id:path} would otherwise shadow that POST and swallow a
# ".../describe" suffix as part of the table id.
@router.post("/columns/{project}/{table_id:path}", summary="Add a table column")
def add_column(
    project: str,
    table_id: str,
    column: str = Query(...),
    not_null: bool = False,
    default: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Add a single column to a table. Mirrors `kbagent storage add-column`."""
    return registry.storage.add_column(
        alias=project,
        table_id=table_id,
        column=column,
        not_null=not_null,
        default=default,
        branch_id=branch_id,
    )


# ---- Files ----


@router.get("/files", summary="List storage files")
def list_files(
    project: str,
    tag: list[str] | None = Query(None),
    limit: int = 50,
    offset: int = 0,
    query: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List files in a project's Storage Files API. Mirrors `kbagent storage files`."""
    return registry.storage.list_files(
        alias=project,
        tags=tag,
        limit=limit,
        offset=offset,
        query=query,
        branch_id=branch_id,
    )


@router.post("/files/upload", summary="Upload a file to Storage")
async def upload_file(
    project: str = Form(...),
    name: str | None = Form(None),
    permanent: bool = Form(False),
    tag: list[str] = Form([]),
    branch_id: int | None = Form(None),
    file: UploadFile = File(...),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Upload a file into Storage Files. Mirrors `kbagent storage file-upload`."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "x").suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return registry.storage.upload_file(
            alias=project,
            file_path=str(tmp_path),
            name=name or file.filename,
            tags=tag,
            is_permanent=permanent,
            branch_id=branch_id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/files/{project}/{file_id}", summary="Get file detail")
def file_detail(
    project: str,
    file_id: int,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch detail for a single Storage file. Mirrors `kbagent storage file-detail`."""
    return registry.storage.get_file_info(alias=project, file_id=file_id)


@router.get("/files/{project}/{file_id}/download", summary="Download a file")
def file_download(
    project: str,
    file_id: int,
    registry: ServiceRegistry = Depends(get_registry),
) -> FileResponse:
    """Download a Storage file. Mirrors `kbagent storage file-download`."""
    out_dir = Path(tempfile.mkdtemp(prefix="kbagent-file-"))
    result = registry.storage.download_file(
        alias=project, file_id=file_id, output_path=str(out_dir)
    )
    file_path = result.get("local_path") if isinstance(result, dict) else None
    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=500, detail="Download produced no file.")
    return FileResponse(
        path=file_path, media_type="application/octet-stream", filename=Path(file_path).name
    )


@router.delete("/files/{project}", summary="Delete files")
def delete_files(
    project: str,
    file_id: list[int] = Query(...),
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete one or more Storage files. Mirrors `kbagent storage file-delete`."""
    return registry.storage.delete_files(alias=project, file_ids=file_id, dry_run=dry_run)


@router.post("/files/{project}/{file_id}/tag", summary="Add or remove file tags")
def tag_file(
    project: str,
    file_id: int,
    body: TagFile,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Add or remove tags on a Storage file. Mirrors `kbagent storage file-tag`."""
    return registry.storage.tag_file(
        alias=project,
        file_id=file_id,
        add_tags=body.add,
        remove_tags=body.remove,
    )


@router.post("/files/{project}/load-to-table", summary="Load a file into a table")
def load_file_to_table(
    project: str,
    body: LoadFileToTable,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Load a Storage file's contents into a table. Mirrors `kbagent storage load-file`."""
    return registry.storage.load_file_to_table(
        alias=project,
        file_id=body.file_id,
        table_id=body.table_id,
        incremental=body.incremental,
        delimiter=body.delimiter,
        enclosure=body.enclosure,
        branch_id=body.branch_id,
    )
