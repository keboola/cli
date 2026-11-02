"""OAuth broker reads (identity verify, consumer get).

Only the read surface is exposed over REST. The oauth:manage token still
leaves the macOS keychain only after the login-password dialog on the host,
so a REST caller cannot read a consumer without a human answering it.
`identity add` (takes the token) and `rotate-azure-secret` (needs a code
typed at a terminal) are CLI-only. See
`plugins/kbagent/skills/kbagent/references/oauth-broker-workflow.md`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/identity", summary="Check the stored oauth:manage token of a stack")
def verify_identity(
    stack: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Keychain protection, scopes and broker access. Mirrors `kbagent oauth identity verify`."""
    return registry.oauth.verify_identity(stack)


@router.get("/consumers/{component_id}", summary="Get one broker consumer, secrets redacted")
def get_consumer(
    component_id: str,
    stack: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """appSecret and appSecretDocker show as <encrypted>. Mirrors `kbagent oauth consumer get`."""
    return registry.oauth.get_consumer(stack, component_id)
