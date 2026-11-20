"""Restore cross-project table edges from the synced bucket snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..constants import STORAGE_BUCKETS_FILENAME, STORAGE_DIR_NAME

if TYPE_CHECKING:
    from .deep_lineage_service import LineageGraph, Table


@dataclass(frozen=True)
class SyncedProject:
    directory: Path
    alias: str
    project_id: int
    api_host: str


@dataclass(frozen=True)
class BucketLink:
    source_project_id: str
    source_bucket_id: str
    target_bucket_id: str


def _read_links(project: SyncedProject, warnings: list[str]) -> list[BucketLink]:
    buckets_file = project.directory / STORAGE_DIR_NAME / STORAGE_BUCKETS_FILENAME
    warning = (
        f"Bucket sharing metadata for project '{project.alias}' is missing, outdated, or invalid "
        f"in {buckets_file}; cross-project lineage may be incomplete. "
        "Run 'kbagent sync pull --all-projects' with storage metadata enabled and rebuild."
    )
    try:
        records = json.loads(buckets_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        warnings.append(warning)
        return []
    if not isinstance(records, list) or not records:
        warnings.append(warning)
        return []

    links: list[BucketLink] = []
    incomplete = False
    for record in records:
        if not isinstance(record, dict) or "source_bucket" not in record:
            incomplete = True
            continue
        source = record["source_bucket"]
        if source is None:
            continue
        source_project_id = str(source.get("project_id", "")) if isinstance(source, dict) else ""
        source_bucket_id = source.get("bucket_id") if isinstance(source, dict) else None
        target_bucket_id = record.get("id")
        if (
            not source_project_id.isdecimal()
            or not isinstance(source_bucket_id, str)
            or not source_bucket_id
            or not isinstance(target_bucket_id, str)
            or not target_bucket_id
        ):
            incomplete = True
            continue
        links.append(BucketLink(source_project_id, source_bucket_id, target_bucket_id))
    if incomplete:
        warnings.append(warning)
    return list(dict.fromkeys(links))


def add_sharing_edges(graph: LineageGraph, warnings: list[str]) -> None:
    """Connect same-name tables from the source bucket to the linked bucket."""
    projects = graph._synced_projects_with_tables
    if len(projects) < 2:
        return

    from .deep_lineage_service import Edge

    table_index: dict[tuple[str, str], dict[str, Table]] = {}
    for table in graph.tables.values():
        table_index.setdefault((table.project_alias, table.bucket_id), {})[table.name] = table
    aliases = {(p.api_host, str(p.project_id)): p.alias for p in projects if p.project_id}
    for project in projects:
        for link in _read_links(project, warnings):
            source_alias = aliases.get((project.api_host, link.source_project_id))
            if source_alias is None or source_alias == project.alias:
                continue
            source_tables = table_index.get((source_alias, link.source_bucket_id), {})
            target_tables = table_index.get((project.alias, link.target_bucket_id), {})
            for name, target_table in target_tables.items():
                if (source_table := source_tables.get(name)) is not None:
                    graph.add_edge(
                        Edge(
                            source_fqn=source_table.fqn,
                            target_fqn=target_table.fqn,
                            source_type="table",
                            target_type="table",
                            edge_type="cross_project_share",
                            detection="bucket_sharing",
                            columns=source_table.columns,
                        )
                    )
