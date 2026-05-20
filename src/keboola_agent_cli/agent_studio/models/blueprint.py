"""Blueprint — a curated, forkable Playbook template.

In `docs/agents-v2.md` the entity is called a *Solution* (§ 12) and
carries a Problem / What-it-does / Expected-impact narrative plus an
embedded ``playbook_template``. The UI surface and the renamed term is
*Blueprint* (see `docs/mockups/README.md` "Earlier names").

Phase 1 ships a read-only catalogue: the cards from
`docs/mockups/02-blueprints-catalog.png`, served from a static
in-code seed (``agent_studio.blueprints_catalog``). Forking a
Blueprint mints a new Playbook prefilled with the blueprint's
connections / skills / plugins. The full ``playbook_template`` (SOP
steps, budget, approval policy) lands when those Playbook
substructures exist.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Catalogue categories — these drive the filter chips on the Blueprints
# page (`docs/mockups/02-blueprints-catalog.png`). Kept as a plain
# tuple (not an enum) because they are display strings, surfaced
# verbatim in the UI.
BLUEPRINT_CATEGORIES: tuple[str, ...] = (
    "Data Cleanup",
    "Process Mining",
    "Decision Analysis",
    "Decision Triggers",
    "Custom Agent Builder",
)


class Blueprint(BaseModel):
    """A vertical Playbook template, packaged for browsing + forking."""

    id: str = Field(..., description="Stable slug, e.g. 'cross-source-crm-cleanup'.")
    name: str
    category: str = Field(..., description="One of BLUEPRINT_CATEGORIES; drives the filter chips.")
    description: str = Field(..., description="One/two-line blurb shown on the catalogue card.")
    systems: list[str] = Field(
        default_factory=list,
        description=(
            "Human-facing 'Systems:' caption on the card — the Keboola "
            "components / external services this blueprint touches "
            "(e.g. 'keboola.ex-salesforce')."
        ),
    )
    connections: list[str] = Field(
        default_factory=list,
        description="Connection IDs prefilled into a forked Playbook.",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Skill IDs prefilled into a forked Playbook.",
    )
    plugins: list[str] = Field(
        default_factory=list,
        description="Plugin IDs prefilled into a forked Playbook.",
    )
