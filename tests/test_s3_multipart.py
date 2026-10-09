"""S3 multipart upload (client/_s3_multipart.py) and the SigV4 signer it uses.

The S3 side is a small in-memory fake behind ``httpx.MockTransport``: it
re-verifies every request's SigV4 signature from what was actually sent (so
URL encoding mismatches between signing and the wire fail here), assembles the
uploaded parts, and lets each test script failures per part / per call.
"""

import datetime
import hashlib
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from keboola_agent_cli.client import KeboolaClient, storage_tables
from keboola_agent_cli.client import _s3_multipart as m
from keboola_agent_cli.client._transfer import _s3_signed_headers
from keboola_agent_cli.constants import (
    S3_MULTIPART_MAX_PART_SIZE,
    S3_MULTIPART_MAX_PARTS,
)
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

GiB = 1024**3
MiB = 1024**2
NS = "http://s3.amazonaws.com/doc/2006-03-01/"
REGION = "eu-central-1"
BUCKET = "kbc-sapi-files"
KEY = "exp-15/1/files/2026/10/09/123.big file+v1.csv.gz"
UPLOAD_ID = "VXBsb2FkIElE+/=="
CREDS = {
    "AccessKeyId": "ASIATESTKEY",
    "SecretAccessKey": "test/secret+key",
    "SessionToken": "test-session-token==",
    "Expiration": "2999-01-01T00:00:00+00:00",
}
_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
_BASE = "https://connection.keboola.com"
_OWNED = {"host", "x-amz-date", "x-amz-content-sha256", "x-amz-security-token"}


def _target(
    headers: dict[str, str] | None = None, creds: dict[str, str] | None = None
) -> m.S3Target:
    return m.S3Target(
        bucket=BUCKET,
        key=KEY,
        region=REGION,
        credentials=creds or CREDS,
        upload_headers=headers if headers is not None else {"x-amz-acl": "private"},
    )


def _error_xml(code: str) -> bytes:
    return f"<?xml version='1.0'?><Error><Code>{code}</Code><Message>m</Message></Error>".encode()


def _verify_signature(request: httpx.Request) -> None:
    """Recompute SigV4 from what httpx actually sent and compare."""
    auth = request.headers["authorization"]
    signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
    for name in request.headers:
        if name.lower().startswith("x-amz-"):
            assert name.lower() in signed, f"{name} sent unsigned"
    body = request.content
    assert hashlib.sha256(body).hexdigest() == request.headers["x-amz-content-sha256"]
    now = datetime.datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=datetime.UTC
    )
    extra = {h: request.headers[h] for h in signed if h not in _OWNED}
    expected = _s3_signed_headers(
        str(request.url),
        CREDS,
        REGION,
        method=request.method,
        payload=body,
        extra_headers=extra,
        sign_payload_hash=True,
        now=now,
    )
    assert expected["Authorization"] == auth


class FakeS3:
    """In-memory S3 multipart endpoint with scriptable failures."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []  # (operation, detail)
        self.parts: dict[int, bytes] = {}
        self.part_attempts: dict[int, int] = {}
        self.create_queue: list[Any] = []
        self.part_queue: dict[int, list[Any]] = {}
        self.complete_queue: list[Any] = []
        self.complete_bodies: list[bytes] = []
        self.create_headers: dict[str, str] = {}
        self.on_part: Callable[[int], None] | None = None
        self.in_flight = 0
        self.max_in_flight = 0
        self.part_delay = 0.0
        self.etag_for: Callable[[int], str | None] = lambda n: f'"etag-{n}"'

    def ops(self, op: str) -> int:
        return sum(1 for o, _ in self.calls if o == op)

    @staticmethod
    def _scripted(queue: list[Any]) -> httpx.Response | None:
        if not queue:
            return None
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def handler(self, request: httpx.Request) -> httpx.Response:
        _verify_signature(request)
        url = urlparse(str(request.url))
        assert url.hostname == f"{BUCKET}.s3.{REGION}.amazonaws.com"
        params = parse_qs(url.query, keep_blank_values=True)
        if request.method == "POST" and "uploads" in params:
            return self._create(request)
        if request.method == "PUT" and "partNumber" in params:
            assert params["uploadId"] == [UPLOAD_ID]
            return self._part(int(params["partNumber"][0]), request.content)
        if request.method == "POST" and "uploadId" in params:
            return self._complete(request.content)
        if request.method == "DELETE":
            with self.lock:
                self.calls.append(("abort", ""))
            return httpx.Response(403, content=_error_xml("AccessDenied"))
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def _create(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.calls.append(("create", ""))
            self.create_headers = {
                k: v
                for k, v in request.headers.items()
                if k.startswith("x-amz-") and k not in _OWNED
            }
        scripted = self._scripted(self.create_queue)
        if scripted is not None:
            return scripted
        body = (
            f'<?xml version="1.0"?><InitiateMultipartUploadResult xmlns="{NS}">'
            f"<Bucket>{BUCKET}</Bucket><Key>k</Key><UploadId>{UPLOAD_ID}</UploadId>"
            "</InitiateMultipartUploadResult>"
        )
        return httpx.Response(200, content=body.encode())

    def _part(self, number: int, content: bytes) -> httpx.Response:
        with self.lock:
            self.calls.append(("part", str(number)))
            self.part_attempts[number] = self.part_attempts.get(number, 0) + 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.part_delay:
                time.sleep(self.part_delay)
            with self.lock:
                scripted = self._scripted(self.part_queue.get(number, []))
            if scripted is not None:
                return scripted
            with self.lock:
                self.parts[number] = content
            if self.on_part is not None:
                self.on_part(number)
            etag = self.etag_for(number)
            headers = {"ETag": etag} if etag else {}
            return httpx.Response(200, headers=headers)
        finally:
            with self.lock:
                self.in_flight -= 1

    def _complete(self, content: bytes) -> httpx.Response:
        with self.lock:
            self.calls.append(("complete", ""))
            self.complete_bodies.append(content)
        scripted = self._scripted(self.complete_queue)
        if scripted is not None:
            return scripted
        body = (
            f'<?xml version="1.0"?>\n\n   <CompleteMultipartUploadResult xmlns="{NS}">'
            f'<ETag>"final-3"</ETag></CompleteMultipartUploadResult>'
        )
        return httpx.Response(200, content=body.encode())

    def assembled(self) -> bytes:
        return b"".join(self.parts[n] for n in sorted(self.parts))


@pytest.fixture
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(
        m, "_new_http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler))
    )
    return fake


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(m, "_wait", lambda delay, stop: recorded.append(delay))
    return recorded


@pytest.fixture
def small_parts(monkeypatch: pytest.MonkeyPatch) -> int:
    """10-byte parts so multipart behaviour is testable with tiny files."""
    monkeypatch.setattr(m, "S3_MULTIPART_PART_SIZE", 10)
    monkeypatch.setattr(m, "S3_MULTIPART_PART_ALIGNMENT", 1)
    monkeypatch.setattr(m, "S3_MULTIPART_MIN_PART_SIZE", 1)
    return 10


def _file(tmp_path: Path, size: int) -> Path:
    path = tmp_path / "data.csv"
    path.write_bytes(bytes((i * 7 + 3) % 251 for i in range(size)))
    return path


def _complete_parts(body: bytes) -> list[tuple[int, str]]:
    root = ET.fromstring(body)
    return [
        (int(p.findtext(f"{{{NS}}}PartNumber") or 0), p.findtext(f"{{{NS}}}ETag") or "")
        for p in root.findall(f"{{{NS}}}Part")
    ]


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------

AWS_DOC_CREDS = {
    "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
    "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}
AWS_DOC_NOW = datetime.datetime(2013, 5, 24, tzinfo=datetime.UTC)


class TestSigner:
    @pytest.mark.parametrize(
        ("url", "extra", "signature"),
        [
            # AWS SigV4 documentation, "Example: GET Object" (Range header signed).
            (
                "https://examplebucket.s3.amazonaws.com/test.txt",
                {"Range": "bytes=0-9"},
                "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
            ),
            # "Example: GET Bucket Lifecycle" -- valueless ?lifecycle -> "lifecycle=".
            (
                "https://examplebucket.s3.amazonaws.com/?lifecycle",
                None,
                "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543",
            ),
            # "Example: Get Bucket (List Objects)" -- sorted query parameters.
            (
                "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J",
                None,
                "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
            ),
        ],
    )
    def test_aws_documentation_vectors(self, url: str, extra: dict | None, signature: str) -> None:
        headers = _s3_signed_headers(
            url,
            AWS_DOC_CREDS,
            "us-east-1",
            extra_headers=extra,
            sign_payload_hash=True,
            now=AWS_DOC_NOW,
        )
        assert headers["Authorization"].endswith(f"Signature={signature}")

    @pytest.mark.parametrize(
        ("url", "signature"),
        [
            (
                "https://bucket.s3.eu-central-1.amazonaws.com/exp/data%20file.csv.gz",
                "7374f587d092483fd9a637919bcc7d993ba483cab29fbc02bc316adf40d22e54",
            ),
            (
                "https://bucket.s3.eu-central-1.amazonaws.com/exp/manifest?versionId=abc&x-id=GetObject",
                "ef09b01443c5ee4145c62b62d38d3d8ed58345a8a9d0b6c4d2026061cc7e239e",
            ),
        ],
    )
    def test_download_get_signature_unchanged(self, url: str, signature: str) -> None:
        """Regression: the positional GET form signs exactly as before the change."""
        creds = {
            "AccessKeyId": "AKIDEXAMPLE",
            "SecretAccessKey": "secret/key+example",
            "SessionToken": "tok==",
        }
        now = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        headers = _s3_signed_headers(url, creds, "eu-central-1", now=now)
        assert headers["Authorization"] == (
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260102/eu-central-1/s3/aws4_request, "
            f"SignedHeaders=host;x-amz-date;x-amz-security-token, Signature={signature}"
        )
        assert headers["x-amz-security-token"] == "tok=="
        assert set(headers) == {
            "Authorization",
            "x-amz-date",
            "x-amz-content-sha256",
            "x-amz-security-token",
        }

    def test_valueless_query_equals_explicit_empty_value(self) -> None:
        base = "https://b.s3.us-east-1.amazonaws.com/k"
        a = _s3_signed_headers(f"{base}?uploads", AWS_DOC_CREDS, "us-east-1", now=AWS_DOC_NOW)
        b = _s3_signed_headers(f"{base}?uploads=", AWS_DOC_CREDS, "us-east-1", now=AWS_DOC_NOW)
        assert a["Authorization"] == b["Authorization"]

    def test_query_order_and_encoding_do_not_matter(self) -> None:
        base = "https://b.s3.us-east-1.amazonaws.com/k"
        a = _s3_signed_headers(
            f"{base}?uploadId=a%2Bb%2F%3D&partNumber=2", AWS_DOC_CREDS, "us-east-1", now=AWS_DOC_NOW
        )
        b = _s3_signed_headers(
            f"{base}?partNumber=2&uploadId=a%2bb/=", AWS_DOC_CREDS, "us-east-1", now=AWS_DOC_NOW
        )
        c = _s3_signed_headers(
            f"{base}?partNumber=2&uploadId=a b", AWS_DOC_CREDS, "us-east-1", now=AWS_DOC_NOW
        )
        assert a["Authorization"] == b["Authorization"]
        assert a["Authorization"] != c["Authorization"]  # '+' is a literal plus, not a space

    def test_reserved_key_characters_sign_what_httpx_sends(self) -> None:
        key = "dir/a b+c%d~e!*'()=&;$,@:.csv"
        url = m.s3_object_url("bkt", key, "us-east-1")
        sent = str(httpx.Request("PUT", url).url)
        a = _s3_signed_headers(url, AWS_DOC_CREDS, "us-east-1", method="PUT", now=AWS_DOC_NOW)
        b = _s3_signed_headers(sent, AWS_DOC_CREDS, "us-east-1", method="PUT", now=AWS_DOC_NOW)
        assert a["Authorization"] == b["Authorization"]
        assert "/dir/" in sent and "%20" in sent and "%2B" in sent

    def test_extra_headers_signed_and_returned(self) -> None:
        headers = _s3_signed_headers(
            "https://b.s3.us-east-1.amazonaws.com/k",
            AWS_DOC_CREDS,
            "us-east-1",
            method="PUT",
            extra_headers={" X-Amz-ACL ": "  private ", "x-amz-server-side-encryption": "AES256"},
            sign_payload_hash=True,
            now=AWS_DOC_NOW,
        )
        assert headers["x-amz-acl"] == "private"
        assert headers["x-amz-server-side-encryption"] == "AES256"
        assert (
            "SignedHeaders=host;x-amz-acl;x-amz-content-sha256;x-amz-date;"
            "x-amz-server-side-encryption" in headers["Authorization"]
        )

    @pytest.mark.parametrize("name", ["Authorization", "x-amz-date", "X-Amz-Security-Token"])
    def test_extra_headers_cannot_override_signer_headers(self, name: str) -> None:
        with pytest.raises(ValueError, match="signer-owned"):
            _s3_signed_headers(
                "https://b.s3.us-east-1.amazonaws.com/k",
                AWS_DOC_CREDS,
                "us-east-1",
                extra_headers={name: "x"},
            )

    def test_payload_hash_override(self) -> None:
        digest = hashlib.sha256(b"body").hexdigest()
        a = _s3_signed_headers(
            "https://b.s3.us-east-1.amazonaws.com/k",
            AWS_DOC_CREDS,
            "us-east-1",
            method="PUT",
            payload=b"body",
            now=AWS_DOC_NOW,
        )
        b = _s3_signed_headers(
            "https://b.s3.us-east-1.amazonaws.com/k",
            AWS_DOC_CREDS,
            "us-east-1",
            method="PUT",
            payload_hash=digest,
            now=AWS_DOC_NOW,
        )
        assert a == b


# ---------------------------------------------------------------------------
# Part sizing
# ---------------------------------------------------------------------------


class TestPartSizing:
    def test_200_gib_uses_default_parts(self) -> None:
        size = 200 * GiB
        part = m.compute_part_size(size)
        assert part == 64 * MiB
        assert -(-size // part) == 3200

    def test_scales_up_beyond_10k_parts(self) -> None:
        size = 1024 * GiB  # 16,384 default parts -> too many
        part = m.compute_part_size(size)
        assert part > 64 * MiB
        assert part % MiB == 0
        assert -(-size // part) <= S3_MULTIPART_MAX_PARTS

    def test_largest_possible_file_fits(self) -> None:
        size = S3_MULTIPART_MAX_PART_SIZE * S3_MULTIPART_MAX_PARTS
        assert m.compute_part_size(size) == S3_MULTIPART_MAX_PART_SIZE

    def test_too_large_refused(self) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            m.compute_part_size(S3_MULTIPART_MAX_PART_SIZE * S3_MULTIPART_MAX_PARTS + 1)
        assert exc_info.value.error_code == ErrorCode.UPLOAD_FAILED
        assert "too large" in exc_info.value.message

    def test_too_large_refused_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(m, "S3_MULTIPART_PART_ALIGNMENT", 1)
        monkeypatch.setattr(m, "S3_MULTIPART_PART_SIZE", 4)
        monkeypatch.setattr(m, "S3_MULTIPART_MAX_PART_SIZE", 8)
        monkeypatch.setattr(m, "S3_MULTIPART_MAX_PARTS", 2)
        with pytest.raises(KeboolaApiError, match="too large"):
            m.upload_s3_multipart(str(_file(tmp_path, 17)), _target())
        assert fake_s3.calls == []


# ---------------------------------------------------------------------------
# Multipart protocol
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("waits")
class TestMultipartHappyPath:
    @pytest.mark.parametrize(("size", "parts"), [(1, 1), (10, 1), (11, 2), (30, 3), (37, 4)])
    def test_uploads_every_byte_in_order(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, size: int, parts: int
    ) -> None:
        path = _file(tmp_path, size)
        progress: list[tuple[int, int]] = []
        m.upload_s3_multipart(
            str(path),
            _target({"x-amz-acl": "private", "x-amz-server-side-encryption": "AES256"}),
            on_progress=lambda done, total: progress.append((done, total)),
        )
        assert fake_s3.assembled() == path.read_bytes()
        assert sorted(fake_s3.parts) == list(range(1, parts + 1))
        assert all(len(fake_s3.parts[n]) == 10 for n in range(1, parts))
        assert fake_s3.ops("create") == 1 and fake_s3.ops("complete") == 1
        assert fake_s3.ops("abort") == 0
        assert _complete_parts(fake_s3.complete_bodies[0]) == [
            (n, f'"etag-{n}"') for n in range(1, parts + 1)
        ]
        # Allowlisted headers ride the Create (signature verified by the fake).
        assert fake_s3.create_headers == {
            "x-amz-acl": "private",
            "x-amz-server-side-encryption": "AES256",
        }
        # Progress: one call per part, monotonic, ends at the total.
        assert len(progress) == parts
        assert [d for d, _ in progress] == sorted(d for d, _ in progress)
        assert progress[-1] == (size, size)
        assert {t for _, t in progress} == {size}

    def test_bounded_buffers_and_concurrency(
        self, monkeypatch: pytest.MonkeyPatch, small_parts: int, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        fake_s3.part_delay = 0.01
        reads: list[int] = []
        live = {"now": 0, "max": 0}
        lock = threading.Lock()
        real_read, real_upload = m._read_part, m._upload_part

        def counting_read(file_path: str, offset: int, length: int) -> bytes:
            data = real_read(file_path, offset, length)
            with lock:
                reads.append(len(data))
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            return data

        def counting_upload(*args: Any) -> Any:
            try:
                return real_upload(*args)
            finally:
                with lock:
                    live["now"] -= 1

        monkeypatch.setattr(m, "_read_part", counting_read)
        monkeypatch.setattr(m, "_upload_part", counting_upload)
        path = _file(tmp_path, 200)  # 20 parts
        m.upload_s3_multipart(str(path), _target())
        assert fake_s3.assembled() == path.read_bytes()
        assert max(reads) <= small_parts
        assert len(reads) == 20
        assert live["max"] <= m.S3_MULTIPART_CONCURRENCY
        assert fake_s3.max_in_flight <= m.S3_MULTIPART_CONCURRENCY
        assert fake_s3.max_in_flight > 1  # actually parallel


class TestMultipartRetries:
    def test_part_503_retried_alone(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.part_queue[2] = [httpx.Response(503, content=_error_xml("SlowDown"))]
        path = _file(tmp_path, 30)
        m.upload_s3_multipart(str(path), _target())
        assert fake_s3.part_attempts == {1: 1, 2: 2, 3: 1}
        assert fake_s3.assembled() == path.read_bytes()
        assert waits == [1.0]

    def test_request_timeout_400_is_retried(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.part_queue[1] = [httpx.Response(400, content=_error_xml("RequestTimeout"))]
        m.upload_s3_multipart(str(_file(tmp_path, 10)), _target())
        assert fake_s3.part_attempts == {1: 2}

    def test_transport_error_retried(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.part_queue[1] = [httpx.ReadError("reset"), httpx.ConnectError("refused")]
        path = _file(tmp_path, 20)
        m.upload_s3_multipart(str(path), _target())
        assert fake_s3.part_attempts[1] == 3
        assert fake_s3.assembled() == path.read_bytes()
        assert waits == [1.0, 2.0]

    def test_retry_after_honoured_and_capped(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.part_queue[1] = [
            httpx.Response(503, headers={"Retry-After": "7"}),
            httpx.Response(503, headers={"Retry-After": "99999"}),
        ]
        m.upload_s3_multipart(str(_file(tmp_path, 10)), _target())
        assert waits == [7.0, float(m.MAX_RETRY_AFTER_SECONDS)]

    def test_part_retries_exhausted(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.part_queue[1] = [httpx.Response(500)] * m.S3_MULTIPART_PART_ATTEMPTS
        with pytest.raises(KeboolaApiError, match="part 1 failed \\(HTTP 500\\)"):
            m.upload_s3_multipart(str(_file(tmp_path, 10)), _target())
        # Parts get a larger budget than the general MAX_RETRIES: one dead part
        # aborts the whole upload.
        assert m.S3_MULTIPART_PART_ATTEMPTS > m.MAX_RETRIES
        assert fake_s3.part_attempts[1] == m.S3_MULTIPART_PART_ATTEMPTS
        assert fake_s3.ops("abort") == 1

    def test_create_retried_on_5xx(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.create_queue = [httpx.Response(502)]
        m.upload_s3_multipart(str(_file(tmp_path, 10)), _target())
        assert fake_s3.ops("create") == 2


@pytest.mark.usefixtures("waits")
class TestMultipartFailures:
    def test_expired_token_fails_immediately_and_aborts(
        self, monkeypatch: pytest.MonkeyPatch, small_parts: int, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(m, "S3_MULTIPART_CONCURRENCY", 1)
        fake_s3.part_queue[2] = [httpx.Response(400, content=_error_xml("ExpiredToken"))]
        with pytest.raises(KeboolaApiError) as exc_info:
            m.upload_s3_multipart(str(_file(tmp_path, 50)), _target())
        err = exc_info.value
        assert err.error_code == ErrorCode.UPLOAD_FAILED and not err.retryable
        assert "part 2" in err.message and "ExpiredToken" in err.message
        assert "12 hours" in err.message
        assert fake_s3.part_attempts[2] == 1
        assert 3 not in fake_s3.part_attempts  # nothing submitted after the failure
        assert fake_s3.ops("abort") == 1  # 403 from abort swallowed, original kept
        assert fake_s3.ops("complete") == 0

    def test_access_denied_names_code(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        fake_s3.part_queue[1] = [httpx.Response(403, content=_error_xml("AccessDenied"))]
        with pytest.raises(KeboolaApiError, match="HTTP 403, AccessDenied"):
            m.upload_s3_multipart(str(_file(tmp_path, 10)), _target())
        assert fake_s3.part_attempts[1] == 1

    def test_missing_etag(self, small_parts: int, fake_s3: FakeS3, tmp_path: Path) -> None:
        fake_s3.etag_for = lambda n: None if n == 2 else f'"e{n}"'
        with pytest.raises(KeboolaApiError, match="part 2 returned no ETag"):
            m.upload_s3_multipart(str(_file(tmp_path, 20)), _target())
        assert fake_s3.ops("abort") == 1

    def test_expired_credentials_refused_before_any_request(
        self, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        creds = {**CREDS, "Expiration": "2020-01-01T00:00:00Z"}
        with pytest.raises(KeboolaApiError, match="already expired"):
            m.upload_s3_multipart(str(_file(tmp_path, 10)), _target(creds=creds))
        assert fake_s3.calls == []

    def test_file_shrinks_mid_upload(
        self, monkeypatch: pytest.MonkeyPatch, small_parts: int, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(m, "S3_MULTIPART_CONCURRENCY", 1)
        path = _file(tmp_path, 30)

        def shrink(number: int) -> None:
            if number == 1:
                os.truncate(path, 15)

        fake_s3.on_part = shrink
        with pytest.raises(KeboolaApiError, match="source file changed during upload"):
            m.upload_s3_multipart(str(path), _target())
        assert fake_s3.ops("abort") == 1
        assert fake_s3.ops("complete") == 0

    def test_file_mtime_changes_before_complete(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        path = _file(tmp_path, 20)

        def touch(number: int) -> None:
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

        fake_s3.on_part = touch
        with pytest.raises(KeboolaApiError, match="source file changed during upload"):
            m.upload_s3_multipart(str(path), _target())
        assert fake_s3.ops("complete") == 0
        assert fake_s3.ops("abort") == 1


class TestComplete:
    def test_200_with_retryable_error_is_retried(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.complete_queue = [
            httpx.Response(
                200, content=b'<?xml version="1.0"?>\n   \n' + _error_xml("InternalError")[21:]
            )
        ]
        m.upload_s3_multipart(str(_file(tmp_path, 20)), _target())
        assert fake_s3.ops("complete") == 2
        assert fake_s3.complete_bodies[0] == fake_s3.complete_bodies[1]

    def test_200_with_other_error_fails(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.complete_queue = [httpx.Response(200, content=b"   " + _error_xml("InvalidPart"))]
        with pytest.raises(
            KeboolaApiError, match="CompleteMultipartUpload failed \\(HTTP 200, InvalidPart\\)"
        ):
            m.upload_s3_multipart(str(_file(tmp_path, 20)), _target())
        assert fake_s3.ops("complete") == 1

    def test_truncated_success_body_is_retried(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float]
    ) -> None:
        fake_s3.complete_queue = [httpx.Response(200, content=b"   \n  ")]
        m.upload_s3_multipart(str(_file(tmp_path, 20)), _target())
        assert fake_s3.ops("complete") == 2

    @pytest.mark.parametrize("verified", [True, False])
    def test_retry_no_such_upload_uses_verifier(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path, waits: list[float], verified: bool
    ) -> None:
        fake_s3.complete_queue = [
            httpx.ReadTimeout("lost response"),
            httpx.Response(404, content=_error_xml("NoSuchUpload")),
        ]
        checked: list[int] = []

        def verify(size: int) -> bool:
            checked.append(size)
            return verified

        upload = lambda: m.upload_s3_multipart(  # noqa: E731
            str(_file(tmp_path, 20)), _target(), verify_uploaded=verify, file_label="123"
        )
        if verified:
            upload()
        else:
            with pytest.raises(KeboolaApiError, match="ambiguous for Storage file 123"):
                upload()
        assert checked == [20]
        assert fake_s3.ops("abort") == 0

    def test_first_attempt_no_such_upload_is_not_ambiguous(
        self, small_parts: int, fake_s3: FakeS3, tmp_path: Path
    ) -> None:
        fake_s3.complete_queue = [httpx.Response(404, content=_error_xml("NoSuchUpload"))]
        called: list[int] = []
        with pytest.raises(KeboolaApiError, match="HTTP 404, NoSuchUpload"):
            m.upload_s3_multipart(
                str(_file(tmp_path, 20)),
                _target(),
                verify_uploaded=lambda s: called.append(s) or True,
            )
        assert called == []


# ---------------------------------------------------------------------------
# KeboolaClient._upload_to_cloud routing + verifier
# ---------------------------------------------------------------------------


def _upload_info() -> dict[str, Any]:
    return {
        "id": 123,
        "provider": "aws",
        "region": REGION,
        "uploadParams": {"key": KEY, "bucket": BUCKET, "acl": "private", "credentials": CREDS},
    }


class TestUploadToCloudRouting:
    @pytest.mark.parametrize("size", [0, 32])
    def test_at_or_below_threshold_single_signed_put(
        self, monkeypatch: pytest.MonkeyPatch, httpx_mock: Any, tmp_path: Path, size: int
    ) -> None:
        monkeypatch.setattr(storage_tables, "S3_MULTIPART_THRESHOLD", 32)
        httpx_mock.add_response(method="PUT", status_code=200)
        path = _file(tmp_path, size)
        progress: list[tuple[int, int]] = []
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(
                _upload_info(), str(path), on_progress=lambda d, t: progress.append((d, t))
            )
        (request,) = httpx_mock.get_requests()
        assert request.method == "PUT"
        assert request.content == path.read_bytes()
        assert request.headers["x-amz-acl"] == "private"
        _verify_signature(request)
        assert progress == [(size, size)]

    def test_single_put_failure_raises_upload_failed(
        self, monkeypatch: pytest.MonkeyPatch, httpx_mock: Any, tmp_path: Path
    ) -> None:
        httpx_mock.add_response(method="PUT", status_code=403, content=_error_xml("AccessDenied"))
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client._upload_to_cloud(_upload_info(), str(_file(tmp_path, 5)))
        assert exc_info.value.error_code == ErrorCode.UPLOAD_FAILED
        assert "AccessDenied" in exc_info.value.message

    def test_above_threshold_goes_multipart(
        self,
        monkeypatch: pytest.MonkeyPatch,
        small_parts: int,
        fake_s3: FakeS3,
        tmp_path: Path,
        waits: list[float],
    ) -> None:
        monkeypatch.setattr(storage_tables, "S3_MULTIPART_THRESHOLD", 32)
        path = _file(tmp_path, 33)
        progress: list[tuple[int, int]] = []
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(
                _upload_info(), str(path), on_progress=lambda d, t: progress.append((d, t))
            )
        assert fake_s3.ops("create") == 1 and len(fake_s3.parts) == 4
        assert fake_s3.assembled() == path.read_bytes()
        assert fake_s3.create_headers == {"x-amz-acl": "private"}
        assert progress[-1] == (33, 33) and len(progress) == 4

    @pytest.mark.parametrize(("content_length", "expected"), [("20", True), ("19", False)])
    def test_verifier_heads_object_with_read_credentials(
        self, httpx_mock: Any, content_length: str, expected: bool
    ) -> None:
        read_creds = {"AccessKeyId": "ASIAREAD", "SecretAccessKey": "s", "SessionToken": "t"}
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/123?federationToken=1",
            json={
                "id": 123,
                "region": REGION,
                "credentials": read_creds,
                "s3Path": {"bucket": BUCKET, "key": KEY},
            },
        )
        httpx_mock.add_response(method="HEAD", headers={"Content-Length": content_length})
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            verify = client._s3_upload_verifier(123)
            assert verify(20) is expected
        head = httpx_mock.get_requests()[-1]
        assert head.method == "HEAD"
        assert "Credential=ASIAREAD/" in head.headers["authorization"]
        assert head.url.host == f"{BUCKET}.s3.{REGION}.amazonaws.com"

    def test_verifier_without_file_id_is_false(self) -> None:
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            assert client._s3_upload_verifier(None)(10) is False
