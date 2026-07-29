"""Bundled flow resources: example configurations + JSON Schemas.

Vendored from upstream sources (issue #397 -- port of the keboola-mcp-server
``get_flow_examples`` tool + authoritative schema bundling):

- ``conditional_flow_examples.jsonl`` / ``legacy_flow_examples.jsonl``:
  verbatim copies of ``src/keboola_mcp_server/resources/flow_examples/*`` from
  https://github.com/keboola/mcp-server (fetched 2026-07-20). One JSON flow
  configuration per line.
- ``conditional-flow-schema.json``: the live ``keboola.flow``
  ``configurationSchema`` snapshot taken from the public Storage component
  index (``GET https://connection.keboola.com/v2/storage``) on 2026-07-20.
  This is the same document ``kbagent flow schema --full --project ALIAS``
  fetches live; the bundled copy is the offline fallback. It is NEWER than
  the (since-removed) mcp-server bundled copy -- upstream deleted theirs in
  favour of live fetching precisely because snapshots drift, so refresh this
  file from the index when flow features change.
- ``flow-schema.json``: the legacy ``keboola.orchestrator`` schema, verbatim
  from ``src/keboola_mcp_server/resources/flow-schema.json`` (still bundled
  upstream; orchestrator is frozen so drift risk is nil). kbagent cannot
  create or edit orchestrator flows (dropped in 0.57.0) -- this schema and
  the legacy examples are informational only.
"""
