"""Data Streams (per-device OTLP sources) -- delegate to StreamClient.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

from typing import Any

from ..constants import (
    OTLP_BUCKET_PREFIX,
    STREAM_DEFAULT_BRANCH,
)
from ..stream_client import StreamClient, provision_otlp_sinks, stream_task_source_id
from ._core import _CoreClient


class _StreamMixin(_CoreClient):
    """Data Streams (per-device OTLP sources) -- delegate to StreamClient."""

    # ------------------------------------------------------------------
    # Data Streams (per-device OTLP sources) -- delegate to StreamClient
    # ------------------------------------------------------------------

    def _get_stream_client(self) -> StreamClient:
        """Lazily build (and cache) a :class:`StreamClient` over this stack+token.

        The Stream control plane lives on a sibling host (``stream.<region>``)
        and authenticates with the same Storage token, so it is reachable from
        the same ``(stack_url, token)`` this client already holds.
        """
        if self._stream_client is None:
            self._stream_client = StreamClient(stack_url=self._stack_url, token=self._token)
        return self._stream_client

    @staticmethod
    def _stream_source_detail(
        source: dict[str, Any], branch_id: str, sink_bucket_id: str | None
    ) -> dict[str, Any]:
        """Normalise a raw Stream ``source`` object into the returned detail dict.

        Flattens the OTLP block so the caller gets ``otlp_url`` (with the ingest
        secret embedded, **unmasked** -- the lib layer hands it to the device
        once and never persists it) without digging into ``source["otlp"]``. The
        raw ``source`` is echoed under ``source`` so nothing is lost.
        """
        otlp = source.get("otlp") or {}
        source_id = source.get("sourceId", "")
        return {
            "id": source_id,
            "source_id": source_id,
            "name": source.get("name", ""),
            "type": source.get("type", ""),
            "description": source.get("description", ""),
            "branch_id": branch_id,
            "otlp_url": otlp.get("url", ""),
            "otlp_secret": otlp.get("secret", ""),
            "base_endpoint": otlp.get("baseUrl", ""),
            "sink_bucket_id": sink_bucket_id,
            "source": source,
        }

    def create_stream_source(
        self,
        name: str,
        *,
        source_type: str = "otlp",
        description: str = "",
        branch_id: str = STREAM_DEFAULT_BRANCH,
        provision_sinks: bool = True,
    ) -> dict[str, Any]:
        """Create a per-device OTLP (or HTTP) stream source and return its detail.

        Async under the hood: the Stream API returns a 202 ``Task`` which is
        polled to completion here (the caller sees one blocking call). For an
        ``otlp`` source with ``provision_sinks`` (default) the logs/metrics/traces
        sinks and the ``in.c-otlp-<sourceId>`` sink bucket are auto-created so
        OTLP data actually lands in Storage -- and so a scoped device token can be
        granted ``write`` on that bucket. Pass ``provision_sinks=False`` for a
        bare source.

        Per-device sources are the unit of isolated event-plane revocation:
        delete one device's source (:meth:`delete_stream_source`) without
        rotating a secret shared with other devices.

        Returns a dict with the flattened endpoint: ``id`` / ``source_id``,
        ``otlp_url`` (secret **unmasked**), ``otlp_secret``, ``sink_bucket_id``
        (``None`` when no sinks were provisioned), and the raw ``source``.
        Requires a Storage token privileged to manage Data Streams.
        """
        stream = self._get_stream_client()
        task = stream.create_source(
            branch_id, name=name, source_type=source_type, description=description or None
        )
        finished = stream.wait_for_task(task)
        source_id = stream_task_source_id(finished) or name
        sink_bucket_id: str | None = None
        if provision_sinks and source_type == "otlp":
            provision_otlp_sinks(stream, branch_id, source_id)
            sink_bucket_id = f"{OTLP_BUCKET_PREFIX}{source_id}"
        source = stream.get_source(branch_id, source_id)
        return self._stream_source_detail(source, branch_id, sink_bucket_id)

    @staticmethod
    def _sink_bucket_from_sinks(sinks_raw: dict[str, Any]) -> str | None:
        """Return the bucket of the source's first table sink, or None if it has none.

        Derived from the ACTUAL sinks rather than assumed from the source type,
        so a source created with ``provision_sinks=False`` (or outside kbagent)
        truthfully reports ``None`` instead of a bucket that does not exist.
        """
        for sink in sinks_raw.get("sinks", []):
            table_id = (sink.get("table") or {}).get("tableId", "")
            if "." in table_id:
                return table_id.rsplit(".", 1)[0]
        return None

    def get_stream_source(
        self, source_id: str, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> dict[str, Any]:
        """Fetch one stream source's detail (endpoint + secret + sink bucket).

        ``sink_bucket_id`` reflects the source's ACTUAL sinks (``None`` when it
        has none -- e.g. created with ``provision_sinks=False`` or outside
        kbagent), so a caller never scopes a device token to a bucket that does
        not exist.
        """
        stream = self._get_stream_client()
        source = stream.get_source(branch_id, source_id)
        sinks = stream.list_sinks(branch_id, source.get("sourceId", source_id))
        return self._stream_source_detail(source, branch_id, self._sink_bucket_from_sinks(sinks))

    def list_stream_sources(
        self, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> list[dict[str, Any]]:
        """List the project's stream sources (raw source objects; find-or-create by name)."""
        raw = self._get_stream_client().list_sources(branch_id)
        return list(raw.get("sources", []))

    def delete_stream_source(
        self, source_id: str, *, branch_id: str = STREAM_DEFAULT_BRANCH
    ) -> None:
        """Delete a stream source (per-device revocation) -- async task polled to completion."""
        stream = self._get_stream_client()
        task = stream.delete_source(branch_id, source_id)
        stream.wait_for_task(task)
