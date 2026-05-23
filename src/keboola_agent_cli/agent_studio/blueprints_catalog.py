"""Static Blueprint catalogue for Phase 1.

These are the cards from `docs/mockups/02-blueprints-catalog.png`,
seeded in code rather than loaded from YAML files. The v2 PRD § 11/§ 12
says Blueprints/Solutions should ultimately be data files
(``plugins/agent-studio-solutions/*.yaml``) so a marketplace can ship
them — that loader is a later slice. For now an in-code seed keeps the
catalogue endpoint dependency-free and lets the UI render the real
designed content.

Keeping the seed as a module-level tuple (not read from disk) means
the catalogue is identical across every install and cannot be
corrupted by a stray file, which is the right trade-off until the
marketplace exists.
"""

from __future__ import annotations

from .models.blueprint import Blueprint

# Order matches the 3x3 grid in the mockup, reading left-to-right,
# top-to-bottom.
BLUEPRINTS: tuple[Blueprint, ...] = (
    Blueprint(
        id="cross-source-crm-cleanup",
        name="Cross-source CRM Cleanup",
        category="Data Cleanup",
        description="Reconciles contact records across Salesforce, HubSpot, Zendesk.",
        systems=[
            "keboola.ex-salesforce",
            "keboola.ex-hubspot-crm",
            "keboola.ex-zendesk-v3",
        ],
        connections=["keboola.connection"],
        skills=["entity-resolution", "schema-normalization", "data-quality-profiling"],
        plugins=["keboola.data-cleanup"],
    ),
    Blueprint(
        id="schema-normalisation-lite",
        name="Schema Normalisation Lite",
        category="Data Cleanup",
        description="Normalises raw extractor output into the bronze layer.",
        systems=["keboola.ex-postgres", "keboola.wr-snowflake"],
        connections=["keboola.connection"],
        skills=["schema-normalization"],
        plugins=["keboola.data-cleanup"],
    ),
    Blueprint(
        id="daily-ar-deductions",
        name="Daily AR Deductions",
        category="Decision Triggers",
        description="Drafts Slack message + Jira ticket for AR disputes.",
        systems=["keboola.ex-netsuite", "slack", "jira"],
        connections=["keboola.connection", "slack"],
        skills=["deduction-classification", "dunning-letter-draft"],
        plugins=["keboola.decision-analysis", "keboola.decision-trigger"],
    ),
    Blueprint(
        id="support-ticket-triage",
        name="Support Ticket Triage",
        category="Process Mining",
        description="Classifies inbound tickets and routes to the right queue.",
        systems=["keboola.ex-zendesk-v3", "keboola.ex-slack"],
        connections=["keboola.connection"],
        skills=["process-discovery", "bottleneck-analysis"],
        plugins=["keboola.process-mining"],
    ),
    Blueprint(
        id="quote-to-cash-process-map",
        name="Quote-to-Cash Process Map",
        category="Process Mining",
        description="Reconstructs and visualises the QTC flow from CRM + ERP.",
        systems=["keboola.ex-salesforce", "keboola.ex-netsuite"],
        connections=["keboola.connection"],
        skills=["process-discovery", "conformance-checking"],
        plugins=["keboola.process-mining"],
    ),
    Blueprint(
        id="q3-cohort-analysis",
        name="Q3 Cohort Analysis",
        category="Decision Analysis",
        description="Cohort retention + LTV across the past 4 quarters.",
        systems=["keboola.ex-stripe", "keboola.ex-ga4"],
        connections=["keboola.connection"],
        skills=["kpi-calculation", "trend-analysis"],
        plugins=["keboola.decision-analysis"],
    ),
    Blueprint(
        id="lead-enrichment",
        name="Lead Enrichment",
        category="Decision Triggers",
        description="Enriches inbound leads with firmographic + intent data.",
        systems=["keboola.ex-salesforce", "clearbit"],
        connections=["keboola.connection", "salesforce"],
        skills=["decision-rules-engine"],
        plugins=["keboola.decision-trigger"],
    ),
    Blueprint(
        id="daily-sales-pipeline-report",
        name="Daily Sales Pipeline Report",
        category="Decision Analysis",
        description="Generates the morning sales pipeline brief.",
        systems=["keboola.ex-salesforce", "slack"],
        connections=["keboola.connection", "slack"],
        skills=["kpi-calculation", "anomaly-detection"],
        plugins=["keboola.decision-analysis"],
    ),
    Blueprint(
        id="custom-agent",
        name="Custom Agent",
        category="Custom Agent Builder",
        description="Empty scaffold. Describe what you want in plain English.",
        systems=["pick at runtime"],
        connections=[],
        skills=[],
        plugins=["kbagent.playbook-builder"],
    ),
)

_BY_ID: dict[str, Blueprint] = {bp.id: bp for bp in BLUEPRINTS}


def list_blueprints(category: str | None = None) -> list[Blueprint]:
    """Return the catalogue, optionally filtered to one category."""

    if category is None or category == "All":
        return list(BLUEPRINTS)
    return [bp for bp in BLUEPRINTS if bp.category == category]


def get_blueprint(blueprint_id: str) -> Blueprint | None:
    """Look up one Blueprint by slug. ``None`` when unknown."""

    return _BY_ID.get(blueprint_id)
