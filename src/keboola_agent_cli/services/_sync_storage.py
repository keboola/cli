"""Storage-metadata writers (``sync pull``) + bucket re-creation (``sync clone``).

Free functions that materialise a project's storage metadata (buckets/tables),
per-config job history, and table data samples to the filesystem during
``sync pull``. Only :func:`fetch_jobs_per_config` needs the ``SyncService`` (for
its ``_resolve_max_workers`` helper); the rest are pure. ``_ensure_path_within``
(the storage-write path-traversal guard) moved here with its only callers.
:func:`create_buckets_from_export` reads the ``buckets.json`` written on pull
back, to recreate a clone's buckets in the target project.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import (
    ENCRYPTED_COLUMN_MASK,
    ENCRYPTED_COLUMN_PREFIX,
    JOBS_FILENAME,
    STORAGE_BUCKETS_FILENAME,
    STORAGE_DIR_NAME,
    STORAGE_SAMPLES_DIR_NAME,
)
from ..errors import ConfigError, KeboolaApiError
from ..sync.manifest import ManifestConfiguration
from ..sync.naming import sanitize_path_segment

if TYPE_CHECKING:
    from ..client import KeboolaClient
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

# Storage API sync-export column ceiling (avoids the API 400 on wide tables).
_MAX_SYNC_COLUMNS = 30


def _ensure_path_within(base_dir: Path, target: Path, what: str) -> None:
    """Reject a write whose path escapes *base_dir* (defense-in-depth).

    Mirrors ``_ensure_within_branch`` for non-config writes (storage metadata +
    samples) whose path segments derive from API-controlled bucket ids / table
    names (GHSA-833q-c5wv-26r7). Raises ConfigError on escape so a malformed or
    compromised Storage response cannot write outside the sync workspace.
    """
    try:
        base_resolved = base_dir.resolve()
        target_resolved = target.resolve()
    except OSError as exc:
        raise ConfigError(f"Cannot resolve sync path: {exc}") from exc
    if not target_resolved.is_relative_to(base_resolved):
        raise ConfigError(
            f"Storage path escapes sync workspace ({what}). Refusing to write "
            f"outside '{base_resolved}'. This indicates a malformed or compromised "
            f"API response or a path-sanitization regression."
        )


def _linked_source(bucket: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``{"bucket_id", "project_id"}`` of a linked bucket's source, else ``None``."""
    source = bucket.get("sourceBucket")
    if not source:
        return None
    return {
        "bucket_id": source.get("id", ""),
        "project_id": (source.get("project") or {}).get("id"),
    }


def write_storage_metadata(
    project_root: Path,
    buckets: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    samples: dict[str, str],
) -> dict[str, int]:
    """Write storage bucket and table metadata to the filesystem.

    Creates:
        storage/buckets.json - list of all buckets
        storage/tables/{bucket_id}/{table_name}.json - per-table metadata
        storage/samples/{bucket}/{table}/sample.csv - data samples (if any)

    Returns:
        Dict with counts: buckets, tables, samples written.
    """
    storage_dir = project_root / STORAGE_DIR_NAME
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Write buckets index. API may return null for tablesCount / dataSizeBytes
    # on empty buckets; coerce to 0 (dict.get default only fires when key is
    # missing, not when the value is explicitly null).
    bucket_summaries = [
        {
            "id": b.get("id", ""),
            "name": b.get("name", ""),
            "stage": b.get("stage", ""),
            "description": b.get("description", ""),
            "backend": b.get("backend", ""),
            "source_bucket": _linked_source(b),
            "tables_count": b.get("tablesCount") or 0,
            "data_size_bytes": b.get("dataSizeBytes") or 0,
            "metadata": b.get("metadata", []),
        }
        for b in buckets
    ]
    buckets_file = storage_dir / STORAGE_BUCKETS_FILENAME
    buckets_file.write_text(
        json.dumps(bucket_summaries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Group tables by bucket
    tables_by_bucket: dict[str, list[dict[str, Any]]] = {}
    for t in tables:
        bucket_id = (
            t.get("bucket", {}).get("id", "")
            if isinstance(t.get("bucket"), dict)
            else t.get("bucketId", "")
        )
        if not bucket_id:
            continue
        tables_by_bucket.setdefault(bucket_id, []).append(t)

    tables_written = 0
    tables_dir = storage_dir / "tables"
    for bucket_id, bucket_tables in tables_by_bucket.items():
        # Sanitize bucket_id for filesystem. sanitize_path_segment first kills
        # traversal (`/`, `..`, absolute paths); the trailing replace keeps the
        # legacy `in.c-foo` -> `in-c-foo` directory naming for legitimate ids
        # (GHSA-833q-c5wv-26r7).
        safe_bucket = sanitize_path_segment(bucket_id).replace(".", "-")
        bucket_dir = tables_dir / safe_bucket
        _ensure_path_within(storage_dir, bucket_dir, f"bucket_id={bucket_id!r}")
        bucket_dir.mkdir(parents=True, exist_ok=True)

        for t in bucket_tables:
            table_name = t.get("name", "unknown")
            table_meta = {
                "id": t.get("id", ""),
                "name": table_name,
                "primary_key": t.get("primaryKey", []),
                "columns": t.get("columns", []),
                # API may return null for rowsCount / dataSizeBytes on
                # newly-created or empty tables; coerce to 0 explicitly
                # (dict.get default only fires when the key is missing).
                "rows_count": t.get("rowsCount") or 0,
                "data_size_bytes": t.get("dataSizeBytes") or 0,
                "last_import_date": t.get("lastImportDate", ""),
                "last_change_date": t.get("lastChangeDate", ""),
                "description": t.get("description", ""),
                "metadata": t.get("metadata", []),
                "column_metadata": t.get("columnMetadata", {}),
            }
            # The table name comes from the API; sanitize it for the filename
            # and assert containment so a crafted name cannot escape the bucket
            # dir (GHSA-833q-c5wv-26r7). The original name stays verbatim in the
            # metadata body above.
            safe_table = sanitize_path_segment(table_name)
            table_file = bucket_dir / f"{safe_table}.json"
            _ensure_path_within(storage_dir, table_file, f"table={table_name!r}")
            table_file.write_text(
                json.dumps(table_meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tables_written += 1

    # Write samples
    samples_written = 0
    if samples:
        samples_dir = storage_dir / STORAGE_SAMPLES_DIR_NAME
        for table_id, csv_data in samples.items():
            # table_id format: "in.c-bucket.table" -> samples/in-c-bucket/table/
            # Every segment derives from the API table_id; sanitize each and
            # assert containment so a crafted id cannot escape (GHSA-833q).
            parts = table_id.split(".", 2)
            if len(parts) >= 3:
                safe_bucket = sanitize_path_segment(f"{parts[0]}-{parts[1]}")
                safe_table = sanitize_path_segment(parts[2])
            else:
                safe_bucket = sanitize_path_segment(table_id.replace(".", "-"))
                safe_table = "data"
            sample_dir = samples_dir / safe_bucket / safe_table
            _ensure_path_within(storage_dir, sample_dir, f"table_id={table_id!r}")
            sample_dir.mkdir(parents=True, exist_ok=True)

            masked_csv = mask_encrypted_columns(csv_data)
            (sample_dir / "sample.csv").write_text(masked_csv, encoding="utf-8")
            samples_written += 1

    return {
        "buckets": len(buckets),
        "tables": tables_written,
        "samples": samples_written,
    }


def fetch_jobs_per_config(
    service: SyncService,
    client: Any,
    components: list[dict[str, Any]],
    job_limit: int,
) -> list[dict[str, Any]]:
    """Fetch jobs per config via /search/jobs in parallel.

    Used as fallback when the grouped-jobs API cannot return all configs in a
    single call (jobsPerGroup * limit <= 500 constraint). Returns data in the
    same format as ``list_jobs_grouped()`` so :func:`write_per_config_jobs`
    works unchanged.
    """
    config_pairs: list[tuple[str, str]] = []
    for comp in components:
        comp_id = comp.get("id", "")
        for cfg in comp.get("configurations", []):
            cfg_id = str(cfg.get("id", ""))
            if comp_id and cfg_id:
                config_pairs.append((comp_id, cfg_id))

    if not config_pairs:
        return []

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    max_workers = min(len(config_pairs), service._resolve_max_workers())

    def _fetch_one(pair: tuple[str, str]) -> None:
        comp_id, cfg_id = pair
        try:
            jobs = client.list_jobs(
                component_id=comp_id,
                config_id=cfg_id,
                limit=job_limit,
            )
            if jobs:
                with lock:
                    results.append(
                        {
                            "group": {"componentId": comp_id, "configId": cfg_id},
                            "jobs": jobs,
                        }
                    )
        except Exception:
            logger.debug("Failed to fetch jobs for %s/%s", comp_id, cfg_id, exc_info=True)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_fetch_one, pair) for pair in config_pairs]
        for future in as_completed(futures):
            future.result()  # propagate unexpected errors

    return results


def write_per_config_jobs(
    branch_dir: Path,
    configurations: list[ManifestConfiguration],
    jobs_grouped: list[dict[str, Any]],
) -> int:
    """Write ``_jobs.jsonl`` files next to each configuration.

    Matches grouped jobs to configs by componentId+configId, then writes a JSONL
    file with light job records. Returns the number of files written.
    """
    jobs_by_config: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for group in jobs_grouped:
        group_key = group.get("group", {})
        component_id = group_key.get("componentId", "")
        config_id = group_key.get("configId", "")
        if component_id and config_id:
            jobs_by_config[(component_id, config_id)] = group.get("jobs", [])

    files_written = 0
    for cfg in configurations:
        key = (cfg.component_id, cfg.id)
        jobs = jobs_by_config.get(key)
        if not jobs:
            continue

        config_dir = branch_dir / cfg.path
        config_dir.mkdir(parents=True, exist_ok=True)
        jobs_file = config_dir / JOBS_FILENAME

        lines: list[str] = []
        for job in jobs:
            light_job: dict[str, Any] = {
                "id": str(job.get("id", "")),
                "status": job.get("status", ""),
                "start_time": job.get("startTime", ""),
                "end_time": job.get("endTime", ""),
                "duration_seconds": job.get("durationSeconds", 0),
            }
            if job.get("mode") and job["mode"] != "run":
                light_job["mode"] = job["mode"]
            # Include error message for failed/warning jobs
            status = job.get("status", "")
            if status in ("error", "warning", "terminated", "cancelled"):
                result = job.get("result", {})
                if isinstance(result, dict) and result.get("message"):
                    light_job["error_message"] = result["message"]
            lines.append(json.dumps(light_job, ensure_ascii=False))

        jobs_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        files_written += 1

    return files_written


def fetch_samples(
    client: Any,
    tables: list[dict[str, Any]],
    sample_limit: int,
    max_samples: int,
) -> dict[str, str]:
    """Fetch CSV data previews for tables, respecting limits.

    Selects tables sorted by rowsCount descending (largest first), limited to
    ``max_samples`` tables. Returns a dict mapping table_id -> CSV string.
    """

    # Storage API may return rowsCount=None for empty/newly-created tables on
    # some backends; dict.get() does not coerce None to the default value.
    def _rows(t: dict[str, Any]) -> int:
        return t.get("rowsCount") or 0

    sorted_tables = sorted(
        [t for t in tables if _rows(t) > 0],
        key=_rows,
        reverse=True,
    )[:max_samples]

    samples: dict[str, str] = {}
    for t in sorted_tables:
        table_id = t.get("id", "")
        if not table_id:
            continue
        try:
            # Limit columns to avoid the API 400 error on wide tables.
            all_columns = t.get("columns", [])
            columns = (
                all_columns[:_MAX_SYNC_COLUMNS] if len(all_columns) > _MAX_SYNC_COLUMNS else None
            )
            csv_data = client.get_table_data_preview(table_id, limit=sample_limit, columns=columns)
            samples[table_id] = csv_data
        except Exception:
            logger.warning("Failed to fetch sample for %s", table_id, exc_info=True)

    return samples


def mask_encrypted_columns(csv_data: str) -> str:
    """Mask encrypted column values in CSV data.

    Encrypted columns in Keboola start with '#' in the column name; their values
    are replaced with the masked placeholder.
    """
    if not csv_data:
        return csv_data

    lines = csv_data.split("\n")
    if not lines:
        return csv_data

    reader = csv.reader(io.StringIO(lines[0]))
    try:
        header = next(reader)
    except StopIteration:
        return csv_data

    encrypted_indices = [
        i for i, col in enumerate(header) if col.startswith(ENCRYPTED_COLUMN_PREFIX)
    ]
    if not encrypted_indices:
        return csv_data

    output = io.StringIO()
    writer = csv.writer(output)
    full_reader = csv.reader(io.StringIO(csv_data))
    for row_idx, row in enumerate(full_reader):
        if row_idx == 0:
            writer.writerow(row)  # header unchanged
        else:
            for idx in encrypted_indices:
                if idx < len(row):
                    row[idx] = ENCRYPTED_COLUMN_MASK
            writer.writerow(row)

    return output.getvalue()


_LEGACY_EXPORT_ERROR = (
    "storage/buckets.json comes from an older kbagent pull and does not record linked "
    "buckets. No bucket was created. Pull the reference again, or use --no-create-buckets."
)


@dataclass
class BucketCreateResult:
    """Outcome of re-creating a reference tree's buckets in a clone target.

    ``created`` / ``skipped`` hold the target bucket ids; ``errors`` collects a
    ``{"bucket_id", "error"}`` record per bucket the API refused, so one bad
    bucket never aborts the clone. ``linked`` holds a
    ``{"bucket_id", "source_bucket_id", "source_project_id"}`` record per linked
    (shared) bucket that was linked in the target to the reference's source.
    """

    created: list[str]
    skipped: list[str]
    errors: list[dict[str, str]]
    linked: list[dict[str, Any]]


def _parse_bucket_id(bucket_id: str) -> tuple[str, str] | None:
    """Split a ``<stage>.c-<name>`` bucket id into ``(stage, name)``.

    Returns ``None`` for anything ``create-bucket`` cannot recreate: an id with
    no ``in`` / ``out`` stage prefix (a system bucket) or an empty name.
    """
    stage, _, remainder = bucket_id.partition(".")
    if stage not in ("in", "out") or not remainder:
        return None
    name = remainder.removeprefix("c-")
    return (stage, name) if name else None


def create_buckets_from_export(
    storage_client: KeboolaClient,
    project_root: Path,
    bucket_map: dict[str, str],
) -> BucketCreateResult:
    """Create a clone's reference buckets in the target project.

    ``sync clone`` copies configs, not storage: the pulled ``storage/`` tree is
    a read-only snapshot, so a cloned config's input/output mappings point at
    buckets that do not exist in a fresh target. This reads the
    ``storage/buckets.json`` export, maps each bucket id through ``bucket_map``
    (so a created bucket matches what :func:`sync.clone.apply_bucket_map`
    rewrote the configs to reference), and creates the ones that are missing.

    Idempotent: an existing bucket is skipped, and a per-bucket API failure is
    collected in the result, never raised. Each bucket is created on the
    backend the export recorded. A linked bucket is linked to the same source
    as in the reference instead: nothing in the project writes into a linked
    bucket, so an empty bucket in its place would stay empty. The source
    project's sharing settings decide whether the target may link it; a
    refused link is collected like any other failure. An export from an older
    pull (no ``source_bucket`` key) creates nothing and records one error
    instead. Only the buckets are
    created -- their tables and data are not in the export, so they are not
    recreated.
    """
    result = BucketCreateResult(created=[], skipped=[], errors=[], linked=[])
    buckets_file = project_root / STORAGE_DIR_NAME / STORAGE_BUCKETS_FILENAME
    if not buckets_file.exists():
        return result
    try:
        records = json.loads(buckets_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read %s; skipping bucket creation", buckets_file, exc_info=True)
        return result
    if not isinstance(records, list):
        return result
    # An older pull wrote no ``source_bucket`` key, so its linked buckets cannot
    # be told apart. Creating them empty cannot be undone by a re-clone (the
    # existing bucket is skipped), so create nothing.
    if any(isinstance(r, dict) and "source_bucket" not in r for r in records):
        result.errors.append({"bucket_id": "", "error": _LEGACY_EXPORT_ERROR})
        return result

    try:
        existing = {str(b.get("id")) for b in storage_client.list_buckets()}
    except KeboolaApiError as exc:
        result.errors.append({"bucket_id": "", "error": f"Cannot list buckets: {exc.message}"})
        return result
    for record in records:
        if not isinstance(record, dict):
            continue
        source_id = str(record.get("id") or "")
        target_id = bucket_map.get(source_id, source_id)
        parsed = _parse_bucket_id(target_id)
        if parsed is None:
            continue
        if target_id in existing:
            result.skipped.append(target_id)
            continue
        stage, name = parsed
        source = record.get("source_bucket")
        link: dict[str, Any] | None = (
            {
                "bucket_id": target_id,
                "source_bucket_id": source.get("bucket_id", ""),
                "source_project_id": source.get("project_id"),
            }
            if isinstance(source, dict)
            else None
        )
        try:
            if link:
                storage_client.link_bucket(
                    source_project_id=link["source_project_id"],
                    source_bucket_id=link["source_bucket_id"],
                    name=name,
                    stage=stage,
                )
            else:
                storage_client.create_bucket(
                    stage=stage,
                    name=name,
                    description=record.get("description") or "",
                    backend=record.get("backend") or None,
                )
        except KeboolaApiError as exc:
            result.errors.append({"bucket_id": target_id, "error": exc.message})
            continue
        if link:
            result.linked.append(link)
        else:
            result.created.append(target_id)
        existing.add(target_id)
    return result
