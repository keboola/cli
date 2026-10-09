"""Local-file helpers for the storage download paths.

Split out of ``storage_service.py`` (past its grandfathered size budget) when
the downloads gained progress reporting: download target containment, the
CSV header/sidecar writers, and the progress callback type.
"""

import csv
from collections.abc import Callable
from pathlib import Path

from ..errors import ErrorCode, KeboolaApiError

# ``(network_bytes_done, total_or_None)`` for a download (client/_transfer.py).
DownloadProgress = Callable[[int, int | None], None]


def safe_download_target(base: Path, server_name: str) -> Path:
    """Contain an API-supplied file name under ``base``.

    The Storage API controls the file ``name``; using it verbatim as a write
    path lets a malicious or compromised response escape the user's chosen
    directory (``../../etc/...`` or an absolute path) and overwrite arbitrary
    files with attacker-controlled bytes. We strip leading separators so an
    absolute name cannot override ``base``, preserve legitimate nested
    subpaths, and assert the resolved path stays within ``base``.
    """
    cleaned = server_name.lstrip("/\\").strip() or "download"
    candidate = (base / cleaned).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise KeboolaApiError(
            message=(
                f"Refusing to write outside the target directory: the "
                f"server-provided file name {server_name!r} escapes {base.resolve()}"
            ),
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
        )
    return candidate


def file_download_target(output_path: str | None, file_name: str) -> str:
    """Local path for ``file-download``: ``output_path`` itself, or ``file_name`` contained.

    A directory ``output_path`` (e.g. the REST file-download endpoint) gets the
    file inside it under its own name. The name comes from the API, so it is
    contained under that directory -- or, without ``output_path``, under CWD --
    so a malicious name (``../../``, absolute) cannot escape. An explicit file
    path is the user's own choice (trusted).
    """
    if output_path and Path(output_path).is_dir():
        return str(safe_download_target(Path(output_path), file_name))
    if output_path:
        return output_path
    return str(safe_download_target(Path.cwd(), file_name))


def write_columns_sidecar(output_dir: str, columns: list[str]) -> None:
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


def prepend_csv_header(file_path: str, columns: list[str]) -> None:
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
