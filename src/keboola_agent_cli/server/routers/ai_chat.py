"""Local AI chat endpoint (#300).

Backs the dashboard's Local AI tile -- a generic chat surface that spawns
the user's local ``claude`` / ``codex`` / ``gemini`` CLI with a meta-prompt
telling it "you are an AI co-pilot for kbagent; run `kbagent context`
first if you need command docs, then answer the user's question."

Distinct from:

- ``/agents/prompt/improve/stream`` -- rewrites a draft prompt into a
  polished single-shot prompt body.
- ``/workspaces/sql/improve/stream`` -- writes SQL grounded in a specific
  workspace context.

This endpoint is the freeform variant -- no output shape constraint, no
single-task framing. Used by the dashboard Local AI chat that replaces
the Kai tile for projects without a master Storage token (#291 wontfix
rationale).

Same SSE wire format as the other helpers (init / stdout / stderr /
done events) so the React side can reuse the streaming progress renderer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...constants import AI_CHAT_HELPER_TIMEOUT
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/ai", tags=["ai-chat"])


class AiChatRequest(BaseModel):
    """Input for the /ai/chat/stream endpoint.

    Single-shot: each request is independent. Conversation history is kept
    on the React side as scrollback; it is NOT forwarded to the AI on the
    next message (yet). Multi-turn with persisted history is tracked as
    a follow-up feature.
    """

    cli: str  # claude | codex | gemini -- same recipe as ai_agent runs
    message: str
    project: str | None = None
    branch_id: int | None = None
    extra_args: list[str] = []


def _sse(event: str, data: dict[str, Any]) -> bytes:
    """Encode a single SSE frame (event + data)."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


@router.post("/chat/stream", summary="Stream a local AI chat response")
async def chat_stream(
    body: AiChatRequest,
    registry: ServiceRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Stream a local-AI chat response back to the dashboard.

    Build a generic chat meta-prompt grounded in the user's active
    project / branch, hand it to the chosen CLI via
    ``stream_ai_agent_events``, and forward the SSE events through to
    the client. The final ``done`` event mirrors the shape used by
    other helpers; the React side renders the assistant's text +
    tool_use activity log in real time.
    """
    from ..agent_runner import (
        build_local_ai_meta_prompt,
        stream_ai_agent_events,
    )

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message must not be empty")

    meta_prompt = build_local_ai_meta_prompt(
        message=message,
        project=body.project,
        branch_id=body.branch_id,
        serve_url=getattr(registry, "serve_url", None),
    )
    params: dict[str, Any] = {
        "cli": body.cli,
        "prompt": meta_prompt,
        "extra_args": body.extra_args,
        "timeout": AI_CHAT_HELPER_TIMEOUT,
    }

    async def gen() -> AsyncIterator[bytes]:
        yield _sse(
            "init",
            {
                "kind": "local_ai_chat",
                "cli": body.cli,
                "project": body.project,
                "branch_id": body.branch_id,
                # Surface the full meta-prompt so the UI can offer a
                # "Show prompt" transparency panel identical to the SQL
                # helper's. Debugging is impossible without it.
                "meta_prompt": meta_prompt,
                "message_preview": message[:200],
            },
        )
        try:
            async for evt in stream_ai_agent_events(registry, params):
                yield _sse(evt["event"], evt["data"])
        except Exception as exc:
            # Catch-all: both ValueError (bad CLI / empty prompt) and any
            # unexpected error need to terminate the SSE stream with a
            # final ``done`` event so the React side doesn't hang on an
            # unterminated stream. Body shape is identical in both cases.
            yield _sse("done", {"status": "error", "error": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
