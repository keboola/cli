"""Encryption endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/encrypt", tags=["encrypt"])


class EncryptRequest(BaseModel):
    project: str
    component_id: str
    values: dict[str, str]


@router.post("/values", summary="Encrypt secret values")
def encrypt_values(
    body: EncryptRequest, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Encrypt one or more values for a specific project + component pair.

    Returns the same keys with `KBC::ProjectSecure::...` ciphertexts. Use
    this before writing secret values into a configuration so they are
    never persisted in plaintext. Mirrors `kbagent encrypt values`.
    """
    return registry.encrypt.encrypt(
        alias=body.project,
        component_id=body.component_id,
        input_data=body.values,
    )
