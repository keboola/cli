"""Module-level helpers for the Keboola client.

Pure functions and small helper classes extracted verbatim from the former
single-file ``client.py`` (issue #520): inline Query Service result
collection, Queue poll-interval scheduling, cloud upload/download plumbing
(S3 SigV4, GCS bearer, ABS SAS), and query-error parsing. None of these touch
``self`` -- they are free functions/classes shared by the client mixins and
the public library facade.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ..constants import (
    CLOUD_UPLOAD_ERROR_BODY_LIMIT,
    FILE_DOWNLOAD_CHUNK_SIZE,
    FILE_DOWNLOAD_TIMEOUT,
    JOB_POLL_CURVE,
    QUERY_RESULTS_PAGE_SIZE,
    STORAGE_JOB_POLL_INTERVAL,
)
from ..errors import ErrorCode, KeboolaApiError

if TYPE_CHECKING:
    from ._client import KeboolaClient


@dataclass(frozen=True)
class InlineQueryResult:
    """One statement's result fetched via the fast inline ``/results`` path."""

    columns: list[dict[str, Any]]  # [{"name", "type", "nullable"}]
    rows: list[list[Any]]  # row values, row-major; capped at the requested limit
    total_rows: int | None  # numberOfRows reported by the warehouse (full count)
    truncated: bool  # True when the warehouse has more rows than we fetched


def _collect_inline_results(
    client: "KeboolaClient",
    query_job_id: str,
    statement_id: str,
    limit: int,
) -> InlineQueryResult:
    """Page through ``GET .../results``, accumulating up to ``limit`` rows.

    The endpoint enforces ``100 <= pageSize <= 100000``, so we always request a
    fixed, valid ``QUERY_RESULTS_PAGE_SIZE`` page and cap the accumulated rows at
    ``limit`` locally -- deriving ``pageSize`` from a small ``limit`` (e.g. 5)
    would trip the API's minimum with a 400. A ``limit`` larger than one page is
    satisfied by walking ``offset``; we stop once the limit is reached (marking
    the result truncated) or when the warehouse runs out of rows.

    Lives in the client layer (not a service) because it is pure Query Service
    pagination over :meth:`KeboolaClient.get_query_results` -- no config, no
    business logic -- so both ``WorkspaceService`` and the public library facade
    (:mod:`keboola_agent_cli.lib`) can share it.
    """
    collected: list[list[Any]] = []
    columns: list[dict[str, Any]] = []
    total_rows: int | None = None
    offset = 0
    exhausted = False
    while len(collected) < limit:
        payload = client.get_query_results(
            query_job_id, statement_id, offset=offset, page_size=QUERY_RESULTS_PAGE_SIZE
        )
        if not columns:
            columns = payload.get("columns", []) or []
        if total_rows is None:
            total_rows = payload.get("numberOfRows")
        page_rows = payload.get("data", []) or []
        collected.extend(page_rows)
        # Last page: the warehouse returned fewer rows than a full page.
        if len(page_rows) < QUERY_RESULTS_PAGE_SIZE:
            exhausted = True
            break
        offset += len(page_rows)
        # Reached the reported total on a page boundary: stop without spending a
        # round-trip on the empty next page (e.g. total == a multiple of the
        # page size, limit larger than total).
        if total_rows is not None and offset >= total_rows:
            exhausted = True
            break

    rows = collected[:limit]
    if total_rows is not None:
        truncated = total_rows > len(rows)
    else:
        # The Query Service normally reports numberOfRows, but if it omits the
        # count we fall back to *how* the loop ended: stopping at the limit cap
        # without exhausting a full last page means there may be more rows. Bias
        # toward over-warning when the true count is unknown.
        truncated = not exhausted and len(collected) >= limit
    return InlineQueryResult(
        columns=columns,
        rows=rows,
        total_rows=total_rows,
        truncated=truncated,
    )


def _iter_poll_intervals(strategy: str) -> Iterator[float]:
    """Yield sleep intervals (seconds) for Queue job polling.

    Two strategies:

    - ``"exponential"`` walks ``JOB_POLL_CURVE``: each (interval, count)
      segment yields ``count`` copies of ``interval``; a segment with
      ``count == 0`` keeps yielding ``interval`` forever (valid only on
      the last segment).
    - ``"fixed"`` yields ``STORAGE_JOB_POLL_INTERVAL`` forever (legacy
      behavior preserved for opt-out via ``--poll-strategy fixed``).

    The deadline check in ``wait_for_queue_job`` stops iteration.
    """
    if strategy == "fixed":
        while True:
            yield STORAGE_JOB_POLL_INTERVAL
    for interval, count in JOB_POLL_CURVE:
        if count <= 0:
            while True:
                yield interval
        for _ in range(count):
            yield interval


# The Query Service surfaces BigQuery errors as a serialized object string, e.g.
#   {Location: "query"; Message: "Syntax error: Unexpected identifier ..."; Reason: "invalidQuery"}
# Pull out the human-readable `Message: "..."` part so a BigQuery failure reads
# like Snowflake's plain text instead of leaking the wrapper into the user's red
# error box. Mirrors keboola-mcp-server's `_BigQueryWorkspace._format_error_message`.
_BQ_ERROR_MESSAGE_RE = re.compile(r'Message:\s*"((?:[^"\\]|\\.)*)"')


def _unwrap_bigquery_error(message: str) -> str:
    """Extract the inner message from a serialized BigQuery Query-Service error.

    Snowflake errors are plain strings with no ``Message: "..."`` wrapper, so
    they pass through unchanged. Only the BigQuery object shape is rewritten.
    """
    if message and (match := _BQ_ERROR_MESSAGE_RE.search(message)):
        return match.group(1).replace('\\"', '"')
    return message


def _extract_query_job_error(job: dict[str, Any]) -> str:
    """Pull the most useful warehouse error message out of a failed Query Service job.

    The Query Service `/api/v1/queries/{id}` response for a failed job carries
    the actual Snowflake / BigQuery error inside ``statements[i].error`` as a
    plain string (e.g. "SQL compilation error:\\nFunction DATE_TRUNC does not
    support VARCHAR(10) argument type"). The top-level ``error`` field is
    usually ABSENT on failures — the previous extractor read only that and so
    emitted the useless "Query job failed: Query execution failed" message
    users were seeing in the SQL editor's red error box (#287).

    Strategy:
    1. Walk ``statements`` and collect every failed statement's error,
       prefixed with the statement index so multi-statement batches stay
       readable. Strings, dicts ({\"message\": "..."}), and unknown shapes
       are all handled.
    2. Fall back to top-level ``error`` (string OR dict-with-message) for
       legacy shapes that don't carry statement-level errors.
    3. Fall back to the original generic string only when neither is set,
       so the caller never sees an empty message.

    The returned string is meant to be embedded into a
    ``KeboolaApiError(message=f"Query job failed: ...")`` and ultimately
    surfaced to the user (and the AI fix-mode helper, which pivots its
    meta-prompt on the warehouse text).
    """

    def _as_text(err: Any) -> str:
        if isinstance(err, str):
            raw = err.strip()
        elif isinstance(err, dict):
            raw = ""
            for key in ("message", "error", "detail"):
                val = err.get(key)
                if isinstance(val, str) and val.strip():
                    raw = val.strip()
                    break
        else:
            raw = str(err).strip() if err is not None else ""
        # BigQuery wraps the real message in a serialized object; Snowflake plain
        # text passes through untouched.
        return _unwrap_bigquery_error(raw)

    statement_errors: list[str] = []
    for i, stmt in enumerate(job.get("statements") or []):
        if not isinstance(stmt, dict):
            continue
        if stmt.get("status") not in ("error", "failed"):
            continue
        text = _as_text(stmt.get("error"))
        if not text:
            continue
        # Single-statement queries don't need the "Statement 1:" prefix —
        # it adds visual noise in the editor's red box for the common case.
        prefix = "" if len(job.get("statements") or []) == 1 else f"Statement {i + 1}: "
        statement_errors.append(f"{prefix}{text}")

    if statement_errors:
        return "\n".join(statement_errors)

    top_level = _as_text(job.get("error"))
    if top_level:
        return top_level

    return "Query execution failed (no error details from Query Service)"


# ---------------------------------------------------------------------------
# Cloud storage upload helpers
# ---------------------------------------------------------------------------


def _build_abs_upload_url(abs_params: dict[str, Any]) -> str:
    """Build Azure Blob Storage upload URL from absUploadParams.

    Parses SASConnectionString to extract BlobEndpoint and SharedAccessSignature,
    then constructs: ``{BlobEndpoint}/{container}/{blobName}?{SAS}``.

    The ``url`` field in the API response is read-only (``sp=rl``).
    The write-capable SAS (``sp=rwl``) is only in ``absUploadParams``.

    Args:
        abs_params: The absUploadParams dict from files/prepare response.

    Returns:
        Full HTTPS URL with write-capable SAS token.
    """
    blob_name = abs_params["blobName"]
    container = abs_params["container"]
    sas_string = abs_params["absCredentials"]["SASConnectionString"]

    # Format: "BlobEndpoint=https://...;SharedAccessSignature=sv=2017-11-09&..."
    # partition("=") splits on first "=" only, preserving "=" in SAS values.
    parts: dict[str, str] = {}
    for segment in sas_string.split(";"):
        key, sep, value = segment.partition("=")
        if sep:
            parts[key] = value

    blob_endpoint = parts.get("BlobEndpoint", "").rstrip("/")
    sas = parts.get("SharedAccessSignature", "")

    return f"{blob_endpoint}/{container}/{blob_name}?{sas}"


# Strict charset: provider error codes are short alphanumeric tokens (GCS/S3
# "AccessDenied", Azure "AuthorizationFailure"). Anything else in the response
# -- signed URLs, credentials, free-form messages -- must never reach the
# user-facing error string.
_CLOUD_ERROR_CODE_RE = re.compile(r"<Code>([A-Za-z0-9._-]{1,64})</Code>")


def _extract_cloud_error_code(response: httpx.Response) -> str | None:
    """Best-effort short error code from a failed cloud-storage response.

    Azure surfaces it in the ``x-ms-error-code`` header; GCS and S3 return an
    XML body with a ``<Code>`` element. Returns ``None`` when neither matches.
    """
    header_code = response.headers.get("x-ms-error-code", "")
    if header_code and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", header_code):
        return header_code
    match = _CLOUD_ERROR_CODE_RE.search(response.text[:CLOUD_UPLOAD_ERROR_BODY_LIMIT])
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Cloud storage download helpers (S3 SigV4, GCS bearer, ABS signed URL)
# ---------------------------------------------------------------------------


def _assert_safe_download_url(url: str) -> None:
    """Reject a download URL whose host resolves to a non-public address.

    A malicious or compromised Storage API response can return a download URL
    pointing at the cloud instance-metadata endpoint (169.254.169.254) or
    localhost; because these fetches carry no Storage token and don't follow
    redirects, the residual SSRF still writes internal/credential data into the
    user's download file (GHSA-hjhx-mx7m-8xx2). We resolve the host and allow
    ONLY globally-routable (public) addresses plus the explicit BYOC private
    ranges below; everything else -- loopback, link-local (incl. the
    169.254.169.254 metadata endpoint), CGNAT 100.64.0.0/10, reserved,
    multicast, unspecified -- is refused.

    RFC1918 + IPv6-ULA *private* ranges are deliberately ALLOWED: BYOC /
    private-tenant Keboola deployments legitimately serve storage from private
    endpoints, and the high-value SSRF target (instance metadata) is link-local,
    not private. An allow-list (public OR explicit private) rather than a
    block-list of `ipaddress` predicates avoids gaps like CGNAT, which none of
    `is_loopback/is_link_local/is_reserved/is_multicast/is_unspecified` catch.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    byoc_private = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    )

    host = urlparse(url).hostname
    if not host:
        raise KeboolaApiError(
            message=f"Refusing to download: URL has no host ({url!r}).",
            status_code=0,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
        )
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        # DNS failure is surfaced by the real fetch; don't mask it here.
        return
    for addr in resolved:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_global or any(ip in net for net in byoc_private):
            continue  # public host, or a BYOC private endpoint -- allowed
        raise KeboolaApiError(
            message=(
                f"Refusing to download from {host} -> {addr}: non-public address "
                f"(not a public host nor a BYOC private range). This indicates a "
                f"malicious or compromised Storage API response (possible SSRF, "
                f"e.g. the cloud instance-metadata endpoint)."
            ),
            status_code=0,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
        )


class _IterBytesReader:
    """Adapt an httpx iter_bytes() iterator to a .read(n) file-like interface.

    shutil.copyfileobj and gzip.GzipFile both need a binary stream with
    read(size). httpx exposes an iterator instead, so we buffer the current
    chunk and hand out at most ``size`` bytes per read, refilling from the
    iterator as needed. The buffer holds at most one iterator chunk at a time
    (~1 MiB), so total memory stays bounded regardless of response size.
    """

    def __init__(self, chunks: Any) -> None:
        self._chunks = iter(chunks)
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            pieces = [self._buf]
            self._buf = b""
            pieces.extend(self._chunks)
            return b"".join(pieces)
        while len(self._buf) < size:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        out = self._buf[:size]
        self._buf = self._buf[size:]
        return out


class _CloudDownloader:
    """Abstraction for downloading from cloud storage using Keboola file credentials.

    Supports three cloud backends:
    - AWS S3: Uses SigV4 signing with temporary credentials
    - GCP GCS: Uses OAuth2 bearer token
    - Azure ABS: Uses presigned/SAS URLs
    """

    def __init__(self, provider: str, auth_fn: Any) -> None:
        self._provider = provider
        self._auth_fn = auth_fn

    @staticmethod
    def create(file_detail: dict[str, Any]) -> "_CloudDownloader":
        """Create a downloader from file detail response.

        Args:
            file_detail: Response from GET /v2/storage/files/{id}?federationToken=1.
        """
        provider = file_detail.get("provider", "")

        if provider == "aws":
            creds = file_detail.get("credentials", {})
            region = file_detail.get("region", "us-east-1")
            return _CloudDownloader(
                provider="aws",
                auth_fn=lambda url: _s3_signed_headers(url, creds, region),
            )
        elif provider == "gcp":
            gcs_creds = file_detail.get("gcsCredentials", {})
            token = gcs_creds.get("access_token", "")
            token_type = gcs_creds.get("token_type", "Bearer")
            return _CloudDownloader(
                provider="gcp",
                auth_fn=lambda _url: {"Authorization": f"{token_type} {token}"},
            )
        elif provider == "azure":
            # Azure: SAS token from absCredentials for authenticating slice downloads
            abs_creds = file_detail.get("absCredentials", {})
            sas_string = abs_creds.get("SASConnectionString", "")
            # Parse "BlobEndpoint=https://...;SharedAccessSignature=sv=..."
            sas_parts: dict[str, str] = {}
            for segment in sas_string.split(";"):
                key, sep, value = segment.partition("=")
                if sep:
                    sas_parts[key] = value
            blob_endpoint = sas_parts.get("BlobEndpoint", "").rstrip("/")
            sas = sas_parts.get("SharedAccessSignature", "")
            return _CloudDownloader(
                provider="azure",
                auth_fn=lambda _url, _be=blob_endpoint, _sas=sas: {
                    "_blob_endpoint": _be,
                    "_sas": _sas,
                },
            )
        else:
            # Other: presigned URLs, no extra auth needed
            return _CloudDownloader(provider=provider, auth_fn=lambda _url: {})

    def resolve_base_url(self, file_detail: dict[str, Any]) -> str:
        """Build the HTTPS base URL for downloading slices.

        Returns:
            Base HTTPS URL (e.g. "https://bucket.s3.region.amazonaws.com/key/prefix/").
        """
        if self._provider == "aws":
            s3_path = file_detail.get("s3Path", {})
            bucket = s3_path.get("bucket", "")
            key = s3_path.get("key", "")
            region = file_detail.get("region", "us-east-1")
            return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
        elif self._provider == "gcp":
            gcs_path = file_detail.get("gcsPath", {})
            bucket = gcs_path.get("bucket", "")
            key = gcs_path.get("key", "")
            return f"https://storage.googleapis.com/{bucket}/{key}"
        elif self._provider == "azure":
            # Azure: base URL from absCredentials endpoint + container
            auth_info = self._auth_fn("")
            blob_endpoint = auth_info.get("_blob_endpoint", "")
            abs_path = file_detail.get("absPath", {})
            container = abs_path.get("container", "")
            return f"{blob_endpoint}/{container}/"
        else:
            # Other: entries should be full URLs
            return ""

    def resolve_slice_url(
        self,
        base_url: str,
        entry_url: str,
        file_detail: dict[str, Any],
    ) -> str:
        """Convert a manifest entry URL to a downloadable HTTPS URL.

        Manifest entries use cloud-native URLs (s3://bucket/key/slice.gz,
        azure://container/blob). This strips the cloud prefix and builds
        an HTTPS URL for download.

        Args:
            base_url: HTTPS base URL from resolve_base_url().
            entry_url: Raw entry URL from manifest (e.g. "s3://bucket/key/slice.gz").
            file_detail: Full file detail dict.

        Returns:
            Full HTTPS URL for the slice.
        """
        if self._provider == "aws":
            # entry_url: "s3://bucket/key/prefix/slice.csv.gz"
            # base_url: "https://bucket.s3.region.amazonaws.com/key/prefix/"
            s3_path = file_detail.get("s3Path", {})
            bucket = s3_path.get("bucket", "")
            key = s3_path.get("key", "")
            prefix = f"s3://{bucket}/{key}"
            relative = entry_url.removeprefix(prefix) if entry_url.startswith(prefix) else entry_url
            return base_url + relative
        elif self._provider == "gcp":
            # entry_url: "gs://bucket/key/prefix/slice.csv.gz"
            gcs_path = file_detail.get("gcsPath", {})
            bucket = gcs_path.get("bucket", "")
            key = gcs_path.get("key", "")
            prefix = f"gs://{bucket}/{key}"
            relative = entry_url.removeprefix(prefix) if entry_url.startswith(prefix) else entry_url
            return base_url + relative
        elif self._provider == "azure":
            # entry_url: "azure://account.blob.core.windows.net/container/blob.gz"
            # Replace azure:// with https:// and append SAS token
            auth_info = self._auth_fn("")
            sas = auth_info.get("_sas", "")
            if entry_url.startswith("azure://"):
                https_url = "https://" + entry_url[len("azure://") :]
                return f"{https_url}?{sas}"
            return entry_url
        else:
            # Other: entry URLs should be full HTTPS URLs
            return entry_url

    def _request_headers(self, url: str) -> dict[str, str]:
        """Resolve auth headers for a cloud URL.

        Azure stores metadata (endpoint, SAS) in the auth_fn result (keys
        prefixed with "_"); those are filtered out here. The SAS token itself
        is embedded into the URL by resolve_slice_url().
        """
        auth_result = self._auth_fn(url)
        return {k: v for k, v in auth_result.items() if not k.startswith("_")}

    def stream_to_file(self, url: str, dest: "Path | str", decompress_gzip: bool) -> int:
        """Stream a cloud URL directly to a local file in bounded-memory chunks.

        Used for slice downloads where the payload can be hundreds of MB per
        slice. Peak RAM is O(chunk size), not O(slice size), which is what
        makes multi-GB table exports survive on small VMs (see issue #187).

        Args:
            url: Full HTTPS URL (with auth baked in for Azure).
            dest: Local file path to write to.
            decompress_gzip: If True, wrap the response stream in gzip.GzipFile
                so the decompressed bytes are what lands on disk. Streaming
                gzip keeps both compressed and decompressed state bounded.

        Returns:
            Number of bytes written to ``dest`` (post-decompression if applicable).
        """
        import gzip
        import shutil

        _assert_safe_download_url(url)
        headers = self._request_headers(url)
        dest_path = Path(dest)
        with (
            httpx.Client(timeout=FILE_DOWNLOAD_TIMEOUT) as http,
            http.stream("GET", url, headers=headers) as response,
        ):
            response.raise_for_status()
            source: Any = _IterBytesReader(response.iter_bytes(FILE_DOWNLOAD_CHUNK_SIZE))
            if decompress_gzip:
                source = gzip.GzipFile(fileobj=source, mode="rb")
            with dest_path.open("wb") as fh:
                shutil.copyfileobj(source, fh, length=FILE_DOWNLOAD_CHUNK_SIZE)

        return dest_path.stat().st_size


def _s3_signed_headers(
    url: str,
    creds: dict[str, str],
    region: str,
    method: str = "GET",
    payload: bytes = b"",
) -> dict[str, str]:
    """Generate AWS SigV4 signed headers for an S3 request.

    Implements minimal AWS Signature Version 4 signing using only stdlib
    (hmac, hashlib, urllib.parse). No boto3/botocore dependency required.

    Args:
        url: Full S3 URL (https://bucket.s3.region.amazonaws.com/key).
        creds: Dict with AccessKeyId, SecretAccessKey, SessionToken.
        region: AWS region (e.g. "us-east-1").
        method: HTTP method (GET or PUT).
        payload: Request body bytes (empty for GET).

    Returns:
        Dict of headers to include in the request.
    """
    import datetime
    import hashlib
    import hmac
    from urllib.parse import unquote, urlparse

    access_key = creds["AccessKeyId"]
    secret_key = creds["SecretAccessKey"]
    session_token = creds.get("SessionToken", "")

    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    query = parsed.query or ""

    now = datetime.datetime.now(datetime.UTC)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")

    service = "s3"
    scope = f"{date_stamp}/{region}/{service}/aws4_request"

    # Canonical request
    canonical_uri = quote(unquote(path), safe="/~")
    if query:
        params_list = sorted(query.split("&"))
        canonical_querystring = "&".join(params_list)
    else:
        canonical_querystring = ""

    headers_to_sign: dict[str, str] = {"host": host, "x-amz-date": amz_date}
    if session_token:
        headers_to_sign["x-amz-security-token"] = session_token

    signed_headers = ";".join(sorted(headers_to_sign.keys()))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))

    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

    # String to sign
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    # Signing key
    def _hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac_sha256(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")

    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    result: dict[str, str] = {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        result["x-amz-security-token"] = session_token
    return result
