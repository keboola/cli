"""Server package.

Lazy attribute access via PEP 562 so importing helpers like
``from keboola_agent_cli.server.agents_store import AgentStore`` does NOT
drag in FastAPI/uvicorn. Those heavy deps are loaded only when
``create_app`` is actually fetched (typically by ``kbagent serve`` or
the FastAPI test suite).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["create_app"]

if TYPE_CHECKING:
    # Imported for type checkers only -- never executed at runtime, so
    # FastAPI stays absent from the CLI-only path.
    from .app import create_app


def __getattr__(name: str) -> Any:
    """PEP 562 lazy export of FastAPI-dependent symbols.

    Tests + the ``serve`` command can still use the canonical
    ``from keboola_agent_cli.server import create_app`` form, but importing
    pure-logic siblings (``agents_store``, ``agent_runner``, ``pricing``)
    no longer pays the FastAPI import tax.
    """
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module 'keboola_agent_cli.server' has no attribute {name!r}")
