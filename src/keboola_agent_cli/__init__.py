"""Keboola Agent CLI - AI-friendly interface to Keboola projects."""

from importlib.metadata import PackageNotFoundError, version

from .constants import APP_NAME
from .lib import Client, FileEntry, Files
from .result_models import (
    ConfigDetailResult,
    JobResult,
    QueryResult,
    SyncPushResult,
    UploadTableResult,
)

try:
    __version__ = version(APP_NAME)
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = [
    "Client",
    "ConfigDetailResult",
    "FileEntry",
    "Files",
    "JobResult",
    "QueryResult",
    "SyncPushResult",
    "UploadTableResult",
    "__version__",
]
