"""S3 multipart upload for AWS federation-token file uploads.

A single S3 PutObject is capped at 5 GiB, and the single-PUT path holds the
whole file in memory, so ``KeboolaClient._upload_to_cloud`` hands anything
above ``S3_MULTIPART_THRESHOLD`` to :func:`upload_s3_multipart` instead:
CreateMultipartUpload -> UploadPart x N (bounded parallelism) ->
CompleteMultipartUpload, all signed with the stdlib SigV4 signer in
``_transfer.py`` using the temporary credentials from ``files/prepare``.

What the federation token allows decides several choices here. The write
policy grants only ``s3:PutObject`` / ``s3:PutObjectAcl`` on the prepared key:
that authorizes Create, UploadPart and Complete, but NOT AbortMultipartUpload
(so abort is best-effort and a 403 is expected) and NOT HeadObject (so an
ambiguous completion is verified with the READ credentials of
``GET /v2/storage/files/{id}?federationToken=1`` -- see ``head_s3_object_size``).

Known, accepted orphan case: a Create whose response is lost in transit is
retried, and the first (unseen) upload id is left behind. Incomplete uploads
are invisible, are never billed to the customer's project, and are reaped by
the bucket's lifecycle rules.

Nothing in this module logs credentials, the session token, signed headers or
the Authorization value. Provider error bodies go to the DEBUG log only,
truncated; raised messages carry only the whitelisted provider error code.
"""

import datetime
import gc
import hashlib
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..constants import (
    BACKOFF_BASE,
    CLOUD_UPLOAD_ERROR_BODY_LIMIT,
    FILE_UPLOAD_TIMEOUT,
    MAX_RETRIES,
    MAX_RETRY_AFTER_SECONDS,
    RETRYABLE_STATUS_CODES,
    S3_MULTIPART_CONCURRENCY,
    S3_MULTIPART_MAX_PART_SIZE,
    S3_MULTIPART_MAX_PARTS,
    S3_MULTIPART_MIN_PART_SIZE,
    S3_MULTIPART_PART_ALIGNMENT,
    S3_MULTIPART_PART_ATTEMPTS,
    S3_MULTIPART_PART_SIZE,
)
from ..errors import ErrorCode, KeboolaApiError
from ._transfer import _CLOUD_ERROR_CODE_RE, _extract_cloud_error_code, _s3_signed_headers

logger = logging.getLogger(__name__)

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

# S3 error codes that are transient even when the HTTP status is not a 5xx
# (RequestTimeout is a 400) or the status is a 200 with an <Error> body
# (CompleteMultipartUpload streams keepalive whitespace, then may fail).
_RETRYABLE_S3_CODES = frozenset(
    {"RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable"}
)

# Synthetic code for a 2xx reply whose body is not the document we expected
# (e.g. a Complete cut off mid-stream). Treated as transient.
_MALFORMED_REPLY = "MalformedResponse"

# uploadParams keys forwarded as signed request headers on the object-creating
# calls (single PUT, CreateMultipartUpload), mapped to their S3 header name.
# Anything else in uploadParams (key, bucket, credentials) is not a header.
_UPLOAD_HEADER_ALLOWLIST = {
    "acl": "x-amz-acl",
    "x-amz-server-side-encryption": "x-amz-server-side-encryption",
}

ProgressCallback = Callable[[int, int], None]
VerifyCallback = Callable[[int], bool]


@dataclass(frozen=True)
class S3Target:
    """Where and as whom to upload: one object key plus its write credentials."""

    bucket: str
    key: str
    region: str
    credentials: dict[str, str]
    upload_headers: dict[str, str]  # allowlisted, sent signed on object-creating calls

    @property
    def object_url(self) -> str:
        return s3_object_url(self.bucket, self.key, self.region)

    @classmethod
    def from_upload_params(cls, upload_params: dict[str, Any], region: str) -> "S3Target":
        return cls(
            bucket=upload_params["bucket"],
            key=upload_params["key"],
            region=region,
            credentials=upload_params["credentials"],
            upload_headers=s3_upload_headers(upload_params),
        )


@dataclass(frozen=True)
class _PartResult:
    """What a finished part leaves behind -- never its payload buffer."""

    part_number: int
    etag: str
    size: int


@dataclass(frozen=True)
class _S3Reply:
    """The final response of a (possibly retried) S3 call."""

    response: httpx.Response
    attempts: int
    error_code: str | None  # provider/synthetic code when the reply is a failure
    root: ET.Element | None  # parsed XML body of a successful reply, if any

    @property
    def ok(self) -> bool:
        return self.error_code is None and 200 <= self.response.status_code < 300


@dataclass(frozen=True)
class _FileSnapshot:
    size: int
    mtime_ns: int


def s3_object_url(bucket: str, key: str, region: str) -> str:
    """Virtual-hosted S3 URL with the key URI-encoded once (``/`` kept).

    The signer decodes and re-encodes the path exactly the same way, so the
    signed canonical URI and the target httpx sends are byte-identical even
    for keys with spaces, ``+``, ``%`` or other reserved characters.
    """
    return f"https://{bucket}.s3.{region}.amazonaws.com/{quote(key, safe='/~')}"


def s3_upload_headers(upload_params: dict[str, Any]) -> dict[str, str]:
    """The allowlisted uploadParams entries as S3 request headers."""
    return {
        header: str(upload_params[param])
        for param, header in _UPLOAD_HEADER_ALLOWLIST.items()
        if upload_params.get(param)
    }


def compute_part_size(size: int) -> int:
    """Part size for a file of ``size`` bytes, within S3's multipart limits.

    The default part size, grown when the file would otherwise need more than
    10,000 parts, rounded up to whole MiB. Raises UPLOAD_FAILED when even the
    5 GiB maximum part cannot fit the file in 10,000 parts.
    """
    part_size = max(S3_MULTIPART_PART_SIZE, -(-size // S3_MULTIPART_MAX_PARTS))
    part_size = -(-part_size // S3_MULTIPART_PART_ALIGNMENT) * S3_MULTIPART_PART_ALIGNMENT
    part_size = max(part_size, S3_MULTIPART_MIN_PART_SIZE)
    if part_size > S3_MULTIPART_MAX_PART_SIZE:
        max_bytes = S3_MULTIPART_MAX_PART_SIZE * S3_MULTIPART_MAX_PARTS
        raise _upload_error(
            f"File is too large for S3 upload: {size} bytes exceeds the S3 limit of "
            f"{S3_MULTIPART_MAX_PARTS} parts x {S3_MULTIPART_MAX_PART_SIZE} bytes "
            f"({max_bytes} bytes)."
        )
    return part_size


def assert_credentials_fresh(credentials: dict[str, Any]) -> None:
    """Fail fast when the federation credentials have already expired.

    ``Expiration`` is ISO 8601 (12 h after ``files/prepare``). A missing or
    unparseable value is not an error: S3 itself will reject an expired token.
    """
    raw = credentials.get("Expiration")
    if not isinstance(raw, str):
        return
    try:
        expires = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=datetime.UTC)
    if expires <= datetime.datetime.now(datetime.UTC):
        raise _upload_error(
            "The S3 upload credentials from files/prepare have already expired; "
            "prepare the upload again."
        )


def head_s3_object_size(file_detail: dict[str, Any]) -> int | None:
    """Content-Length of an uploaded object, via the file's READ credentials.

    ``file_detail`` is ``GET /v2/storage/files/{id}?federationToken=1``, whose
    ``credentials`` allow GetObject (hence HeadObject) on ``s3Path``. Returns
    ``None`` when the object cannot be checked (missing fields, non-200,
    network error) -- callers treat that as "unverified", never as success.
    """
    credentials = file_detail.get("credentials") or {}
    s3_path = file_detail.get("s3Path") or {}
    bucket, key = s3_path.get("bucket"), s3_path.get("key")
    if not (credentials.get("AccessKeyId") and bucket and key):
        return None
    region = file_detail.get("region") or "us-east-1"
    url = s3_object_url(bucket, key, region)
    headers = _s3_signed_headers(url, credentials, region, method="HEAD", sign_payload_hash=True)
    try:
        with _new_http_client() as http:
            response = http.head(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.debug("HeadObject verification failed: %s", type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.debug("HeadObject verification returned HTTP %d", response.status_code)
        return None
    try:
        return int(response.headers.get("content-length", ""))
    except ValueError:
        return None


def upload_s3_multipart(
    file_path: str,
    target: S3Target,
    *,
    on_progress: ProgressCallback | None = None,
    verify_uploaded: VerifyCallback | None = None,
    file_label: str = "",
) -> None:
    """Upload ``file_path`` to ``target`` with S3 multipart upload.

    Args:
        file_path: Local file to upload (read in parts, never whole).
        target: Bucket/key/region, write credentials and allowlisted headers.
        on_progress: ``(bytes_done, total_bytes)``, called from this thread
            after each completed part; monotonic, each part counted once.
        verify_uploaded: Called with the expected size when a retried Complete
            gets NoSuchUpload (the upload was either completed by the lost
            attempt or aborted). True confirms success; anything else fails.
        file_label: Storage file id, named in the ambiguous-completion error.

    Raises:
        KeboolaApiError: UPLOAD_FAILED, ``retryable=False``, on any failure.
    """
    assert_credentials_fresh(target.credentials)
    snapshot = _snapshot(file_path)
    part_size = compute_part_size(snapshot.size)
    part_count = max(1, -(-snapshot.size // part_size))

    with _new_http_client() as http:
        upload_id = _create_upload(http, target)
        try:
            etags = _upload_parts(
                http, target, upload_id, file_path, snapshot, part_size, part_count, on_progress
            )
            if _snapshot(file_path) != snapshot:
                raise _upload_error("The source file changed during upload (size or mtime).")
        except BaseException:
            _abort_upload(http, target, upload_id)
            raise
        _complete_upload(http, target, upload_id, etags, snapshot.size, verify_uploaded, file_label)


# ---------------------------------------------------------------------------
# Protocol steps
# ---------------------------------------------------------------------------


def _create_upload(http: httpx.Client, target: S3Target) -> str:
    reply = _send(
        http,
        target,
        method="POST",
        url=f"{target.object_url}?uploads",
        extra_headers=target.upload_headers,
        expect_root="InitiateMultipartUploadResult",
    )
    if not reply.ok or reply.root is None:
        raise _reply_error("S3 CreateMultipartUpload failed", reply)
    upload_id = reply.root.findtext(f"{_S3_NS}UploadId") or reply.root.findtext("UploadId")
    if not upload_id:
        raise _upload_error("S3 CreateMultipartUpload returned no UploadId.")
    return upload_id


def _upload_parts(
    http: httpx.Client,
    target: S3Target,
    upload_id: str,
    file_path: str,
    snapshot: _FileSnapshot,
    part_size: int,
    part_count: int,
    on_progress: ProgressCallback | None,
) -> dict[int, str]:
    """Upload every part with at most ``S3_MULTIPART_CONCURRENCY`` in flight.

    Submission is bounded: a new part is read and submitted only when a slot
    frees up, so no more than ``concurrency`` part buffers exist at once. On
    the first failure no new part is submitted, the in-flight ones are told to
    stop (they finish their current request) and the first error is raised.
    """
    stop = threading.Event()
    etags: dict[int, str] = {}
    bytes_done = 0
    failure: BaseException | None = None
    next_part = 1
    in_flight: set[Future[_PartResult]] = set()

    def run_part(part_number: int) -> _PartResult:
        offset = (part_number - 1) * part_size
        length = min(part_size, snapshot.size - offset)
        return _upload_part(http, target, upload_id, file_path, part_number, offset, length, stop)

    with ThreadPoolExecutor(
        max_workers=S3_MULTIPART_CONCURRENCY, thread_name_prefix="kbagent-s3-part"
    ) as pool:
        try:
            while in_flight or (failure is None and next_part <= part_count):
                while (
                    failure is None
                    and next_part <= part_count
                    and len(in_flight) < S3_MULTIPART_CONCURRENCY
                ):
                    in_flight.add(pool.submit(run_part, next_part))
                    next_part += 1
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                # httpx leaves a Response <-> stream reference cycle, and the
                # Response holds the Request whose content is the part's bytes.
                # Refcounting never frees a cycle, so a finished part stayed in
                # memory until the cyclic GC happened to run -- a live 10 GB
                # upload peaked at ~1.5 GB RSS. One collection per finished
                # part (every 64 MiB sent) keeps the peak ~concurrency x part.
                gc.collect()
                for future in done:
                    try:
                        result = future.result()
                    except BaseException as exc:  # first error wins; the rest drain
                        if failure is None:
                            failure = exc
                            stop.set()
                            for pending in in_flight:
                                pending.cancel()
                        continue
                    etags[result.part_number] = result.etag
                    bytes_done += result.size
                    if on_progress is not None and failure is None:
                        on_progress(bytes_done, snapshot.size)
        except BaseException:
            # Interrupted in this thread (Ctrl-C, a raising progress callback):
            # stop the workers so the executor's shutdown does not wait out
            # every remaining retry, then let the caller abort the upload.
            stop.set()
            raise
    if failure is not None:
        if isinstance(failure, KeboolaApiError):
            raise failure
        raise _upload_error(f"S3 part upload failed: {type(failure).__name__}") from failure
    return etags


def _upload_part(
    http: httpx.Client,
    target: S3Target,
    upload_id: str,
    file_path: str,
    part_number: int,
    offset: int,
    length: int,
    stop: threading.Event,
) -> _PartResult:
    body = _read_part(file_path, offset, length)
    if len(body) != length:
        raise _upload_error(
            f"The source file changed during upload (part {part_number} read "
            f"{len(body)} of {length} bytes)."
        )
    url = f"{target.object_url}?partNumber={part_number}&uploadId={quote(upload_id, safe='')}"
    reply = _send(
        http,
        target,
        method="PUT",
        url=url,
        body=body,
        stop=stop,
        attempts=S3_MULTIPART_PART_ATTEMPTS,
    )
    if not reply.ok:
        raise _reply_error(f"S3 upload of part {part_number} failed", reply)
    etag = reply.response.headers.get("etag")
    if not etag:
        raise _upload_error(f"S3 upload of part {part_number} returned no ETag.")
    return _PartResult(part_number=part_number, etag=etag, size=length)


def _read_part(file_path: str, offset: int, length: int) -> bytes:
    """Read exactly one part through its own handle (safe across threads)."""
    with open(file_path, "rb") as fh:
        fh.seek(offset)
        return fh.read(length)


def _complete_upload(
    http: httpx.Client,
    target: S3Target,
    upload_id: str,
    etags: dict[int, str],
    size: int,
    verify_uploaded: VerifyCallback | None,
    file_label: str,
) -> None:
    root = ET.Element("CompleteMultipartUpload", xmlns=_S3_NS.strip("{}"))
    for part_number in sorted(etags):
        part = ET.SubElement(root, "Part")
        ET.SubElement(part, "PartNumber").text = str(part_number)
        ET.SubElement(part, "ETag").text = etags[part_number]
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    reply = _send(
        http,
        target,
        method="POST",
        url=f"{target.object_url}?uploadId={quote(upload_id, safe='')}",
        body=body,
        expect_root="CompleteMultipartUploadResult",
    )
    if reply.ok:
        return
    if reply.error_code == "NoSuchUpload" and reply.attempts > 1:
        # An earlier attempt may have completed the upload before its response
        # was lost -- or the upload was aborted. Only the object can tell.
        if verify_uploaded is not None and verify_uploaded(size):
            logger.debug("Complete retry got NoSuchUpload; object verified, treating as done")
            return
        label = f" for Storage file {file_label}" if file_label else ""
        raise _upload_error(
            f"S3 multipart upload completion is ambiguous{label}: a retried "
            "CompleteMultipartUpload got NoSuchUpload and the uploaded object could "
            "not be verified. Upload the file again."
        )
    raise _reply_error("S3 CompleteMultipartUpload failed", reply)


def _abort_upload(http: httpx.Client, target: S3Target, upload_id: str) -> None:
    """Best-effort AbortMultipartUpload -- one attempt, every failure swallowed.

    The write federation policy does not grant s3:AbortMultipartUpload, so a
    403 is the expected answer; the bucket lifecycle reaps the parts instead.
    """
    url = f"{target.object_url}?uploadId={quote(upload_id, safe='')}"
    try:
        headers = _s3_signed_headers(
            url, target.credentials, target.region, method="DELETE", sign_payload_hash=True
        )
        response = http.request("DELETE", url, headers=headers)
        logger.debug("AbortMultipartUpload returned HTTP %d", response.status_code)
    except Exception as exc:  # best effort by design; never mask the original error
        logger.debug("AbortMultipartUpload failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Transport: signed request with retries
# ---------------------------------------------------------------------------


def _send(
    http: httpx.Client,
    target: S3Target,
    *,
    method: str,
    url: str,
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
    expect_root: str | None = None,
    stop: threading.Event | None = None,
    attempts: int = MAX_RETRIES,
) -> _S3Reply:
    """Send a signed S3 request, retrying transient failures.

    Re-signs every attempt (fresh x-amz-date) over the same bytes. Transient:
    a status in RETRYABLE_STATUS_CODES, a retryable S3 error code in the body
    (also inside a 200), a 2xx whose body is not ``expect_root``, and any
    httpx.TransportError. ``attempts`` is the total attempt budget (``MAX_RETRIES``
    by default, as in ``BaseHttpClient``; parts pass a larger one). Returns the last reply (successful or definitive);
    raises UPLOAD_FAILED when every attempt died in transport, or when
    ``stop`` is set (another part already failed).
    """
    payload_hash = hashlib.sha256(body).hexdigest()
    last_reply: _S3Reply | None = None
    last_exc: httpx.TransportError | None = None
    for attempt in range(attempts):
        if stop is not None and stop.is_set():
            raise _upload_error("S3 upload cancelled after another part failed.")
        headers = _s3_signed_headers(
            url,
            target.credentials,
            target.region,
            method=method,
            extra_headers=extra_headers,
            payload_hash=payload_hash,
            sign_payload_hash=True,
        )
        delay = BACKOFF_BASE * (2**attempt)
        try:
            response = http.request(method, url, content=body, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            logger.debug("S3 %s attempt %d: %s", method, attempt + 1, type(exc).__name__)
        else:
            last_reply = _classify(response, attempt + 1, expect_root)
            if last_reply.ok or not _is_transient(last_reply):
                return last_reply
            delay = _retry_delay(response, delay)
        if attempt < attempts - 1:
            _wait(delay, stop)
    if last_reply is not None and last_exc is None:
        return last_reply
    if last_reply is not None:
        # Mixed failures: report the HTTP reply, but keep the true attempt count.
        return _S3Reply(last_reply.response, attempts, last_reply.error_code, None)
    raise _upload_error(
        f"S3 {method} failed after {attempts} attempts: network error ({type(last_exc).__name__})."
    ) from last_exc


def _classify(response: httpx.Response, attempts: int, expect_root: str | None) -> _S3Reply:
    """Turn a raw response into success / provider error / malformed reply."""
    status = response.status_code
    if status >= 300:
        logger.debug(
            "S3 error response (HTTP %d): %s", status, response.text[:CLOUD_UPLOAD_ERROR_BODY_LIMIT]
        )
        code = _extract_cloud_error_code(response) or f"HTTP{status}"
        return _S3Reply(response, attempts, code, None)
    text = response.text.strip()
    if not text:
        code = _MALFORMED_REPLY if expect_root else None
        return _S3Reply(response, attempts, code, None)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return _S3Reply(response, attempts, _MALFORMED_REPLY if expect_root else None, None)
    local_name = root.tag.removeprefix(_S3_NS)
    if local_name == "Error":
        logger.debug("S3 error inside HTTP %d: %s", status, text[:CLOUD_UPLOAD_ERROR_BODY_LIMIT])
        match = _CLOUD_ERROR_CODE_RE.search(text[:CLOUD_UPLOAD_ERROR_BODY_LIMIT])
        return _S3Reply(response, attempts, match.group(1) if match else _MALFORMED_REPLY, None)
    if expect_root and local_name != expect_root:
        return _S3Reply(response, attempts, _MALFORMED_REPLY, None)
    return _S3Reply(response, attempts, None, root)


def _is_transient(reply: _S3Reply) -> bool:
    return (
        reply.response.status_code in RETRYABLE_STATUS_CODES
        or reply.error_code in _RETRYABLE_S3_CODES
        or reply.error_code == _MALFORMED_REPLY
    )


def _retry_delay(response: httpx.Response, default: float) -> float:
    """Honour a numeric Retry-After (capped), else the exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return default
    try:
        return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return default


def _wait(delay: float, stop: threading.Event | None) -> None:
    """Back off; a set ``stop`` event cuts a part's wait short."""
    if stop is None:
        time.sleep(delay)
    else:
        stop.wait(delay)


def _new_http_client() -> httpx.Client:
    """One client shared by every part thread (httpx.Client is thread-safe)."""
    return httpx.Client(timeout=FILE_UPLOAD_TIMEOUT)


def _snapshot(file_path: str) -> _FileSnapshot:
    stat = os.stat(file_path)
    return _FileSnapshot(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _reply_error(context: str, reply: _S3Reply) -> KeboolaApiError:
    status = reply.response.status_code
    code = reply.error_code or ""
    if code == "ExpiredToken":
        return _upload_error(
            f"{context}: the S3 upload credentials expired (HTTP {status}, ExpiredToken). "
            "They are valid for 12 hours from files/prepare -- a very large file needs a "
            "sustained upload rate high enough to finish within that window.",
            status_code=status,
        )
    suffix = f", {code}" if code and not code.startswith("HTTP") else ""
    return _upload_error(f"{context} (HTTP {status}{suffix}).", status_code=status)


def _upload_error(message: str, *, status_code: int = 0) -> KeboolaApiError:
    return KeboolaApiError(
        message=message,
        status_code=status_code,
        error_code=ErrorCode.UPLOAD_FAILED,
        retryable=False,
    )
