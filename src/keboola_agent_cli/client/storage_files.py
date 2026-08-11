"""Storage Files API: upload, download (incl. sliced), tagging.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ..constants import (
    FILE_DOWNLOAD_CHUNK_SIZE,
    FILE_DOWNLOAD_TIMEOUT,
)
from ..errors import ErrorCode, KeboolaApiError
from ._core import _CoreClient
from ._transfer import (
    _assert_safe_download_url,
    _CloudDownloader,
    _IterBytesReader,
)

if TYPE_CHECKING:
    from ._client import KeboolaClient

logger = logging.getLogger(__name__)


class _StorageFilesMixin(_CoreClient):
    """Storage Files API: upload, download (incl. sliced), tagging."""

    def get_file_info(self, file_id: int, branch_id: int | None = None) -> dict[str, Any]:
        """Get file metadata including download URL.

        Args:
            file_id: Storage file ID (from export job results).
            branch_id: If set, query file from a specific dev branch scope.

        Returns:
            File resource dict with 'url', 'isSliced', 'sizeBytes', etc.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "GET",
            f"{prefix}/files/{file_id}",
            params={"federationToken": "1"},
        )
        return response.json()

    def list_files(
        self,
        limit: int = 20,
        offset: int = 0,
        tags: list[str] | None = None,
        since_id: int | None = None,
        query: str | None = None,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List Storage Files with optional filtering.

        Args:
            limit: Max number of files to return.
            offset: Pagination offset.
            tags: Filter by tags (AND logic — all tags must match).
            since_id: Return only files with ID greater than this.
            query: Full-text search query on file name.
            branch_id: If set, list files from a specific dev branch.

        Returns:
            List of file resource dicts.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tags:
            for i, tag in enumerate(tags):
                params[f"tags[{i}]"] = tag
        if since_id is not None:
            params["sinceId"] = since_id
        if query:
            params["q"] = query
        response = self._request("GET", f"{prefix}/files", params=params)
        return response.json()

    def upload_file(
        self: "KeboolaClient",
        file_path: str,
        name: str | None = None,
        tags: list[str] | None = None,
        is_permanent: bool = False,
        notify: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to Storage Files.

        Wraps prepare_file_upload + _upload_to_cloud into a single call.

        Args:
            file_path: Local path to the file to upload.
            name: Custom filename (defaults to local file basename).
            tags: Optional list of tags to assign.
            is_permanent: If True, file is not auto-deleted after 15 days.
            notify: If True, send notification on upload completion.
            branch_id: If set, upload to a specific dev branch.

        Returns:
            File resource dict with id, name, sizeBytes, tags, url.
        """
        p = Path(file_path)
        size_bytes = p.stat().st_size
        file_name = name or p.name
        upload_info = self.prepare_file_upload(
            name=file_name,
            size_bytes=size_bytes,
            tags=tags,
            is_permanent=is_permanent,
            notify=notify,
        )
        self._upload_to_cloud(upload_info, file_path)
        # Return file info (prepare response has the file metadata)
        return {
            "id": upload_info["id"],
            "name": upload_info.get("name", file_name),
            "sizeBytes": size_bytes,
            "tags": upload_info.get("tags", tags or []),
            "isPermanent": upload_info.get("isPermanent", is_permanent),
            "created": upload_info.get("created"),
        }

    def delete_file(self, file_id: int, branch_id: int | None = None) -> None:
        """Delete a Storage File.

        Args:
            file_id: Storage file ID.
            branch_id: If set, target a file in a specific dev branch scope.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request("DELETE", f"{prefix}/files/{file_id}")

    def tag_file(self, file_id: int, tag: str, branch_id: int | None = None) -> None:
        """Add a tag to a Storage File.

        Args:
            file_id: Storage file ID.
            tag: Tag string to add.
            branch_id: If set, target a file in a specific dev branch scope.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request("POST", f"{prefix}/files/{file_id}/tags", data={"tag": tag})

    def untag_file(self, file_id: int, tag: str, branch_id: int | None = None) -> None:
        """Remove a tag from a Storage File.

        Args:
            file_id: Storage file ID.
            tag: Tag string to remove.
            branch_id: If set, target a file in a specific dev branch scope.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_tag = quote(tag, safe="")
        self._request("DELETE", f"{prefix}/files/{file_id}/tags/{safe_tag}")

    def download_sliced_file(self, file_detail: dict[str, Any], output_path: str) -> int:
        """Download a sliced file by fetching manifest and concatenating slices.

        Handles S3 (SigV4 auth) and GCS (bearer token) providers.
        Decompresses gzipped slices transparently.

        Streams each slice chunk-by-chunk into a temp file and concatenates
        into ``output_path``. Peak RAM is O(chunk size), not O(slice size) —
        required for multi-GB tables on memory-constrained hosts (issue #187).

        The manifest `url` from file info is already a presigned URL (download
        directly). Manifest entries have cloud-native URLs (s3://, gs://) that
        need auth — we build HTTPS URLs from the s3Path/gcsPath credentials.

        Args:
            file_detail: Full file info dict from get_file_info()
                (must include provider credentials from federationToken=1).
            output_path: Local file path to write to.

        Returns:
            Number of bytes written.
        """
        import os
        import shutil
        import tempfile

        entries, base_url, downloader, _manifest_data = self._prepare_sliced_download(file_detail)

        # Stream each slice into a temp file, then copy-append into output.
        # Keeping per-slice temp files on disk (not in RAM) is the whole point.
        total = 0
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as out_fh:
            for entry in entries:
                entry_url = entry.get("url", "")
                slice_url = downloader.resolve_slice_url(base_url, entry_url, file_detail)
                is_gz = entry_url.split("?")[0].endswith(".gz")
                # `mkstemp` + immediate close, NOT `NamedTemporaryFile`. We hand
                # the *path* to `stream_to_file`, which opens it a second time --
                # and a `NamedTemporaryFile` cannot be reopened by name on
                # Windows while its own handle is open (documented platform
                # difference), so every sliced download died there with
                # `PermissionError: [Errno 13] Permission denied`. mkstemp gives
                # the same collision-free name without holding it open.
                fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, prefix=".slice-")
                os.close(fd)
                tmp_path = Path(tmp_name)
                try:
                    downloader.stream_to_file(slice_url, tmp_name, decompress_gzip=is_gz)
                    with tmp_path.open("rb") as tmp:
                        shutil.copyfileobj(tmp, out_fh, length=FILE_DOWNLOAD_CHUNK_SIZE)
                    total += tmp_path.stat().st_size
                finally:
                    tmp_path.unlink(missing_ok=True)

        return total

    def _prepare_sliced_download(
        self, file_detail: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str, "_CloudDownloader", bytes]:
        """Fetch and parse the manifest, returning entries + download context.

        The manifest is small JSON (few KB even for TB tables), so loading it
        fully is fine. Entries are the per-slice URLs that callers iterate.

        Returns a 4-tuple: (entries, base_url, downloader, raw_manifest_bytes).
        The raw manifest is useful for callers that persist it next to slices.
        """
        import json as json_mod

        provider = file_detail.get("provider", "")
        downloader = _CloudDownloader.create(file_detail)

        _assert_safe_download_url(file_detail["url"])
        with httpx.Client(timeout=FILE_DOWNLOAD_TIMEOUT) as http:
            resp = http.get(file_detail["url"])
            resp.raise_for_status()
            manifest_data = resp.content

        manifest = json_mod.loads(manifest_data)
        entries = manifest.get("entries", [])
        if not entries:
            raise KeboolaApiError(
                message="Sliced file manifest has no entries",
                status_code=500,
                error_code=ErrorCode.EXPORT_EMPTY_MANIFEST,
                retryable=False,
            )

        logger.info("Downloading %d slices (provider=%s)", len(entries), provider)
        base_url = downloader.resolve_base_url(file_detail)
        return entries, base_url, downloader, manifest_data

    def download_sliced_file_to_dir(
        self, file_detail: dict[str, Any], output_dir: str
    ) -> dict[str, Any]:
        """Download a sliced file preserving each slice as a separate local file.

        Unlike download_sliced_file() which binary-concatenates slices, this
        writes every manifest entry into its own file under ``output_dir``.
        Required for formats like Parquet where each slice is a self-contained
        file with its own footer and cannot be safely concatenated.

        The original manifest is also written to ``output_dir/_manifest.json``
        so the slice set stays self-describing. The leading underscore follows
        the Hive/Spark/pyarrow convention that makes Parquet readers skip the
        file when scanning the directory as a dataset.

        Gzip-compressed slices (typical for CSV) are decompressed transparently
        and the ``.gz`` suffix is stripped from the written filename. Parquet
        slices are written as-is (Snappy compression lives inside the format).

        Args:
            file_detail: Full file info dict from get_file_info() with
                federationToken=1 provider credentials.
            output_dir: Directory to write slices into. Created if missing.

        Returns:
            Dict with ``output_dir``, ``slice_count``, ``total_bytes``, and
            ``slices`` (list of ``{path, size_bytes}``).
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        entries, base_url, downloader, manifest_data = self._prepare_sliced_download(file_detail)

        # Persist the manifest alongside slices for traceability.
        (out / "_manifest.json").write_bytes(manifest_data)

        slices: list[dict[str, Any]] = []
        total = 0

        for idx, entry in enumerate(entries):
            entry_url = entry.get("url", "")
            slice_url = downloader.resolve_slice_url(base_url, entry_url, file_detail)

            clean_url = entry_url.split("?")[0]
            basename = clean_url.rsplit("/", 1)[-1]
            is_gz = clean_url.endswith(".gz")
            if is_gz:
                basename = basename.removesuffix(".gz")
            if not basename:
                basename = f"part-{idx:05d}"

            slice_path = out / basename
            written = downloader.stream_to_file(slice_url, slice_path, decompress_gzip=is_gz)
            slices.append({"path": str(slice_path.resolve()), "size_bytes": written})
            total += written

        return {
            "output_dir": str(out.resolve()),
            "slice_count": len(slices),
            "total_bytes": total,
            "slices": slices,
        }

    def download_file(self, url: str, output_path: str) -> int:
        """Download a non-sliced file from a presigned URL.

        Streams the body chunk-by-chunk and decompresses gzip on the fly, so
        peak RAM stays at O(chunk size) even for multi-GB payloads (issue #187).

        Args:
            url: Presigned download URL from file info.
            output_path: Local file path to write to.

        Returns:
            Number of bytes written (post-decompression if the URL is gzipped).
        """
        import gzip
        import shutil

        _assert_safe_download_url(url)
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        is_gzipped = url.rstrip("?").split("?")[0].endswith(".gz")

        with (
            httpx.Client(timeout=FILE_DOWNLOAD_TIMEOUT) as http,
            http.stream("GET", url) as response,
        ):
            response.raise_for_status()
            source: Any = _IterBytesReader(response.iter_bytes(FILE_DOWNLOAD_CHUNK_SIZE))
            if is_gzipped:
                source = gzip.GzipFile(fileobj=source, mode="rb")
            with out_path.open("wb") as fh:
                shutil.copyfileobj(source, fh, length=FILE_DOWNLOAD_CHUNK_SIZE)

        return out_path.stat().st_size
