"""Keboola Agent CLI - AI-friendly interface to Keboola projects."""

import importlib
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from .constants import APP_NAME

if TYPE_CHECKING:
    from .lib import Client, FileEntry, Files
    from .result_models import (
        CloneResult,
        ConfigDetailResult,
        JobResult,
        QueryResult,
        ScopedTokenResult,
        StreamSourceResult,
        SyncPushResult,
        TokenListEntryResult,
        UploadTableResult,
    )
    from .services.job_idempotency_store import JobIdempotencyStore

try:
    __version__ = version(APP_NAME)
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

# The SDK facade is imported on first attribute access (PEP 562), not at package
# import: every `kbagent` invocation imports this package, and loading the SDK
# (lib -> client -> models) here cost the CLI tens of milliseconds per run
# (issue #801). `from keboola_agent_cli import Client` works exactly as before,
# and the TYPE_CHECKING block above keeps the names visible to type checkers.
_LAZY_EXPORTS: dict[str, str] = {
    "Client": ".lib",
    "FileEntry": ".lib",
    "Files": ".lib",
    "CloneResult": ".result_models",
    "ConfigDetailResult": ".result_models",
    "JobResult": ".result_models",
    "QueryResult": ".result_models",
    "ScopedTokenResult": ".result_models",
    "StreamSourceResult": ".result_models",
    "SyncPushResult": ".result_models",
    "TokenListEntryResult": ".result_models",
    "UploadTableResult": ".result_models",
    "JobIdempotencyStore": ".services.job_idempotency_store",
}


def __getattr__(name: str) -> Any:
    """Resolve a public SDK name from its module on first access."""
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})


__all__ = [
    "Client",
    "CloneResult",
    "ConfigDetailResult",
    "FileEntry",
    "Files",
    "JobIdempotencyStore",
    "JobResult",
    "QueryResult",
    "ScopedTokenResult",
    "StreamSourceResult",
    "SyncPushResult",
    "TokenListEntryResult",
    "UploadTableResult",
    "__version__",
]
