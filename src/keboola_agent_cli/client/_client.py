"""Composition of the Keboola API client from its endpoint-family mixins.

``KeboolaClient`` is assembled here from the per-family mixins (storage tables,
storage files, configs, queue, tokens, branches, stream, query, workspaces,
billing, misc) over the shared ``_CoreClient`` plumbing base. It stays a
single class exposing every Storage/Queue method at its original signature,
so ``keboola_agent_cli.Client`` and its ``.raw`` accessor are unaffected by
the split of the former single-file ``client.py`` into a package (issue #520).

Inherits shared retry/error logic from BaseHttpClient (via _CoreClient).
"""

import httpx

from ._core import _CoreClient
from .billing import _BillingMixin
from .branches import _BranchesMixin
from .configs import _ConfigsMixin
from .misc import _MiscMixin
from .query import _QueryMixin
from .queue import _QueueMixin
from .storage_files import _StorageFilesMixin
from .storage_tables import _StorageTablesMixin
from .stream import _StreamMixin
from .tokens import _TokensMixin
from .workspaces import _WorkspacesMixin


class KeboolaClient(
    _StorageTablesMixin,
    _StorageFilesMixin,
    _ConfigsMixin,
    _QueueMixin,
    _TokensMixin,
    _BranchesMixin,
    _StreamMixin,
    _QueryMixin,
    _WorkspacesMixin,
    _BillingMixin,
    _MiscMixin,
    _CoreClient,
):
    """HTTP client for the Keboola Storage API and Queue API.

    Provides methods to interact with Keboola endpoints with built-in
    retry logic (exponential backoff for 429/5xx), timeouts, and
    automatic token masking in error messages.

    Inherits _do_request() and _raise_api_error() from BaseHttpClient.
    """

    def __init__(self, stack_url: str, token: str, *, http_auth: httpx.Auth | None = None) -> None:
        """Construct the client.

        ``http_auth`` is additive and keyword-only: omitting it (the
        default) is byte-identical to the client's behaviour before session
        (bearer) auth existed. When set, it is forwarded to ``_CoreClient``,
        which then omits ``X-StorageApi-Token`` so the sentinel session
        token never goes on the wire as a header value.
        """
        super().__init__(stack_url, token, http_auth=http_auth)
