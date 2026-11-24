"""The SDK facade shares the created-job error helpers without the service layer."""

import subprocess
import sys


def test_sdk_import_does_not_load_job_service() -> None:
    code = (
        "import sys, keboola_agent_cli.lib; "
        "print('keboola_agent_cli.services.job_service' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "False"


def test_job_service_and_sdk_share_one_helper() -> None:
    from keboola_agent_cli import job_wait_errors, lib
    from keboola_agent_cli.services import job_service

    assert job_service.created_job_error is job_wait_errors.created_job_error
    assert lib.created_job_error is job_wait_errors.created_job_error
