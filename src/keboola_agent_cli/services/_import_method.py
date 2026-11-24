"""Pre-flight check of an explicit import method against the table's backend.

Storage accepts ``importMethod`` only on Snowflake tables and refuses any
other value with HTTP 400 ``storage.tables.importMethodNotSupported`` -- but
only on the import POST, after ``upload-table`` has auto-created the bucket
and table and uploaded the whole file. Reading the backend first lets the
command refuse before it writes anything. The Storage refusal stays mapped
(``_storage_jobs.with_import_hint``) for a backend this check cannot read.
"""

from __future__ import annotations

from typing import Protocol

from ..errors import ErrorCode, KeboolaApiError
from ..models import ImportMethod, TokenVerifyResponse

# Storage's backend name for the only backend that accepts an import method.
SNOWFLAKE_BACKEND = "snowflake"


class _BackendReader(Protocol):
    def get_bucket_detail(self, bucket_id: str, branch_id: int | None = None) -> dict: ...

    def verify_token(self) -> TokenVerifyResponse: ...


def refuse_unsupported_import_method(
    client: _BackendReader,
    table_id: str,
    import_method: ImportMethod | str,
    branch_id: int | None,
    *,
    creates_missing_bucket: bool,
) -> ImportMethod:
    """Return ``import_method`` as an enum; refuse it on a non-Snowflake target.

    ``auto`` costs no request. Any other value reads the bucket's backend; a
    missing bucket that the caller will create gets the project's default
    backend (what ``create_bucket`` without a backend uses). A bucket or token
    read that fails (a missing bucket the caller will not create, 403, any
    other API error), or a backend Storage does not report, is left to
    Storage: the method is returned and the request goes on.

    Raises:
        ValueError: ``import_method`` is not an :class:`ImportMethod` value.
        KeboolaApiError: ``INVALID_ARGUMENT`` when the backend is not
            Snowflake; nothing has been written.
    """
    method = ImportMethod(import_method)
    if method == ImportMethod.auto:
        return method
    bucket_id = table_id.rsplit(".", 1)[0]
    try:
        backend = client.get_bucket_detail(bucket_id, branch_id=branch_id).get("backend")
        where = f"table {table_id} is in bucket {bucket_id} on the {{backend}} backend"
    except KeboolaApiError as exc:
        if exc.status_code != 404 or not creates_missing_bucket:
            # An unreadable bucket (403, or 404 the caller will not create)
            # is not this check's error: the request goes on and Storage decides.
            return method
        try:
            backend = client.verify_token().default_backend
        except KeboolaApiError:
            return method
        where = (
            f"bucket {bucket_id} does not exist yet; a new bucket gets the project's "
            "default backend, {backend}"
        )
    backend = backend.lower() if isinstance(backend, str) else ""
    if not backend or backend == SNOWFLAKE_BACKEND:
        return method
    raise KeboolaApiError(
        message=(
            f"Import method '{method.value}' works only on Snowflake tables, and "
            f"{where.format(backend=backend)}. Run the import without --import-method "
            "(import_method over kbagent serve) or with the value 'auto'. Nothing was "
            "created or uploaded."
        ),
        status_code=400,
        error_code=ErrorCode.INVALID_ARGUMENT,
        retryable=False,
        details={"table_id": table_id, "backend": backend, "import_method": method.value},
    )
