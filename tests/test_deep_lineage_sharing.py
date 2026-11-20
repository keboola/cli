"""Cross-project lineage regression tests for sync exports (#1029)."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.services._sync_storage import write_storage_metadata
from keboola_agent_cli.services.deep_lineage_service import DeepLineageService

SOURCE_BUCKET = "out.c-published"
LINKED_BUCKET = "in.c-renamed"
SOURCE = f"producer:{SOURCE_BUCKET}.orders"
TARGET = f"consumer:{LINKED_BUCKET}.orders"


def _sync_project(
    root: Path,
    alias: str,
    project_id: int,
    bucket: dict[str, Any],
    names: list[str],
    *,
    api_host: str = "connection.example.com",
) -> Path:
    project = root / alias
    manifest = project / ".keboola" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"project": {"id": project_id, "apiHost": api_host}}), encoding="utf-8"
    )
    bucket_id = bucket["id"]
    tables = [
        {"id": f"{bucket_id}.{name}", "name": name, "bucketId": bucket_id, "columns": ["id"]}
        for name in names
    ]
    write_storage_metadata(project, [bucket], tables, {})
    return project / "storage" / "buckets.json"


@pytest.fixture()
def linked_projects(tmp_path: Path) -> Path:
    _sync_project(
        tmp_path,
        "producer",
        11,
        {"id": SOURCE_BUCKET, "sharing": "organization"},
        ["orders", "source_only"],
    )
    return _sync_project(
        tmp_path,
        "consumer",
        22,
        {
            "id": LINKED_BUCKET,
            "sourceBucket": {"id": SOURCE_BUCKET, "project": {"id": "11"}},
        },
        ["orders", "target_only"],
    )


def test_build_and_show_use_synced_links_without_registered_projects(
    linked_projects: Path, tmp_path: Path
) -> None:
    # Same bucket and table without a link, on a different stack with a colliding project ID.
    _sync_project(
        tmp_path,
        "unrelated",
        11,
        {"id": SOURCE_BUCKET},
        ["orders", "target_only"],
        api_host="another-stack.example.com",
    )
    runner = CliRunner()
    cache = tmp_path / "lineage.json"
    build = runner.invoke(
        app,
        ["--json", "lineage", "build", "-d", str(tmp_path), "-o", str(cache)],
    )

    assert build.exit_code == 0, build.output
    data = json.loads(build.output)["data"]
    assert data == json.loads(cache.read_text(encoding="utf-8"))
    assert data["warnings"] == []
    assert data["summary"]["detection_methods"] == {"bucket_sharing": 1}
    edge = data["edges"][0]
    assert (edge["source_fqn"], edge["target_fqn"]) == (SOURCE, TARGET)
    assert edge["edge_type"] == "cross_project_share"
    assert edge["columns"] == ["id"]

    shown = runner.invoke(
        app, ["--json", "lineage", "show", "--file", str(cache), "--upstream", TARGET]
    )
    assert shown.exit_code == 0, shown.output
    edges = json.loads(shown.output)["data"]["edges"]
    assert [(e["source"], e["target"]) for e in edges] == [(SOURCE, TARGET)]


@pytest.mark.parametrize("contents", [None, "{", "[]", '[{"id": "in.c-renamed"}]'])
def test_incomplete_sharing_metadata_warns_without_failing_build(
    linked_projects: Path, tmp_path: Path, config_store: ConfigStore, contents: str | None
) -> None:
    if contents is None:
        linked_projects.unlink()
    else:
        linked_projects.write_text(contents, encoding="utf-8")

    data = DeepLineageService(config_store).build_lineage(tmp_path)

    assert data["summary"]["tables"] == 4
    assert data["summary"]["edges"] == 0
    (warning,) = data["warnings"]
    assert "project 'consumer'" in warning
    assert "sync pull --all-projects" in warning


def test_malformed_bucket_does_not_drop_valid_links(
    linked_projects: Path, tmp_path: Path, config_store: ConfigStore
) -> None:
    records = json.loads(linked_projects.read_text(encoding="utf-8"))
    records.append({"id": "other", "source_bucket": {"bucket_id": SOURCE_BUCKET}})
    linked_projects.write_text(json.dumps(records), encoding="utf-8")

    data = DeepLineageService(config_store).build_lineage(tmp_path)

    assert data["summary"]["detection_methods"] == {"bucket_sharing": 1}
    assert len(data["warnings"]) == 1
