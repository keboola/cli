"""Column-level security endpoints -- the REST mirror of ``kbagent cls *``.

Built by :func:`.rls.build_policy_router`: same routes, permission gates and
bodies as the ``rls`` router, over the ``cls-policy`` object type (each rule
is ``{principal|principals, visible_columns}``, validated by the service).
"""

from __future__ import annotations

from .rls import build_policy_router

router = build_policy_router("cls", "CLS")
