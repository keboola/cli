"""`job run` refuses `keboola.data-apps`: an app is started via the Data Science API."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.job_service import JobService


def test_service_refuses_data_apps_without_creating_a_job(tmp_config_dir: Path) -> None:
    store = setup_single_project(tmp_config_dir)
    client = MagicMock()
    service = JobService(config_store=store, client_factory=lambda url, token: client)

    with pytest.raises(KeboolaApiError) as exc_info:
        service.run_job("prod", "keboola.data-apps", "123")

    assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
    assert "kbagent data-app start" in exc_info.value.message
    assert "keboola.data-app-control" in exc_info.value.message
    client.create_job.assert_not_called()
