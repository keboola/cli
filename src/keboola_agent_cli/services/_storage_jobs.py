"""Storage import jobs: result shaping, wait-budget validation, error hints.

Lives outside ``storage_service.py`` because that module is over its
file-size budget (CONTRIBUTING.md > "File-size budgets"): everything here is
pure -- no client, no config -- so it is what moves out.

Background (issue #834): a Storage table import is an async job. For a
multi-gigabyte file the job can outlive any sensible local wait, and giving
up locally does NOT stop it. These helpers keep the job and file IDs on every
result and every error, so a caller can follow the job (``storage
job-detail``) or re-import the already-uploaded file (``storage load-file``)
instead of uploading it again.
"""

from __future__ import annotations

import csv
import gzip
import math
from typing import Any

from ..errors import ErrorCode, KeboolaApiError

# The first two bytes of every gzip stream (RFC 1952). Sniffed instead of
# trusting the file extension: a `.csv` that is really gzip, or a `.gz` that
# is really plain text, must both read correctly.
_GZIP_MAGIC = b"\x1f\x8b"


def validate_wait_timeout(timeout: float | None) -> None:
    """Reject a wait budget that is not a positive finite number of seconds.

    Raises:
        ValueError: ``timeout`` is zero, negative, NaN or infinite.
    """
    if timeout is None:
        return
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"--timeout must be a positive number of seconds, got {timeout}.")


def import_job_fields(job: dict[str, Any]) -> dict[str, Any]:
    """The keys an import result adds for its job (upload-table, load-file).

    ``imported_rows`` stays None until the job succeeded -- a queued job has
    imported nothing yet, and reporting 0 would read as an empty import.
    """
    status = job.get("status")
    results = job.get("results") if status == "success" else None
    results = results if isinstance(results, dict) else {}
    return {
        "job_id": job.get("id"),
        "job_status": status,
        "imported_rows": results.get("importedRowsCount"),
        "warnings": results.get("warnings", []),
    }


def _job_file_id(job: dict[str, Any]) -> Any:
    """The source file of an import/create job, if the job names one.

    ``operationParams.source.fileId`` is where Storage records the
    ``dataFileId`` an import was started with (the Keboola UI's job detail
    reads the same path). Other job types have no file -> None.
    """
    params = job.get("operationParams")
    source = params.get("source") if isinstance(params, dict) else None
    return source.get("fileId") if isinstance(source, dict) else None


def _job_error(job: dict[str, Any]) -> dict[str, Any] | None:
    """``{message, code}`` of a failed job, None when the job did not fail."""
    if job.get("status") != "error":
        return None
    error = job.get("error")
    if isinstance(error, dict):
        return {"message": error.get("message"), "code": error.get("code")}
    # Tolerate a bare string (see client._core._storage_job_error_message).
    return {"message": error if isinstance(error, str) else None, "code": None}


def summarize_storage_job(alias: str, job: dict[str, Any]) -> dict[str, Any]:
    """Map a raw Storage job onto the ``storage job-detail`` output shape."""
    results = job.get("results")
    results = results if isinstance(results, dict) else {}
    return {
        "project_alias": alias,
        "job_id": job.get("id"),
        "status": job.get("status"),
        "operation_name": job.get("operationName"),
        "table_id": job.get("tableId"),
        "file_id": _job_file_id(job),
        "created_time": job.get("createdTime"),
        "start_time": job.get("startTime"),
        "end_time": job.get("endTime"),
        "imported_rows": results.get("importedRowsCount"),
        "warnings": results.get("warnings", []),
        "results": job.get("results"),
        "error": _job_error(job),
    }


def with_import_hint(exc: KeboolaApiError, alias: str, table_id: str) -> KeboolaApiError:
    """Append the follow-up kbagent command to an import failure.

    The client cannot name it (it knows no project alias). Two cases:

    - wait timeout (``STORAGE_JOB_TIMEOUT`` with a ``job_id``): the import is
      still running -- follow it with ``storage job-detail --wait``;
    - enqueue failure after the upload (a ``file_id`` but no ``job_id``):
      the file is in Storage -- import it with ``storage load-file``.

    Anything else comes back as an equal copy, so a caller can always
    ``raise with_import_hint(exc, ...) from exc``.
    """
    job_id = exc.details.get("job_id")
    file_id = exc.details.get("file_id")
    if exc.error_code == ErrorCode.STORAGE_JOB_TIMEOUT and job_id is not None:
        hint = (
            f" Follow it with: kbagent storage job-detail --project {alias} "
            f"--job-id {job_id} --wait"
        )
    elif file_id is not None and job_id is None:
        hint = (
            f" Import it with: kbagent storage load-file --project {alias} "
            f"--file-id {file_id} --table-id {table_id}"
        )
    else:
        hint = ""
    return KeboolaApiError(
        message=exc.message + hint,
        status_code=exc.status_code,
        error_code=exc.error_code,
        retryable=exc.retryable,
        details=exc.details,
    )


def read_csv_header(file_path: str, delimiter: str = ",", enclosure: str = '"') -> list[str]:
    """Return column names from the first row of a CSV or gzipped CSV file.

    Gzip is detected by its magic bytes, so ``.csv.gz`` uploads auto-create
    their table like plain CSVs do. Strips leading/trailing whitespace and
    skips empty fields. Handles a UTF-8 BOM (utf-8-sig). Only the first row
    is read -- a 200 GB file costs one line, compressed or not.

    Raises:
        ValueError: The first row has no non-empty field, the gzip stream is
            corrupt, or the header is not valid UTF-8.
    """
    with open(file_path, "rb") as fh:
        is_gzip = fh.read(2) == _GZIP_MAGIC
    # An empty enclosure is valid for Storage (no quoting); csv needs it spelled
    # as QUOTE_NONE rather than an empty quotechar.
    dialect: dict[str, Any] = (
        {"delimiter": delimiter, "quotechar": enclosure}
        if enclosure
        else {"delimiter": delimiter, "quoting": csv.QUOTE_NONE}
    )
    try:
        if is_gzip:
            with gzip.open(file_path, "rt", encoding="utf-8-sig", newline="") as fh:
                header = next(csv.reader(fh, **dialect), [])
        else:
            with open(file_path, newline="", encoding="utf-8-sig") as fh:
                header = next(csv.reader(fh, **dialect), [])
    except (OSError, EOFError) as exc:
        # gzip raises BadGzipFile (an OSError) on a bad header and EOFError on
        # a truncated stream.
        what = "corrupt gzip file" if is_gzip else "read error"
        raise ValueError(f"Cannot read the CSV header: {what} ({exc}).") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"Cannot read the CSV header: not valid UTF-8 ({exc}).") from exc
    except (csv.Error, TypeError) as exc:
        # TypeError: a delimiter/enclosure csv rejects (e.g. more than one char).
        raise ValueError(f"Cannot read the CSV header: {exc}.") from exc
    columns = [col.strip() for col in header if col.strip()]
    if not columns:
        raise ValueError("CSV file has no column headers in the first row.")
    return columns
