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


@router.get("/ping")
def ping(
    project: str | None = None, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.kai.ping(registry.kai.resolve_alias(project))


@router.post("/ask")
def ask(body: KaiMessage, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.kai.ask(alias=registry.kai.resolve_alias(body.project), message=body.message)


@router.post("/chat")
def chat(body: KaiMessage, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.kai.chat_message(
        alias=registry.kai.resolve_alias(body.project),
        message=body.message,
        chat_id=body.chat_id,
    )


@router.get("/history")
def history(
    project: str | None = None,
    limit: int = 10,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.kai.get_history(alias=registry.kai.resolve_alias(project), limit=limit)
