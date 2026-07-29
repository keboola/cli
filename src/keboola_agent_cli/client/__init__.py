"""Keboola API client package.

The Keboola Storage + Queue API client used to live in a single ``client.py``
module. It was split into this package by endpoint family (issue #520) once it
crossed CONTRIBUTING.md's 2,000-line hard ceiling.

The split is pure movement: ``KeboolaClient`` stays one class, composed from
per-family mixins, and every name that used to be importable from the old
module is re-exported here so existing importers keep working unchanged.
``time``, ``QUERY_RESULTS_PAGE_SIZE`` and ``_CloudDownloader`` are re-exported
too because existing tests reach them as ``keboola_agent_cli.client.<name>``
(patch targets and a page-size assertion).
"""

import time  # noqa: F401  -- re-exported: tests patch keboola_agent_cli.client.time.{sleep,monotonic}

from ..constants import QUERY_RESULTS_PAGE_SIZE
from ._client import KeboolaClient
from ._transfer import (
    InlineQueryResult,
    _assert_safe_download_url,
    _build_abs_upload_url,
    _CloudDownloader,
    _collect_inline_results,
    _extract_cloud_error_code,
    _extract_query_job_error,
    _iter_poll_intervals,
    _unwrap_bigquery_error,
)

__all__ = [
    "QUERY_RESULTS_PAGE_SIZE",
    "InlineQueryResult",
    "KeboolaClient",
    "_CloudDownloader",
    "_assert_safe_download_url",
    "_build_abs_upload_url",
    "_collect_inline_results",
    "_extract_cloud_error_code",
    "_extract_query_job_error",
    "_iter_poll_intervals",
    "_unwrap_bigquery_error",
]
