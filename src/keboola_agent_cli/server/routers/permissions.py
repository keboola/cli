"""Read-only discovery of the firewall policy the REST surface enforces.

Issue #655 asked for this alongside enforcement, and the two belong together:
once a route can answer 403 PERMISSION_DENIED, a client needs a way to learn
the rules *before* it starts making calls it is not allowed to make. Without
it the only discovery channel is trial and error against destructive routes.

There is deliberately no write counterpart. ``permissions set`` / ``reset``
edit ``config.json`` on the host, and letting a bearer token widen the very
policy that constrains it would make the firewall self-defeating -- a policy
change stays a terminal action on the machine running the server.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...permissions import (
    INERT_PATTERN_HINT,
    INERT_SINCE_VERSION,
    PermissionEngine,
    find_inert_patterns,
)
from ..dependencies import get_permission_engine

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.get("/show", summary="Show the active permission policy")
def show(engine: PermissionEngine = Depends(get_permission_engine)) -> dict[str, Any]:
    """Report the policy every route on this server is checked against.

    Mirrors `kbagent permissions show --json`, with one deliberate difference:
    the CLI reports the persisted policy and the session ``--deny-*`` flags as
    two separate layers, while this returns the single EFFECTIVE policy the
    server enforces -- ``create_app`` merges the two at startup and a REST
    caller can neither see nor change the flags the daemon was launched with.

    ``active`` is False when no policy is configured at all; ``policy`` is then
    null and every operation is allowed.
    """
    policy = engine.policy
    payload: dict[str, Any] = {
        "active": policy is not None,
        "policy": (
            None
            if policy is None
            else {"mode": policy.mode, "allow": list(policy.allow), "deny": list(policy.deny)}
        ),
    }

    # Additive keys, present only when the policy carries rules that can no
    # longer match anything -- same wording the CLI and `doctor` use, so a
    # client cannot learn a third phrasing of one problem.
    inert = find_inert_patterns(policy)
    if inert:
        payload["inert_patterns"] = inert
        payload["inert_since_version"] = INERT_SINCE_VERSION
        payload["inert_hint"] = INERT_PATTERN_HINT

    return payload
