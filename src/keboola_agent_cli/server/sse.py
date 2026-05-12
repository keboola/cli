"""SSE helpers for streaming endpoints.

Currently three streaming surfaces use SSE:
- Job logs (tail running job stdout/stderr)
- Job execution with ``--wait`` semantics (status transitions)
- Kai chat (token-by-token assistant response)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from sse_starlette.sse import EventSourceResponse


def json_event(data: Any, event: str = "message") -> dict[str, str]:
    """Build an SSE event dict for ``EventSourceResponse``."""
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


def sse_response(generator: AsyncIterator[dict[str, str]]) -> EventSourceResponse:
    """Wrap an async generator yielding event dicts."""
    return EventSourceResponse(generator)
