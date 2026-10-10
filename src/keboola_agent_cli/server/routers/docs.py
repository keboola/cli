"""Documentation Q&A endpoints (Keboola docs natural-language questions).

Route prefix is ``/documentation`` -- deliberately NOT ``/docs``: the bearer
auth middleware exempts every path starting with ``/docs`` (the Swagger UI
surface, see ``server/auth.py``), so a ``/docs``-prefixed router would ship
its endpoints unauthenticated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/documentation", tags=["documentation"])


class DocsQuery(BaseModel):
    query: str
    project: str | None = None


@router.post("/query", summary="Ask the Keboola documentation a question")
def query(body: DocsQuery, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Natural-language question answered from the official Keboola docs.

    Mirrors `kbagent docs query`. ``project`` selects which project's stack
    URL + token reach the AI Service. Omitted resolves through the
    default-project cascade (``--project`` > ``KBAGENT_PROJECT`` env >
    ``project use`` pin > sole project); the answer itself is
    project-independent.
    """
    return registry.docs.ask_docs(alias=body.project, query=body.query)
