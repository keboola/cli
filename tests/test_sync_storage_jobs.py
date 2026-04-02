"""Tests for sync storage metadata, per-config jobs, and related client methods.

Covers:
- Client: list_jobs_grouped(), list_buckets_with_metadata(),
  list_tables_with_metadata(), get_table_data_preview()
- SyncService: _write_storage_metadata(), _write_per_config_jobs(),
  _mask_encrypted_columns(), _fetch_samples()
- Integration: pull() with no_storage, no_jobs flags
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from helpers import setup_single_project
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.constants import (
    ENCRYPTED_COLUMN_MASK,
    JOBS_FILENAME,
    STORAGE_BUCKETS_FILENAME,
    STORAGE_DIR_NAME,
)
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import ManifestConfiguration

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

SAMPLE_VERIFY_TOKEN = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)

SAMPLE_BRANCHES = [
    {"id": 12345, "name": "Main", "isDefault": True},
]

SAMPLE_COMPONENTS_SIMPLE = [
    {
        "id": "keboola.ex-http",
        "type": "extractor",
        "configurations": [
            {
                "id": "cfg-001",
                "name": "My HTTP Extractor",
                "description": "Fetches data",
                "configuration": {
                    "parameters": {"baseUrl": "https://api.example.com"},
                },
                "rows": [],
            }
        ],
    },
    {
        "id": "keboola.snowflake-transformation",
        "type": "transformation",
        "configurations": [
            {
                "id": "cfg-002",
                "name": "Clean Data",
                "description": "Cleans raw data",
                "configuration": {"parameters": {}},
                "rows": [],
            }
        ],
    },
]

SAMPLE_BUCKETS_API = [
    {
        "id": "in.c-data",
        "name": "c-data",
        "stage": "in",
        "description": "Input data bucket",
        "tablesCount": 3,
        "dataSizeBytes": 1024000,
        "metadata": [{"key": "owner", "value": "team-a"}],
    },
    {
        "id": "out.c-results",
        "name": "c-results",
        "stage": "out",
        "description": "",
        "tablesCount": 1,
        "dataSizeBytes": 512,
        "metadata": [],
    },
]

SAMPLE_TABLES_API = [
    {
        "id": "in.c-data.users",
        "name": "users",
        "bucket": {"id": "in.c-data"},
        "primaryKey": ["id"],
        "columns": ["id", "name", "email"],
        "rowsCount": 5000,
        "dataSizeBytes": 204800,
        "lastImportDate": "2026-03-20T10:00:00Z",
        "lastChangeDate": "2026-03-20T10:00:00Z",
        "description": "User accounts",
        "metadata": [],
        "columnMetadata": {},
    },
    {
        "id": "in.c-data.orders",
        "name": "orders",
        "bucket": {"id": "in.c-data"},
        "primaryKey": ["order_id"],
        "columns": ["order_id", "user_id", "amount"],
        "rowsCount": 50000,
        "dataSizeBytes": 2048000,
        "lastImportDate": "2026-03-21T08:00:00Z",
        "lastChangeDate": "2026-03-21T08:00:00Z",
        "description": "",
        "metadata": [],
        "columnMetadata": {},
    },
    {
        "id": "out.c-results.summary",
        "name": "summary",
        "bucket": {"id": "out.c-results"},
        "primaryKey": [],
        "columns": ["metric", "value"],
        "rowsCount": 10,
        "dataSizeBytes": 256,
        "lastImportDate": "2026-03-19T12:00:00Z",
        "lastChangeDate": "2026-03-19T12:00:00Z",
        "description": "Summary stats",
        "metadata": [],
        "columnMetadata": {},
    },
]

SAMPLE_GROUPED_JOBS = [
    {
        "group": {"componentId": "keboola.ex-http", "configId": "cfg-001"},
        "jobs": [
            {
                "id": 1001,
                "status": "success",
                "startTime": "2026-03-21T10:00:00Z",
                "endTime": "2026-03-21T10:05:00Z",
                "durationSeconds": 300,
                "mode": "run",
            },
            {
                "id": 1002,
                "status": "error",
                "startTime": "2026-03-20T10:00:00Z",
                "endTime": "2026-03-20T10:01:00Z",
                "durationSeconds": 60,
                "mode": "run",
                "result": {"message": "Connection timeout"},
            },
        ],
    },
    {
        "group": {"componentId": "keboola.snowflake-transformation", "configId": "cfg-002"},
        "jobs": [
            {
                "id": 2001,
                "status": "success",
                "startTime": "2026-03-21T11:00:00Z",
                "endTime": "2026-03-21T11:10:00Z",
                "durationSeconds": 600,
                "mode": "debug",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Mock client factory
# ---------------------------------------------------------------------------


def _make_sync_mock_client(
    verify_token_response: TokenVerifyResponse | None = None,
    components_response: list | None = None,
    branches_response: list | None = None,
    buckets_response: list | None = None,
    tables_response: list | None = None,
    jobs_grouped_response: list | None = None,
) -> MagicMock:
    """Create a mock KeboolaClient suitable for sync tests with storage/jobs."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    if verify_token_response:
        client.verify_token.return_value = verify_token_response

    if components_response is not None:
        client.list_components_with_configs.return_value = components_response

    if branches_response is not None:
        client.list_dev_branches.return_value = branches_response

    if buckets_response is not None:
        client.list_buckets_with_metadata.return_value = buckets_response
    else:
        # Default: return empty so pull doesn't fail
        client.list_buckets_with_metadata.return_value = []

    if tables_response is not None:
        client.list_tables_with_metadata.return_value = tables_response
    else:
        client.list_tables_with_metadata.return_value = []

    if jobs_grouped_response is not None:
        client.list_jobs_grouped.return_value = jobs_grouped_response
    else:
        client.list_jobs_grouped.return_value = []

    return client


def _init_project(
    tmp_config_dir: Path,
    project_root: Path,
    branches_response: list | None = None,
) -> Any:
    """Helper: init a project and return the ConfigStore for reuse."""

    init_client = _make_sync_mock_client(
        verify_token_response=SAMPLE_VERIFY_TOKEN,
        branches_response=branches_response or SAMPLE_BRANCHES,
    )
    store = setup_single_project(tmp_config_dir)
    init_svc = SyncService(
        config_store=store,
        client_factory=lambda url, token: init_client,
    )
    init_svc.init_sync(alias="prod", project_root=project_root)
    return store


# ===================================================================
# 1. Client tests - list_jobs_grouped()
# ===================================================================


class TestListJobsGrouped:
    """Tests for KeboolaClient.list_jobs_grouped()."""

    def test_list_jobs_grouped_sends_correct_params(self, httpx_mock) -> None:
        """list_jobs_grouped sends correct groupBy, jobsPerGroup, limit params."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/grouped-jobs?groupBy%5B%5D=componentId&groupBy%5B%5D=configId&jobsPerGroup=5&limit=100&sortBy=startTime&sortOrder=desc",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.list_jobs_grouped()
            assert result == []

    def test_list_jobs_grouped_with_custom_params(self, httpx_mock) -> None:
        """list_jobs_grouped passes custom jobs_per_group and limit."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/grouped-jobs?groupBy%5B%5D=componentId&groupBy%5B%5D=configId&jobsPerGroup=3&limit=100&sortBy=startTime&sortOrder=desc",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.list_jobs_grouped(jobs_per_group=3, limit=100)
            assert result == []

    def test_list_jobs_grouped_with_created_time_from(self, httpx_mock) -> None:
        """list_jobs_grouped passes createdTimeFrom filter."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/grouped-jobs?groupBy%5B%5D=componentId&groupBy%5B%5D=configId&jobsPerGroup=5&limit=100&sortBy=startTime&sortOrder=desc&filters%5BcreatedTimeFrom%5D=2026-03-20T00%3A00%3A00Z",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.list_jobs_grouped(created_time_from="2026-03-20T00:00:00Z")
            assert result == []

    def test_list_jobs_grouped_parses_response(self, httpx_mock) -> None:
        """list_jobs_grouped correctly parses grouped job response."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/grouped-jobs?groupBy%5B%5D=componentId&groupBy%5B%5D=configId&jobsPerGroup=5&limit=100&sortBy=startTime&sortOrder=desc",
            json=SAMPLE_GROUPED_JOBS,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.list_jobs_grouped()

            assert len(result) == 2
            assert result[0]["group"]["componentId"] == "keboola.ex-http"
            assert result[0]["group"]["configId"] == "cfg-001"
            assert len(result[0]["jobs"]) == 2
            assert result[0]["jobs"][0]["status"] == "success"


# ===================================================================
# 2. Client tests - list_buckets_with_metadata, list_tables_with_metadata,
#    get_table_data_preview
# ===================================================================


class TestListBucketsWithMetadata:
    """Tests for KeboolaClient.list_buckets_with_metadata()."""

    def test_list_buckets_with_metadata_url_and_params(self, httpx_mock) -> None:
        """list_buckets_with_metadata sends include=metadata param."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets?include=metadata",
            json=SAMPLE_BUCKETS_API,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.list_buckets_with_metadata()

            assert len(result) == 2
            assert result[0]["id"] == "in.c-data"
            assert isinstance(result, list)


class TestListTablesWithMetadata:
    """Tests for KeboolaClient.list_tables_with_metadata()."""

    def test_list_tables_with_metadata_url_and_params(self, httpx_mock) -> None:
        """list_tables_with_metadata sends include=columns,metadata,buckets param."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables?include=columns%2Cmetadata%2Cbuckets",
            json=SAMPLE_TABLES_API,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.list_tables_with_metadata()

            assert len(result) == 3
            assert result[0]["id"] == "in.c-data.users"
            assert isinstance(result, list)


class TestGetTableDataPreview:
    """Tests for KeboolaClient.get_table_data_preview()."""

    def test_get_table_data_preview_url(self, httpx_mock) -> None:
        """get_table_data_preview sends correct URL with encoded table ID."""
        csv_data = '"id","name"\n"1","Alice"\n"2","Bob"\n'
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-data.users/data-preview?limit=100",
            text=csv_data,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.get_table_data_preview("in.c-data.users")

            assert '"id"' in result
            assert '"Alice"' in result

    def test_get_table_data_preview_custom_limit(self, httpx_mock) -> None:
        """get_table_data_preview respects custom limit param."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-data.users/data-preview?limit=10",
            text='"id","name"\n"1","Alice"\n',
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.get_table_data_preview("in.c-data.users", limit=10)

            assert isinstance(result, str)

    def test_get_table_data_preview_returns_string(self, httpx_mock) -> None:
        """get_table_data_preview returns raw CSV text, not parsed JSON."""
        csv_text = '"col1","col2"\n"a","b"\n'
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/out.c-results.summary/data-preview?limit=100",
            text=csv_text,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-test-token",
        ) as client:
            result = client.get_table_data_preview("out.c-results.summary")

            assert result == csv_text


# ===================================================================
# 3. SyncService tests - _write_storage_metadata()
# ===================================================================


class TestWriteStorageMetadata:
    """Tests for SyncService._write_storage_metadata()."""

    def _make_svc(self, tmp_config_dir: Path) -> SyncService:
        """Create a SyncService with a minimal ConfigStore."""
        store = setup_single_project(tmp_config_dir)
        return SyncService(config_store=store)

    def test_creates_directory_structure(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """_write_storage_metadata creates storage/buckets.json and storage/tables/ dirs."""
        svc = self._make_svc(tmp_config_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        stats = svc._write_storage_metadata(project_root, SAMPLE_BUCKETS_API, SAMPLE_TABLES_API, {})

        storage_dir = project_root / STORAGE_DIR_NAME
        assert storage_dir.exists()
        assert (storage_dir / STORAGE_BUCKETS_FILENAME).exists()
        assert (storage_dir / "tables").exists()

        assert stats["buckets"] == 2
        assert stats["tables"] == 3
        assert stats["samples"] == 0

    def test_bucket_summary_format(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """buckets.json contains correct summary fields for each bucket."""
        svc = self._make_svc(tmp_config_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        svc._write_storage_metadata(project_root, SAMPLE_BUCKETS_API, [], {})

        buckets_file = project_root / STORAGE_DIR_NAME / STORAGE_BUCKETS_FILENAME
        buckets = json.loads(buckets_file.read_text(encoding="utf-8"))

        assert len(buckets) == 2

        b0 = buckets[0]
        assert b0["id"] == "in.c-data"
        assert b0["name"] == "c-data"
        assert b0["stage"] == "in"
        assert b0["description"] == "Input data bucket"
        assert b0["tables_count"] == 3
        assert b0["data_size_bytes"] == 1024000
        assert b0["metadata"] == [{"key": "owner", "value": "team-a"}]

        b1 = buckets[1]
        assert b1["id"] == "out.c-results"
        assert b1["tables_count"] == 1

    def test_table_metadata_format(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Per-table JSON files contain correct metadata fields."""
        svc = self._make_svc(tmp_config_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        svc._write_storage_metadata(project_root, SAMPLE_BUCKETS_API, SAMPLE_TABLES_API, {})

        # Check table under in.c-data -> in-c-data/users.json
        table_file = project_root / STORAGE_DIR_NAME / "tables" / "in-c-data" / "users.json"
        assert table_file.exists()

        table_meta = json.loads(table_file.read_text(encoding="utf-8"))
        assert table_meta["id"] == "in.c-data.users"
        assert table_meta["name"] == "users"
        assert table_meta["primary_key"] == ["id"]
        assert table_meta["columns"] == ["id", "name", "email"]
        assert table_meta["rows_count"] == 5000
        assert table_meta["data_size_bytes"] == 204800
        assert table_meta["description"] == "User accounts"

    def test_tables_grouped_by_bucket(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Tables are organized in subdirectories named after their bucket."""
        svc = self._make_svc(tmp_config_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        svc._write_storage_metadata(project_root, SAMPLE_BUCKETS_API, SAMPLE_TABLES_API, {})

        tables_dir = project_root / STORAGE_DIR_NAME / "tables"

        # in.c-data -> in-c-data (dots replaced with dashes)
        assert (tables_dir / "in-c-data" / "users.json").exists()
        assert (tables_dir / "in-c-data" / "orders.json").exists()

        # out.c-results -> out-c-results
        assert (tables_dir / "out-c-results" / "summary.json").exists()

    def test_empty_buckets_and_tables(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """_write_storage_metadata with empty lists creates only the directory."""
        svc = self._make_svc(tmp_config_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        stats = svc._write_storage_metadata(project_root, [], [], {})

        assert stats["buckets"] == 0
        assert stats["tables"] == 0

        # buckets.json should exist with empty list
        buckets_file = project_root / STORAGE_DIR_NAME / STORAGE_BUCKETS_FILENAME
        assert buckets_file.exists()
        assert json.loads(buckets_file.read_text(encoding="utf-8")) == []

    def test_samples_written_to_correct_path(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Samples are written to storage/samples/{bucket}/{table}/sample.csv."""
        svc = self._make_svc(tmp_config_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        samples = {
            "in.c-data.users": '"id","name"\n"1","Alice"\n',
        }

        stats = svc._write_storage_metadata(
            project_root, SAMPLE_BUCKETS_API, SAMPLE_TABLES_API, samples
        )

        assert stats["samples"] == 1

        sample_file = (
            project_root / STORAGE_DIR_NAME / "samples" / "in-c-data" / "users" / "sample.csv"
        )
        assert sample_file.exists()
        assert '"Alice"' in sample_file.read_text(encoding="utf-8")


# ===================================================================
# 4. SyncService tests - _write_per_config_jobs()
# ===================================================================


class TestWritePerConfigJobs:
    """Tests for SyncService._write_per_config_jobs()."""

    def _make_svc(self, tmp_config_dir: Path) -> SyncService:
        store = setup_single_project(tmp_config_dir)
        return SyncService(config_store=store)

    def _make_configs(self) -> list[ManifestConfiguration]:
        """Create ManifestConfiguration objects matching SAMPLE_GROUPED_JOBS."""
        return [
            ManifestConfiguration(
                branchId=12345,
                componentId="keboola.ex-http",
                id="cfg-001",
                path="extractor/keboola.ex-http/my-http-extractor",
            ),
            ManifestConfiguration(
                branchId=12345,
                componentId="keboola.snowflake-transformation",
                id="cfg-002",
                path="transformation/keboola.snowflake-transformation/clean-data",
            ),
        ]

    def test_writes_jobs_jsonl_next_to_configs(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """_write_per_config_jobs creates _jobs.jsonl in each config directory."""
        svc = self._make_svc(tmp_config_dir)
        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        configs = self._make_configs()

        files_written = svc._write_per_config_jobs(branch_dir, configs, SAMPLE_GROUPED_JOBS)

        assert files_written == 2

        # Check first config's jobs file
        jobs_file_1 = branch_dir / configs[0].path / JOBS_FILENAME
        assert jobs_file_1.exists()

        # Check second config's jobs file
        jobs_file_2 = branch_dir / configs[1].path / JOBS_FILENAME
        assert jobs_file_2.exists()

    def test_jsonl_format(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Each line in _jobs.jsonl is a valid JSON object."""
        svc = self._make_svc(tmp_config_dir)
        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        configs = self._make_configs()

        svc._write_per_config_jobs(branch_dir, configs, SAMPLE_GROUPED_JOBS)

        jobs_file = branch_dir / configs[0].path / JOBS_FILENAME
        lines = jobs_file.read_text(encoding="utf-8").strip().split("\n")

        assert len(lines) == 2  # 2 jobs for cfg-001

        for line in lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict)

    def test_light_job_fields(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Each job record contains only the light fields."""
        svc = self._make_svc(tmp_config_dir)
        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        configs = self._make_configs()

        svc._write_per_config_jobs(branch_dir, configs, SAMPLE_GROUPED_JOBS)

        jobs_file = branch_dir / configs[0].path / JOBS_FILENAME
        lines = jobs_file.read_text(encoding="utf-8").strip().split("\n")

        # First job (success, mode=run -> mode omitted)
        job1 = json.loads(lines[0])
        assert job1["id"] == "1001"
        assert job1["status"] == "success"
        assert job1["start_time"] == "2026-03-21T10:00:00Z"
        assert job1["end_time"] == "2026-03-21T10:05:00Z"
        assert job1["duration_seconds"] == 300
        # mode "run" should NOT be included
        assert "mode" not in job1

    def test_error_message_included_for_error_jobs(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """error_message is included for error/warning status jobs."""
        svc = self._make_svc(tmp_config_dir)
        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        configs = self._make_configs()

        svc._write_per_config_jobs(branch_dir, configs, SAMPLE_GROUPED_JOBS)

        jobs_file = branch_dir / configs[0].path / JOBS_FILENAME
        lines = jobs_file.read_text(encoding="utf-8").strip().split("\n")

        # Second job has status "error" with result message
        job2 = json.loads(lines[1])
        assert job2["status"] == "error"
        assert job2["error_message"] == "Connection timeout"

    def test_mode_field_only_for_non_run(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """mode field is included only when it's not 'run'."""
        svc = self._make_svc(tmp_config_dir)
        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        configs = self._make_configs()

        svc._write_per_config_jobs(branch_dir, configs, SAMPLE_GROUPED_JOBS)

        # Second config (cfg-002) has mode="debug"
        jobs_file = branch_dir / configs[1].path / JOBS_FILENAME
        lines = jobs_file.read_text(encoding="utf-8").strip().split("\n")

        job = json.loads(lines[0])
        assert job["mode"] == "debug"

    def test_configs_without_matching_jobs_no_file(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Configs that have no matching jobs don't get a _jobs.jsonl file."""
        svc = self._make_svc(tmp_config_dir)
        branch_dir = tmp_path / "main"
        branch_dir.mkdir()

        # Config with a component/config that has no matching jobs
        configs = [
            ManifestConfiguration(
                branchId=12345,
                componentId="keboola.wr-db",
                id="cfg-999",
                path="writer/keboola.wr-db/no-match",
            ),
        ]

        files_written = svc._write_per_config_jobs(branch_dir, configs, SAMPLE_GROUPED_JOBS)

        assert files_written == 0
        jobs_file = branch_dir / configs[0].path / JOBS_FILENAME
        assert not jobs_file.exists()

    def test_empty_jobs_grouped(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """No files written when jobs_grouped is empty."""
        svc = self._make_svc(tmp_config_dir)
        branch_dir = tmp_path / "main"
        branch_dir.mkdir()
        configs = self._make_configs()

        files_written = svc._write_per_config_jobs(branch_dir, configs, [])

        assert files_written == 0


# ===================================================================
# 5. SyncService tests - _mask_encrypted_columns()
# ===================================================================


class TestMaskEncryptedColumns:
    """Tests for SyncService._mask_encrypted_columns()."""

    def test_mask_columns_starting_with_hash(self) -> None:
        """Columns starting with '#' have their values masked."""
        csv_data = '"id","name","#password"\n"1","Alice","secret123"\n"2","Bob","p@ss"\n'

        result = SyncService._mask_encrypted_columns(csv_data)

        assert "#password" in result
        assert "secret123" not in result
        assert "p@ss" not in result
        assert ENCRYPTED_COLUMN_MASK in result

    def test_non_encrypted_columns_unchanged(self) -> None:
        """Columns not starting with '#' keep their values."""
        csv_data = '"id","name","email"\n"1","Alice","alice@test.com"\n'

        result = SyncService._mask_encrypted_columns(csv_data)

        # No encrypted columns -> returned unchanged (original string)
        assert result == csv_data

    def test_empty_csv_input(self) -> None:
        """Empty string input is returned as-is."""
        result = SyncService._mask_encrypted_columns("")

        assert result == ""

    def test_csv_with_no_encrypted_columns(self) -> None:
        """CSV with no '#' columns is returned unchanged (original string)."""
        csv_data = '"col1","col2","col3"\n"a","b","c"\n'

        result = SyncService._mask_encrypted_columns(csv_data)

        # No encrypted columns -> original string returned
        assert result == csv_data

    def test_multiple_encrypted_columns(self) -> None:
        """Multiple '#' columns are all masked."""
        csv_data = '"id","#token","#secret"\n"1","tok-xxx","sec-yyy"\n'

        result = SyncService._mask_encrypted_columns(csv_data)

        assert "tok-xxx" not in result
        assert "sec-yyy" not in result
        # The mask should appear twice (once per encrypted column per data row)
        assert result.count(ENCRYPTED_COLUMN_MASK) == 2

    def test_header_line_only(self) -> None:
        """CSV with only header line (no data rows) returns valid output."""
        csv_data = '"id","#password"\n'

        result = SyncService._mask_encrypted_columns(csv_data)

        # Header should be preserved (csv writer may strip quotes)
        assert "#password" in result


# ===================================================================
# 6. SyncService tests - _fetch_samples()
# ===================================================================


class TestFetchSamples:
    """Tests for SyncService._fetch_samples()."""

    def _make_svc(self, tmp_config_dir: Path) -> SyncService:
        store = setup_single_project(tmp_config_dir)
        return SyncService(config_store=store)

    def test_tables_sorted_by_rows_count_largest_first(self, tmp_config_dir: Path) -> None:
        """Tables are sorted by rowsCount descending before fetching."""
        svc = self._make_svc(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_table_data_preview.return_value = '"col"\n"val"\n'

        tables = [
            {"id": "t1", "rowsCount": 100},
            {"id": "t2", "rowsCount": 50000},
            {"id": "t3", "rowsCount": 1000},
        ]

        result = svc._fetch_samples(mock_client, tables, sample_limit=100, max_samples=2)

        # max_samples=2, so only the top 2 (t2 with 50000, t3 with 1000) should be fetched
        assert len(result) == 2
        assert "t2" in result
        assert "t3" in result
        assert "t1" not in result

    def test_max_samples_limit(self, tmp_config_dir: Path) -> None:
        """Only max_samples tables are fetched."""
        svc = self._make_svc(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_table_data_preview.return_value = '"data"\n"value"\n'

        tables = [{"id": f"table-{i}", "rowsCount": 1000 - i} for i in range(10)]

        result = svc._fetch_samples(mock_client, tables, sample_limit=50, max_samples=3)

        assert len(result) == 3
        assert mock_client.get_table_data_preview.call_count == 3

    def test_tables_with_zero_rows_skipped(self, tmp_config_dir: Path) -> None:
        """Tables with rowsCount=0 are excluded from sampling."""
        svc = self._make_svc(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_table_data_preview.return_value = '"col"\n"val"\n'

        tables = [
            {"id": "has-data", "rowsCount": 500},
            {"id": "empty-table", "rowsCount": 0},
            {"id": "also-empty", "rowsCount": 0},
        ]

        result = svc._fetch_samples(mock_client, tables, sample_limit=100, max_samples=10)

        assert len(result) == 1
        assert "has-data" in result
        assert "empty-table" not in result

    def test_failed_preview_skipped_gracefully(self, tmp_config_dir: Path) -> None:
        """If get_table_data_preview fails for a table, it's skipped."""
        svc = self._make_svc(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_table_data_preview.side_effect = [
            '"col"\n"ok"\n',
            Exception("API error"),
            '"col"\n"also-ok"\n',
        ]

        tables = [
            {"id": "t1", "rowsCount": 3000},
            {"id": "t2", "rowsCount": 2000},
            {"id": "t3", "rowsCount": 1000},
        ]

        result = svc._fetch_samples(mock_client, tables, sample_limit=100, max_samples=10)

        assert len(result) == 2
        assert "t1" in result
        assert "t3" in result
        assert "t2" not in result

    def test_sample_limit_passed_to_preview(self, tmp_config_dir: Path) -> None:
        """sample_limit is passed as the limit param to get_table_data_preview."""
        svc = self._make_svc(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_table_data_preview.return_value = '"col"\n'

        tables = [{"id": "t1", "rowsCount": 100}]

        svc._fetch_samples(mock_client, tables, sample_limit=42, max_samples=10)

        mock_client.get_table_data_preview.assert_called_once_with("t1", limit=42, columns=None)


# ===================================================================
# 7. Integration: pull() with new params
# ===================================================================


class TestPullWithNewParams:
    """Tests for pull() with no_storage, no_jobs flags and result keys."""

    def test_pull_with_no_storage(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull with no_storage=True should NOT call storage API methods."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_SIMPLE,
            buckets_response=SAMPLE_BUCKETS_API,
            tables_response=SAMPLE_TABLES_API,
            jobs_grouped_response=SAMPLE_GROUPED_JOBS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        svc.pull(alias="prod", project_root=project_root, no_storage=True)

        # Storage methods should NOT have been called
        pull_client.list_buckets_with_metadata.assert_not_called()
        pull_client.list_tables_with_metadata.assert_not_called()

        # Jobs should still be fetched
        pull_client.list_jobs_grouped.assert_called_once()

        # Storage dir should not exist
        assert not (project_root / STORAGE_DIR_NAME).exists()

    def test_pull_with_no_jobs(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull with no_jobs=True should NOT call jobs API methods."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_SIMPLE,
            buckets_response=SAMPLE_BUCKETS_API,
            tables_response=SAMPLE_TABLES_API,
            jobs_grouped_response=SAMPLE_GROUPED_JOBS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        svc.pull(alias="prod", project_root=project_root, no_jobs=True)

        # Jobs method should NOT have been called
        pull_client.list_jobs_grouped.assert_not_called()

        # Storage should still be fetched
        pull_client.list_buckets_with_metadata.assert_called_once()
        pull_client.list_tables_with_metadata.assert_called_once()

        # No _jobs.jsonl files should exist
        jobs_files = list(project_root.rglob(JOBS_FILENAME))
        assert len(jobs_files) == 0

    def test_pull_result_includes_jobs_written_key(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """pull result dict includes 'jobs_written' key."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_SIMPLE,
            jobs_grouped_response=SAMPLE_GROUPED_JOBS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(alias="prod", project_root=project_root)

        assert "jobs_written" in result
        assert isinstance(result["jobs_written"], int)
        assert result["jobs_written"] == 2  # 2 configs matched

    def test_pull_result_includes_storage_key(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull result dict includes 'storage' key with bucket/table/sample counts."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_SIMPLE,
            buckets_response=SAMPLE_BUCKETS_API,
            tables_response=SAMPLE_TABLES_API,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(alias="prod", project_root=project_root)

        assert "storage" in result
        assert result["storage"]["buckets"] == 2
        assert result["storage"]["tables"] == 3
        assert result["storage"]["samples"] == 0

    def test_pull_both_no_storage_and_no_jobs(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull with both no_storage=True and no_jobs=True only fetches configs."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_SIMPLE,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(
            alias="prod",
            project_root=project_root,
            no_storage=True,
            no_jobs=True,
        )

        # Neither storage nor jobs should have been called
        pull_client.list_buckets_with_metadata.assert_not_called()
        pull_client.list_tables_with_metadata.assert_not_called()
        pull_client.list_jobs_grouped.assert_not_called()

        # Result should still have the keys with zero/empty values
        assert result["jobs_written"] == 0
        assert result["storage"]["buckets"] == 0
        assert result["storage"]["tables"] == 0


# ===================================================================
# 8. Per-config job fetching fallback
# ===================================================================


def _make_many_configs(n: int) -> list[dict[str, Any]]:
    """Generate n components each with 1 config, to simulate a large project."""
    return [
        {
            "id": f"keboola.component-{i}",
            "type": "extractor",
            "configurations": [
                {
                    "id": f"cfg-{i:04d}",
                    "name": f"Config {i}",
                    "description": "",
                    "configuration": {"parameters": {}},
                    "rows": [],
                }
            ],
        }
        for i in range(n)
    ]


class TestFetchJobsPerConfig:
    """Tests for SyncService._fetch_jobs_per_config() fallback method."""

    def test_returns_grouped_format(self, tmp_config_dir: Path) -> None:
        """_fetch_jobs_per_config returns data in same format as list_jobs_grouped."""
        store = setup_single_project(tmp_config_dir)
        client = MagicMock()
        # list_jobs returns different results per config
        client.list_jobs.return_value = [
            {"id": 1001, "status": "success", "startTime": "2026-03-21T10:00:00Z"},
        ]

        svc = SyncService(config_store=store, client_factory=lambda u, t: client)
        result = svc._fetch_jobs_per_config(client, SAMPLE_COMPONENTS_SIMPLE, job_limit=5)

        assert len(result) == 2  # 2 configs in SAMPLE_COMPONENTS_SIMPLE
        for group in result:
            assert "group" in group
            assert "componentId" in group["group"]
            assert "configId" in group["group"]
            assert "jobs" in group
            assert len(group["jobs"]) == 1

    def test_skips_configs_without_jobs(self, tmp_config_dir: Path) -> None:
        """Configs with no jobs are not included in the result."""
        store = setup_single_project(tmp_config_dir)
        client = MagicMock()

        # First config has jobs, second has none
        call_count = 0

        def _side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("component_id") == "keboola.ex-http":
                return [{"id": 1001, "status": "success"}]
            return []

        client.list_jobs.side_effect = _side_effect

        svc = SyncService(config_store=store, client_factory=lambda u, t: client)
        result = svc._fetch_jobs_per_config(client, SAMPLE_COMPONENTS_SIMPLE, job_limit=5)

        assert len(result) == 1
        assert result[0]["group"]["componentId"] == "keboola.ex-http"

    def test_handles_api_errors_gracefully(self, tmp_config_dir: Path) -> None:
        """Individual config failures are logged but don't break the whole fetch."""
        store = setup_single_project(tmp_config_dir)
        client = MagicMock()

        def _side_effect(**kwargs):
            if kwargs.get("component_id") == "keboola.ex-http":
                raise RuntimeError("Connection failed")
            return [{"id": 2001, "status": "success"}]

        client.list_jobs.side_effect = _side_effect

        svc = SyncService(config_store=store, client_factory=lambda u, t: client)
        result = svc._fetch_jobs_per_config(client, SAMPLE_COMPONENTS_SIMPLE, job_limit=5)

        # Only the successful config should be in results
        assert len(result) == 1
        assert result[0]["group"]["componentId"] == "keboola.snowflake-transformation"

    def test_empty_components(self, tmp_config_dir: Path) -> None:
        """Empty components list returns empty results."""
        store = setup_single_project(tmp_config_dir)
        client = MagicMock()

        svc = SyncService(config_store=store, client_factory=lambda u, t: client)
        result = svc._fetch_jobs_per_config(client, [], job_limit=5)

        assert result == []
        client.list_jobs.assert_not_called()

    def test_passes_job_limit_to_client(self, tmp_config_dir: Path) -> None:
        """The job_limit parameter is forwarded as limit to client.list_jobs."""
        store = setup_single_project(tmp_config_dir)
        client = MagicMock()
        client.list_jobs.return_value = []

        components = [
            {
                "id": "keboola.ex-http",
                "type": "extractor",
                "configurations": [
                    {
                        "id": "cfg-001",
                        "name": "Test",
                        "description": "",
                        "configuration": {},
                        "rows": [],
                    }
                ],
            }
        ]

        svc = SyncService(config_store=store, client_factory=lambda u, t: client)
        svc._fetch_jobs_per_config(client, components, job_limit=10)

        client.list_jobs.assert_called_once_with(
            component_id="keboola.ex-http",
            config_id="cfg-001",
            limit=10,
        )


class TestPullJobsFallback:
    """Tests for pull() switching to per-config fetching when grouped-jobs limit is insufficient."""

    def test_uses_grouped_when_limit_sufficient(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull() uses grouped-jobs when group_limit >= total configs."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        # 2 configs, job_limit=1 -> group_limit=500 >> 2
        pull_client = _make_sync_mock_client(
            components_response=SAMPLE_COMPONENTS_SIMPLE,
            jobs_grouped_response=SAMPLE_GROUPED_JOBS,
        )
        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(alias="prod", project_root=project_root, job_limit=1)

        pull_client.list_jobs_grouped.assert_called_once()
        pull_client.list_jobs.assert_not_called()
        assert result["jobs_written"] == 2

    def test_falls_back_to_per_config_when_limit_insufficient(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """pull() falls back to per-config fetching when group_limit < total configs."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        # 200 configs, job_limit=10 -> group_limit=50 < 200 -> fallback
        many_components = _make_many_configs(200)
        pull_client = _make_sync_mock_client(
            components_response=many_components,
        )
        pull_client.list_jobs.return_value = [
            {
                "id": 9001,
                "status": "success",
                "startTime": "2026-03-21T10:00:00Z",
                "endTime": "2026-03-21T10:05:00Z",
                "durationSeconds": 300,
                "mode": "run",
            },
        ]

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        result = svc.pull(alias="prod", project_root=project_root, job_limit=10)

        # Should NOT use grouped-jobs, SHOULD use list_jobs per config
        pull_client.list_jobs_grouped.assert_not_called()
        assert pull_client.list_jobs.call_count == 200
        assert result["jobs_written"] == 200

    def test_boundary_exact_limit(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull() uses grouped-jobs when group_limit == total configs (boundary case)."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        # 100 configs, job_limit=5 -> group_limit=100 == 100 -> grouped path
        components_100 = _make_many_configs(100)
        pull_client = _make_sync_mock_client(
            components_response=components_100,
            jobs_grouped_response=[],
        )

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        svc.pull(alias="prod", project_root=project_root, job_limit=5)

        pull_client.list_jobs_grouped.assert_called_once()
        pull_client.list_jobs.assert_not_called()

    def test_boundary_one_over_limit(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """pull() falls back when group_limit < total configs by just 1."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        store = _init_project(tmp_config_dir, project_root)

        # 101 configs, job_limit=5 -> group_limit=100 < 101 -> fallback
        components_101 = _make_many_configs(101)
        pull_client = _make_sync_mock_client(
            components_response=components_101,
        )
        pull_client.list_jobs.return_value = []

        svc = SyncService(
            config_store=store,
            client_factory=lambda url, token: pull_client,
        )

        svc.pull(alias="prod", project_root=project_root, job_limit=5)

        pull_client.list_jobs_grouped.assert_not_called()
        assert pull_client.list_jobs.call_count == 101
