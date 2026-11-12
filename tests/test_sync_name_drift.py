"""``sync push`` name-drift warnings and the dirs of same-named configs.

``sync pull`` gives the second of two same-named configs the dir
``<name>-<id8>`` (``<name>-<id8>-<n>`` when that is taken too). That dir
matches the config name, so push must not report it as drift on every run.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.services._sync_pull_dirs import free_path
from keboola_agent_cli.services._sync_push_ops import detect_name_drift
from keboola_agent_cli.sync.manifest import ManifestConfiguration
from keboola_agent_cli.sync.naming import sanitize_name

CONFIG_ID = "bbbb2222-second"


def _drift(tmp_path: Path, dirname: str, config_name: str) -> list[dict[str, str]]:
    service = MagicMock()
    service._find_branch_path.return_value = "main"
    service._read_config_file.return_value = {"name": config_name}
    manifest = MagicMock()
    manifest.configurations = [
        ManifestConfiguration(
            branchId=1,
            componentId="keboola.scheduler",
            id=CONFIG_ID,
            path=f"other/keboola.scheduler/{dirname}",
            metadata={},
        )
    ]
    return detect_name_drift(service, manifest, tmp_path)


@pytest.mark.parametrize(
    "dirname",
    [
        "schedule-for-flow",
        "schedule-for-flow-bbbb2222",
        "schedule-for-flow-bbbb2222-2",
        "schedule-for-flow-bbbb2222-13",
    ],
)
def test_dir_of_a_same_named_config_is_no_drift(tmp_path: Path, dirname: str) -> None:
    assert _drift(tmp_path, dirname, "Schedule for flow") == []


@pytest.mark.parametrize("taken_count", [1, 2, 3])
@pytest.mark.parametrize("config_name", ["Schedule for flow", "Long name " * 20])
def test_dir_that_pull_allocates_is_no_drift(
    tmp_path: Path, config_name: str, taken_count: int
) -> None:
    """The names come from ``free_path`` itself, so the two cannot drift apart."""
    base = sanitize_name(config_name)
    taken = {base}
    for _ in range(taken_count - 1):
        taken.add(free_path(base, CONFIG_ID[:8], taken))
    dirname = free_path(base, CONFIG_ID[:8], taken)
    assert dirname != base
    assert _drift(tmp_path, dirname, config_name) == []


@pytest.mark.parametrize(
    "dirname",
    [
        "old-schedule-name",
        "schedule-for-flow-cccc3333",
        "schedule-for-flow-bbbb2222-x",
        "schedule-for-flow-bbbb2222-²",
        "schedule-for-flow-2",
    ],
)
def test_other_dir_names_are_drift(tmp_path: Path, dirname: str) -> None:
    warnings = _drift(tmp_path, dirname, "Schedule for flow")
    assert [(w["local_dirname"], w["expected_dirname"]) for w in warnings] == [
        (dirname, "schedule-for-flow")
    ]
