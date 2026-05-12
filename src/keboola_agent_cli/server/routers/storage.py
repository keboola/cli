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


@router.get("/buckets")
def list_buckets(
    project: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    aliases = [project] if project else None
    return registry.storage.list_buckets(aliases=aliases, branch_id=branch_id)


@router.get("/buckets/{project}/{bucket_id:path}")
def bucket_detail(
    project: str,
    bucket_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.get_bucket_detail(
        alias=project, bucket_id=bucket_id, branch_id=branch_id
    )


@router.post("/buckets/{project}")
def create_bucket(
    project: str, body: CreateBucket, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.storage.create_bucket(
        alias=project,
        stage=body.stage,
        name=body.name,
        description=body.description,
        backend=body.backend,
        branch_id=body.branch_id,
    )


@router.delete("/buckets/{project}")
def delete_buckets(
    project: str,
    bucket_id: list[str] = Query(...),
    force: bool = False,
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.delete_buckets(
        alias=project,
        bucket_ids=bucket_id,
        force=force,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.post("/buckets/{project}/{bucket_id:path}/describe")
def describe_bucket(
    project: str,
    bucket_id: str,
    body: DescribeBucket,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.describe_bucket(
        alias=project,
        bucket_id=bucket_id,
        description=body.description,
        branch_id=body.branch_id,
    )


@router.get("/tables")
def list_tables(
    project: list[str] | None = Query(None),
    bucket_id: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.list_tables(aliases=project, bucket_id=bucket_id, branch_id=branch_id)


@router.get("/tables/{project}/{table_id:path}")
def table_detail(
    project: str,
    table_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.get_table_detail(alias=project, table_id=table_id, branch_id=branch_id)


@router.post("/tables/{project}")
def create_table(
    project: str, body: CreateTable, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.storage.create_table(
        alias=project,
        bucket_id=body.bucket_id,
        name=body.name,
        columns=body.columns,
        primary_key=body.primary_key,
        branch_id=body.branch_id,
        not_null_columns=body.not_null_columns,
        defaults=body.defaults,
    )


@router.post("/tables/{project}/upload")
async def upload_table(
    project: str,
    table_id: str = Form(...),
    incremental: bool = Form(False),
    branch_id: int | None = Form(None),
    file: UploadFile = File(...),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "x").suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return registry.storage.upload_table(
            alias=project,
            table_id=table_id,
            file_path=tmp_path,
            incremental=incremental,
            branch_id=branch_id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/tables/{project}/{table_id:path}/download")
def download_table(
    project: str,
    table_id: str,
    columns: list[str] | None = Query(None),
    limit: int | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> FileResponse:
    out_path = Path(tempfile.mkstemp(suffix=".csv", prefix="kbagent-")[1])
    registry.storage.download_table(
        alias=project,
        table_id=table_id,
        output_path=out_path,
        columns=columns,
        limit=limit,
        branch_id=branch_id,
    )
    return FileResponse(
        path=str(out_path),
        media_type="text/csv",
        filename=f"{table_id.replace('.', '_')}.csv",
    )


@router.delete("/tables/{project}")
def delete_tables(
    project: str,
    table_id: list[str] = Query(...),
    force: bool = False,
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.delete_tables(
        alias=project,
        table_ids=table_id,
        force=force,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.post("/tables/{project}/truncate")
def truncate_tables(
    project: str,
    table_id: list[str] = Query(...),
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.truncate_tables(
        alias=project,
        table_ids=table_id,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.delete("/columns/{project}/{table_id:path}")
def delete_columns(
    project: str,
    table_id: str,
    column: list[str] = Query(...),
    force: bool = False,
    dry_run: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.delete_columns(
        alias=project,
        table_id=table_id,
        columns=column,
        force=force,
        dry_run=dry_run,
        branch_id=branch_id,
    )


@router.post("/tables/{project}/{table_id:path}/swap")
def swap_tables(
    project: str,
    table_id: str,
    body: SwapTables,
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.swap_tables(
        alias=project,
        table_id=table_id,
        target_table_id=body.target_table_id,
        branch_id=body.branch_id,
        dry_run=dry_run,
    )


@router.post("/tables/{project}/{table_id:path}/describe")
def describe_table(
    project: str,
    table_id: str,
    body: DescribeTable,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.describe_table(
        alias=project,
        table_id=table_id,
        description=body.description,
        branch_id=body.branch_id,
    )


@router.post("/columns/{project}/{table_id:path}/describe")
def describe_columns(
    project: str,
    table_id: str,
    body: DescribeColumns,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.describe_columns(
        alias=project,
        table_id=table_id,
        column_descriptions=body.columns,
        branch_id=body.branch_id,
    )


# ---- Files ----


@router.get("/files")
def list_files(
    project: str,
    tag: list[str] | None = Query(None),
    limit: int = 50,
    offset: int = 0,
    query: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.list_files(
        alias=project,
        tags=tag,
        limit=limit,
        offset=offset,
        query=query,
        branch_id=branch_id,
    )


@router.post("/files/upload")
async def upload_file(
    project: str = Form(...),
    name: str | None = Form(None),
    permanent: bool = Form(False),
    tag: list[str] = Form([]),
    branch_id: int | None = Form(None),
    file: UploadFile = File(...),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "x").suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return registry.storage.upload_file(
            alias=project,
            file_path=tmp_path,
            name=name or file.filename,
            tags=tag,
            is_permanent=permanent,
            branch_id=branch_id,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/files/{project}/{file_id}")
def file_detail(
    project: str,
    file_id: int,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.get_file_info(alias=project, file_id=file_id)


@router.get("/files/{project}/{file_id}/download")
def file_download(
    project: str,
    file_id: int,
    registry: ServiceRegistry = Depends(get_registry),
) -> FileResponse:
    out_dir = Path(tempfile.mkdtemp(prefix="kbagent-file-"))
    result = registry.storage.download_file(alias=project, file_id=file_id, output_dir=out_dir)
    file_path = result.get("local_path") if isinstance(result, dict) else None
    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=500, detail="Download produced no file.")
    return FileResponse(
        path=file_path, media_type="application/octet-stream", filename=Path(file_path).name
    )


@router.delete("/files/{project}")
def delete_files(
    project: str,
    file_id: list[int] = Query(...),
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.delete_files(alias=project, file_ids=file_id, dry_run=dry_run)


@router.post("/files/{project}/{file_id}/tag")
def tag_file(
    project: str,
    file_id: int,
    body: TagFile,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.tag_file(
        alias=project,
        file_id=file_id,
        add_tags=body.add,
        remove_tags=body.remove,
    )


@router.post("/files/{project}/load-to-table")
def load_file_to_table(
    project: str,
    body: LoadFileToTable,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.storage.load_file_to_table(
        alias=project,
        file_id=body.file_id,
        table_id=body.table_id,
        incremental=body.incremental,
        delimiter=body.delimiter,
        enclosure=body.enclosure,
        branch_id=body.branch_id,
    )
