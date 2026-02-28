"""Lineage service - cross-project data flow analysis via bucket sharing.

Queries the Storage API for bucket sharing metadata and builds
a graph of data flow edges between projects. Fetches buckets from
all projects in parallel using BaseService._run_parallel().
"""

from typing import Any

from ..errors import KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService


class LineageService(BaseService):
    """Business logic for cross-project data lineage via bucket sharing.

    Queries each project's Storage API for bucket sharing metadata,
    classifies buckets as shared or linked, and builds a deduplicated
    set of data flow edges.

    Uses BaseService._run_parallel() for concurrent project fetching.
    """

    def _fetch_project_buckets(
        self, alias: str, project: ProjectConfig
    ) -> tuple[str, ProjectConfig, list[dict[str, Any]]] | tuple[str, dict[str, str]]:
        """Fetch buckets for a single project (runs in a worker thread).

        Creates its own KeboolaClient, fetches buckets, and closes the client.
        Returns either (alias, project, buckets) on success or (alias, error_dict)
        on failure. The client is always closed in the finally block.
        """
        client = self._client_factory(project.stack_url, project.token)
        try:
            buckets = client.list_buckets(include="linkedBuckets")
            return (alias, project, buckets)
        except KeboolaApiError as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        except Exception as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                },
            )
        finally:
            client.close()

    def get_lineage(self, aliases: list[str] | None = None) -> dict[str, Any]:
        """Analyze cross-project data lineage via bucket sharing.

        For each resolved project, fetches buckets with linkedBuckets info
        in parallel using BaseService._run_parallel(), classifies them as
        shared or linked, and builds deduplicated edges.

        Args:
            aliases: Project aliases to query. None means all projects.

        Returns:
            Dict with keys:
                - "edges": list of data flow edge dicts (sorted by source/target)
                - "shared_buckets": list of shared bucket dicts
                - "linked_buckets": list of linked bucket dicts
                - "summary": dict with counts
                - "errors": list of error dicts

        Raises:
            ConfigError: If a specified alias is not found.
        """
        projects = self.resolve_projects(aliases)

        # Early return for zero projects
        if not projects:
            return {
                "edges": [],
                "shared_buckets": [],
                "linked_buckets": [],
                "summary": {
                    "total_shared_buckets": 0,
                    "total_linked_buckets": 0,
                    "total_edges": 0,
                    "projects_queried": 0,
                    "projects_with_errors": 0,
                },
                "errors": [],
            }

        # Build project_id -> alias lookup for cross-referencing
        project_id_to_alias: dict[int, str] = {}
        for alias, project in projects.items():
            project_id_to_alias[project.project_id] = alias

        all_shared_buckets: list[dict[str, Any]] = []
        all_linked_buckets: list[dict[str, Any]] = []
        edges_by_key: dict[tuple[int, str, int, str], dict[str, Any]] = {}

        # Fetch buckets from all projects in parallel using BaseService._run_parallel()
        successes, errors = self._run_parallel(projects, self._fetch_project_buckets)

        for success in successes:
            alias, project, buckets = success
            self._process_buckets(
                buckets=buckets,
                alias=alias,
                project=project,
                project_id_to_alias=project_id_to_alias,
                all_shared_buckets=all_shared_buckets,
                all_linked_buckets=all_linked_buckets,
                edges_by_key=edges_by_key,
            )

        # Sort results for deterministic output (parallel execution order varies).
        # Use str() in sort keys: source_project_id comes from Pydantic (int) but
        # target_project_id comes from API linkedBy[].project.id which returns str.
        edges = sorted(
            edges_by_key.values(),
            key=lambda e: (
                str(e.get("source_project_id", "")),
                str(e.get("source_bucket_id", "")),
                str(e.get("target_project_id", "")),
                str(e.get("target_bucket_id", "")),
            ),
        )
        all_shared_buckets.sort(
            key=lambda b: (str(b.get("project_id", "")), str(b.get("bucket_id", "")))
        )
        all_linked_buckets.sort(
            key=lambda b: (str(b.get("project_id", "")), str(b.get("bucket_id", "")))
        )
        errors.sort(key=lambda e: e.get("project_alias", ""))

        summary = {
            "total_shared_buckets": len(all_shared_buckets),
            "total_linked_buckets": len(all_linked_buckets),
            "total_edges": len(edges),
            "projects_queried": len(projects),
            "projects_with_errors": len(errors),
        }

        return {
            "edges": edges,
            "shared_buckets": all_shared_buckets,
            "linked_buckets": all_linked_buckets,
            "summary": summary,
            "errors": errors,
        }

    def _process_buckets(
        self,
        buckets: list[dict[str, Any]],
        alias: str,
        project: ProjectConfig,
        project_id_to_alias: dict[int, str],
        all_shared_buckets: list[dict[str, Any]],
        all_linked_buckets: list[dict[str, Any]],
        edges_by_key: dict[tuple[int, str, int, str], dict[str, Any]],
    ) -> None:
        """Process buckets from a single project, extracting shared/linked info and edges."""
        for bucket in buckets:
            sharing = bucket.get("sharing")
            linked_by = bucket.get("linkedBy", [])
            source_bucket = bucket.get("sourceBucket")

            # Shared bucket: has sharing field
            if sharing:
                shared_info = {
                    "project_alias": alias,
                    "project_id": project.project_id,
                    "project_name": project.project_name,
                    "bucket_id": bucket.get("id", ""),
                    "bucket_name": bucket.get("name", bucket.get("displayName", "")),
                    "sharing_type": sharing,
                    "shared_by": bucket.get("sharedBy", {}),
                }
                all_shared_buckets.append(shared_info)

                # Each linkedBy entry = one edge from this project to target
                for link in linked_by:
                    target_project = link.get("project", {})
                    target_project_id = target_project.get("id", 0)
                    target_bucket_id = link.get("id", "")

                    edge_key = (
                        project.project_id,
                        bucket.get("id", ""),
                        target_project_id,
                        target_bucket_id,
                    )

                    edge = edges_by_key.get(edge_key, {})
                    edge.update(
                        {
                            "source_project_alias": alias,
                            "source_project_id": project.project_id,
                            "source_project_name": project.project_name,
                            "source_bucket_id": bucket.get("id", ""),
                            "source_bucket_name": bucket.get(
                                "name", bucket.get("displayName", "")
                            ),
                            "sharing_type": sharing,
                            "target_project_alias": project_id_to_alias.get(
                                target_project_id, ""
                            ),
                            "target_project_id": target_project_id,
                            "target_project_name": target_project.get("name", ""),
                            "target_bucket_id": target_bucket_id,
                        }
                    )
                    edges_by_key[edge_key] = edge

            # Linked bucket: has sourceBucket field
            if source_bucket:
                source_project = source_bucket.get("project", {})
                linked_info = {
                    "project_alias": alias,
                    "project_id": project.project_id,
                    "project_name": project.project_name,
                    "bucket_id": bucket.get("id", ""),
                    "bucket_name": bucket.get("name", bucket.get("displayName", "")),
                    "source_bucket_id": source_bucket.get("id", ""),
                    "source_project_id": source_project.get("id", 0),
                    "source_project_name": source_project.get("name", ""),
                    "is_readonly": bucket.get("isReadonly", False),
                }
                all_linked_buckets.append(linked_info)

                source_project_id = source_project.get("id", 0)
                source_bucket_id = source_bucket.get("id", "")

                edge_key = (
                    source_project_id,
                    source_bucket_id,
                    project.project_id,
                    bucket.get("id", ""),
                )

                edge = edges_by_key.get(edge_key, {})
                # Only set fields that are empty or missing
                if not edge.get("source_project_alias"):
                    edge["source_project_alias"] = project_id_to_alias.get(
                        source_project_id, ""
                    )
                if not edge.get("source_project_id"):
                    edge["source_project_id"] = source_project_id
                if not edge.get("source_project_name"):
                    edge["source_project_name"] = source_project.get("name", "")
                if not edge.get("source_bucket_id"):
                    edge["source_bucket_id"] = source_bucket_id
                # Always set target side from linked bucket
                edge["target_project_alias"] = alias
                edge["target_project_id"] = project.project_id
                edge["target_project_name"] = project.project_name
                edge["target_bucket_id"] = bucket.get("id", "")
                # Preserve sharing_type if already set from shared side
                if not edge.get("sharing_type"):
                    edge["sharing_type"] = ""

                edges_by_key[edge_key] = edge
