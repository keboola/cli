"""Kai (Keboola AI) chat endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/kai", tags=["kai"])


class KaiMessage(BaseModel):
    message: str
    chat_id: str | None = None
    project: str | None = None


@router.get("/ping", summary="Kai liveness probe")
def ping(
    project: str | None = None, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Quick reachability check against the Kai endpoint for `project`."""
    return registry.kai.ping(registry.kai.resolve_alias(project))


@router.get("/preflight", summary="Inspect Kai readiness")
def preflight(
    project: str | None = None, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Inspect the configured token's Kai readiness without raising.

    Used by the UI to render a single, informative warning ('use the master
    "owner" token + enable AI Agent Chat') instead of letting /ping or /chat
    blow up with KAI_NOT_ENABLED on every interaction.
    """
    return registry.kai.preflight(registry.kai.resolve_alias(project))


@router.post("/ask", summary="Single-shot Kai question")
def ask(body: KaiMessage, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Stateless one-off question (no chat history). Mirrors `kbagent kai ask`."""
    return registry.kai.ask(alias=registry.kai.resolve_alias(body.project), message=body.message)


@router.post("/chat", summary="Continue a Kai chat")
def chat(body: KaiMessage, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Send a message in a stateful chat (omit `chat_id` to start a new one).
    Mirrors `kbagent kai chat`.
    """
    return registry.kai.chat_message(
        alias=registry.kai.resolve_alias(body.project),
        message=body.message,
        chat_id=body.chat_id,
    )


@router.get("/history", summary="List recent Kai chats")
def history(
    project: str | None = None,
    limit: int = 10,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List the most recent `limit` chats for `project`. Mirrors
    `kbagent kai history`.
    """
    return registry.kai.get_history(alias=registry.kai.resolve_alias(project), limit=limit)


@router.get("/chat/{chat_id}", summary="Replay one chat")
def chat_detail(
    chat_id: str,
    project: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch the full message history for one chat (used to restore a
    conversation after the user navigates away and back)."""
    return registry.kai.get_chat_detail(alias=registry.kai.resolve_alias(project), chat_id=chat_id)
