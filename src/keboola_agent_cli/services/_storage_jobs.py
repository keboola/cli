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
import shlex
import zlib
from dataclasses import dataclass
from typing import Any

from ..client import storage_job_error_message
from ..errors import ErrorCode, KeboolaApiError

# The first two bytes of every gzip stream (RFC 1952). Sniffed instead of
# trusting the file extension: a `.csv` that is really gzip, or a `.gz` that
# is really plain text, must both read correctly.
_GZIP_MAGIC = b"\x1f\x8b"

# The `--delimiter` / `--enclosure` defaults of upload-table and load-file.
# A recovery command omits a flag that would only restate its default.
DEFAULT_IMPORT_DELIMITER = ","
DEFAULT_IMPORT_ENCLOSURE = '"'


@dataclass(frozen=True)
class ImportOptions:
    """How an import was started -- what a recovery command must repeat.

    A re-import that drops any of these is a DIFFERENT import: without
    ``incremental`` it is a full load that replaces the table, without
    ``branch_id`` it lands in production (or whatever branch is active when
    the user runs it).
    """

    incremental: bool = False
    delimiter: str = DEFAULT_IMPORT_DELIMITER
    enclosure: str = DEFAULT_IMPORT_ENCLOSURE
    branch_id: int | None = None


def validate_wait_timeout(timeout: float | None) -> None:
    """Reject a wait budget that is not a positive finite number of seconds.

    Raises:
        ValueError: ``timeout`` is zero, negative, NaN or infinite.
    """
    if timeout is None:
        return
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"--timeout must be a positive number of seconds, got {timeout}.")


def resolve_wait_timeout(timeout: float | None, default: float) -> float:
    """Validate a wait budget, then resolve ``None`` to ``default``.

    Branches on ``is None`` -- NOT ``timeout or default``, which would turn
    a falsy-but-invalid ``0.0`` into the default instead of rejecting it.

    Raises:
        ValueError: ``timeout`` is zero, negative, NaN or infinite.
    """
    validate_wait_timeout(timeout)
    return default if timeout is None else timeout


def import_job_fields(job: dict[str, Any], file_id: Any) -> dict[str, Any]:
    """The keys an import result adds for its job (upload-table, load-file).

    ``imported_rows`` stays None until the job succeeded -- a queued job has
    imported nothing yet, and reporting 0 would read as an empty import.

    Raises:
        KeboolaApiError: ``STORAGE_JOB_FAILED`` when ``job`` is already a
            terminal failure. With ``--no-wait`` the enqueue response itself
            can be one (Storage rejected the file fast); reporting it as a
            queued success would exit 0 on a failed import. Same message and
            details as the waited path, so callers see one failure shape.
    """
    status = job.get("status")
    if status == "error":
        raise KeboolaApiError(
            message=storage_job_error_message(job),
            status_code=500,
            error_code=ErrorCode.STORAGE_JOB_FAILED,
            retryable=False,
            details={"job_id": job.get("id"), "file_id": file_id},
        )
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
    # Tolerate a bare string (see client._core.storage_job_error_message).
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


def _branch_args(branch_id: int | None) -> list[str]:
    """Always ``--branch ID``, with ``--branch 0`` for production.

    Spelled out even when it came from ``branch use``: the hint is read later,
    possibly after the active branch changed, and must target the same branch.
    A production import must say so too -- omitting the flag would let a
    since-activated dev branch capture the re-import (a full load there
    replaces the wrong table's contents).
    """
    return ["--branch", str(branch_id or 0)]


def _load_file_command(alias: str, file_id: Any, table_id: str, options: ImportOptions) -> str:
    """The ``storage load-file`` command that repeats this exact import."""
    args = [
        "kbagent",
        "storage",
        "load-file",
        "--project",
        alias,
        "--file-id",
        str(file_id),
        "--table-id",
        table_id,
    ]
    if options.incremental:
        args.append("--incremental")
    if options.delimiter != DEFAULT_IMPORT_DELIMITER:
        args += ["--delimiter", options.delimiter]
    if options.enclosure != DEFAULT_IMPORT_ENCLOSURE:
        args += ["--enclosure", options.enclosure]
    args += _branch_args(options.branch_id)
    return shlex.join(args)


def _table_detail_command(alias: str, table_id: str, branch_id: int | None) -> str:
    args = ["kbagent", "storage", "table-detail", "--project", alias, "--table-id", table_id]
    return shlex.join(args + _branch_args(branch_id))


def _job_detail_command(alias: str, job_id: Any) -> str:
    """The ``storage job-detail --wait`` command that follows a running job."""
    args = ["kbagent", "storage", "job-detail", "--project", alias, "--job-id", str(job_id)]
    return shlex.join([*args, "--wait"])


def _with_hint(exc: KeboolaApiError, hint: str) -> KeboolaApiError:
    """An equal copy of ``exc`` with ``hint`` appended to its message."""
    return KeboolaApiError(
        message=exc.message + hint,
        status_code=exc.status_code,
        error_code=exc.error_code,
        retryable=exc.retryable,
        details=exc.details,
    )


def with_job_timeout_hint(
    exc: KeboolaApiError,
    alias: str,
    table_id: str | None = None,
    branch_id: int | None = None,
) -> KeboolaApiError:
    """Append the follow-up commands to a wait timeout (create-table, swap-tables).

    The client cannot name them (it knows no project alias). A
    ``STORAGE_JOB_TIMEOUT`` with a ``job_id`` gets ``storage job-detail
    --wait``, plus ``storage table-detail`` for ``table_id`` when given.
    Anything else comes back as an equal copy, so a caller can always
    ``raise with_job_timeout_hint(exc, ...) from exc``.
    """
    job_id = exc.details.get("job_id")
    if exc.error_code != ErrorCode.STORAGE_JOB_TIMEOUT or job_id is None:
        return _with_hint(exc, "")
    hint = f" Follow it with: {_job_detail_command(alias, job_id)}"
    if table_id is not None:
        hint += (
            f" -- then verify the table with: {_table_detail_command(alias, table_id, branch_id)}"
        )
    return _with_hint(exc, hint)


def with_import_hint(
    exc: KeboolaApiError, alias: str, table_id: str, options: ImportOptions
) -> KeboolaApiError:
    """Append the follow-up kbagent command to an import failure.

    The client cannot name it (it knows no project alias). Three cases:

    - wait timeout (``STORAGE_JOB_TIMEOUT`` with a ``job_id``): the import is
      still running -- follow it with ``storage job-detail --wait``;
    - AMBIGUOUS enqueue failure (``details["import_may_be_running"]``: the
      POST may have reached Storage, its response was lost): check the table
      first, re-import only if nothing ran -- a blind re-import can load the
      file twice;
    - DEFINITIVE enqueue failure after the upload (a ``file_id`` but no
      ``job_id``): the file is in Storage -- import it with ``storage
      load-file``, repeating every option of the original import.

    Anything else comes back as an equal copy, so a caller can always
    ``raise with_import_hint(exc, ...) from exc``.
    """
    job_id = exc.details.get("job_id")
    file_id = exc.details.get("file_id")
    if exc.error_code == ErrorCode.STORAGE_JOB_TIMEOUT and job_id is not None:
        return with_job_timeout_hint(exc, alias)
    if exc.details.get("import_may_be_running") and job_id is None:
        hint = (
            " The import request may have reached Storage, so the import may already be "
            "running -- do NOT re-import yet. Check the table first: "
            f"{_table_detail_command(alias, table_id, options.branch_id)} "
            "and the recent Storage jobs in the Keboola UI (no job ID was returned)."
        )
        if file_id is not None:
            hint += (
                f" Only if neither shows an import of Storage file {file_id}, re-import it "
                f"with: {_load_file_command(alias, file_id, table_id, options)}"
            )
    elif file_id is not None and job_id is None:
        hint = f" Import it with: {_load_file_command(alias, file_id, table_id, options)}"
    else:
        hint = ""
    return _with_hint(exc, hint)


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
    except (OSError, EOFError, zlib.error) as exc:
        # gzip raises BadGzipFile (an OSError) on a bad header, EOFError on a
        # truncated stream and zlib.error on corrupt DEFLATE data.
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
