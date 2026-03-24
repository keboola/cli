"""Tests for OutputFormatter with JSON and Rich dual mode."""

import json
import sys
from io import StringIO

from rich.console import Console

from keboola_agent_cli.output import (
    OutputFormatter,
    _format_duration,
    _seconds_to_human,
    format_config_detail,
    format_configs_table,
    format_doctor_panel,
    format_job_detail,
    format_jobs_table,
    format_lineage_table,
    format_tool_result,
    format_tools_table,
)


class TestOutputFormatterJsonMode:
    """Tests for OutputFormatter in JSON mode."""

    def test_output_list_data(self) -> None:
        """JSON mode outputs a valid JSON envelope with list data."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.output([{"id": 1, "name": "test"}])
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"] == [{"id": 1, "name": "test"}]

    def test_output_dict_data(self) -> None:
        """JSON mode outputs valid JSON with dict data."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.output({"key": "value"})
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"]["key"] == "value"

    def test_output_empty_list(self) -> None:
        """JSON mode handles empty list data."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.output([])
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"] == []

    def test_error_json_output(self) -> None:
        """JSON mode error outputs structured error envelope with derived error_type."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.error(
                message="Token expired",
                error_code="INVALID_TOKEN",
                project="prod-aws",
                retryable=False,
            )
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_TOKEN"
        assert result["error"]["error_type"] == "authentication"
        assert result["error"]["message"] == "Token expired"
        assert result["error"]["project"] == "prod-aws"
        assert result["error"]["retryable"] is False

    def test_error_json_output_explicit_error_type(self) -> None:
        """JSON mode error uses explicit error_type when provided."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.error(
                message="Something failed",
                error_code="CUSTOM_ERROR",
                error_type="validation",
            )
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["error"]["error_type"] == "validation"
        assert result["error"]["code"] == "CUSTOM_ERROR"

    def test_error_json_output_network_type(self) -> None:
        """JSON mode error derives network type for TIMEOUT error code."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.error(
                message="Request timed out",
                error_code="TIMEOUT",
                retryable=True,
            )
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["error"]["error_type"] == "network"

    def test_error_json_output_fallback_api_type(self) -> None:
        """JSON mode error falls back to 'api' type for unknown error codes."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.error(
                message="Server error",
                error_code="INTERNAL_ERROR",
            )
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["error"]["error_type"] == "api"

    def test_success_json_output(self) -> None:
        """JSON mode success outputs structured response."""
        formatter = OutputFormatter(json_mode=True, no_color=True)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            formatter.success("Project added successfully")
        finally:
            sys.stdout = old_stdout

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["data"]["message"] == "Project added successfully"


class TestOutputFormatterHumanMode:
    """Tests for OutputFormatter in human (Rich) mode."""

    def test_output_calls_human_formatter(self) -> None:
        """Human mode calls the provided human_formatter callable."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        called_with: list = []

        def mock_formatter(console: Console, data: object) -> None:
            called_with.append(data)

        formatter.output({"test": "data"}, mock_formatter)
        assert len(called_with) == 1
        assert called_with[0] == {"test": "data"}

    def test_output_without_formatter_does_not_crash(self) -> None:
        """Human mode without a formatter falls back to console.print and does not crash."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        formatter.output("simple string")

    def test_error_does_not_crash(self) -> None:
        """Human mode error output does not crash."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        formatter.error("Something went wrong")

    def test_success_does_not_crash(self) -> None:
        """Human mode success output does not crash."""
        formatter = OutputFormatter(json_mode=False, no_color=True)
        formatter.success("All good")


class TestOutputFormatterInit:
    """Tests for OutputFormatter initialization options."""

    def test_json_mode_flag(self) -> None:
        """json_mode flag is stored correctly."""
        formatter = OutputFormatter(json_mode=True)
        assert formatter.json_mode is True

    def test_verbose_flag(self) -> None:
        """verbose flag is stored correctly."""
        formatter = OutputFormatter(verbose=True)
        assert formatter.verbose is True

    def test_no_color_creates_console(self) -> None:
        """no_color creates a Console instance with color disabled."""
        formatter = OutputFormatter(no_color=True)
        assert formatter.console is not None
        assert formatter.err_console is not None


class TestFormatJobsTable:
    """Tests for format_jobs_table Rich output."""

    def test_jobs_table_with_jobs(self) -> None:
        """format_jobs_table renders table with job rows and project column."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "jobs": [
                {
                    "id": 1001,
                    "status": "success",
                    "component": "keboola.ex-db-snowflake",
                    "configId": "101",
                    "createdTime": "2026-02-26T10:00:00Z",
                    "durationSeconds": 45,
                    "project_alias": "prod",
                },
                {
                    "id": 1002,
                    "status": "error",
                    "component": "keboola.wr-db-snowflake",
                    "configId": "201",
                    "createdTime": "2026-02-26T11:00:00Z",
                    "durationSeconds": 120,
                    "project_alias": "prod",
                },
            ],
            "errors": [],
        }

        format_jobs_table(console, data)
        output = console.file.getvalue()

        assert "Jobs" in output
        assert "Project" in output
        assert "prod" in output
        assert "1001" in output
        assert "1002" in output
        assert "success" in output
        assert "error" in output
        assert "45s" in output
        assert "2m 0s" in output

    def test_jobs_table_empty_jobs(self) -> None:
        """format_jobs_table shows helpful message when no jobs found."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {"jobs": [], "errors": []}

        format_jobs_table(console, data)
        output = console.file.getvalue()

        assert "No jobs found" in output

    def test_jobs_table_with_errors_only(self) -> None:
        """format_jobs_table shows errors when all projects fail."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "jobs": [],
            "errors": [
                {
                    "project_alias": "bad",
                    "error_code": "INVALID_TOKEN",
                    "message": "Token expired",
                },
            ],
        }

        format_jobs_table(console, data)
        output = console.file.getvalue()

        assert "Warning" in output
        assert "bad" in output
        assert "Token expired" in output
        assert "all projects failed" in output

    def test_jobs_table_multi_project(self) -> None:
        """format_jobs_table shows both project aliases in one table."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "jobs": [
                {
                    "id": 1001,
                    "status": "success",
                    "component": "comp-a",
                    "configId": "1",
                    "createdTime": "2026-02-26T10:00:00Z",
                    "durationSeconds": 10,
                    "project_alias": "prod",
                },
                {
                    "id": 2001,
                    "status": "processing",
                    "component": "comp-b",
                    "configId": "2",
                    "createdTime": "2026-02-26T12:00:00Z",
                    "project_alias": "dev",
                },
            ],
            "errors": [],
        }

        format_jobs_table(console, data)
        output = console.file.getvalue()

        assert "prod" in output
        assert "dev" in output
        assert "Project" in output

    def test_jobs_table_duration_formatting(self) -> None:
        """format_jobs_table formats duration in human-readable format."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "jobs": [
                {
                    "id": 1,
                    "status": "success",
                    "component": "c",
                    "configId": "1",
                    "createdTime": "2026-01-01T00:00:00Z",
                    "durationSeconds": 3661,  # 1h 1m
                    "project_alias": "x",
                },
            ],
            "errors": [],
        }

        format_jobs_table(console, data)
        output = console.file.getvalue()

        assert "1h 1m" in output

    def test_jobs_table_no_duration(self) -> None:
        """format_jobs_table shows '-' for jobs without duration info."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "jobs": [
                {
                    "id": 1,
                    "status": "processing",
                    "component": "c",
                    "configId": "1",
                    "createdTime": "2026-01-01T00:00:00Z",
                    "project_alias": "x",
                },
            ],
            "errors": [],
        }

        format_jobs_table(console, data)
        output = console.file.getvalue()

        # Should contain a dash for missing duration
        assert "-" in output


class TestFormatJobDetail:
    """Tests for format_job_detail Rich output."""

    def test_job_detail_renders_key_fields(self) -> None:
        """format_job_detail renders all key fields in a panel."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "id": "1001",
            "status": "error",
            "component": "keboola.ex-db-snowflake",
            "config": "123",
            "mode": "run",
            "type": "standard",
            "createdTime": "2026-02-26T10:00:00Z",
            "startTime": "2026-02-26T10:00:05Z",
            "endTime": "2026-02-26T10:00:50Z",
            "durationSeconds": 45,
            "branchId": "538",
            "url": "https://queue.keboola.com/jobs/1001",
            "result": {
                "message": "Validation Error: missing field",
                "error": {"type": "user"},
            },
            "project_alias": "prod",
        }

        format_job_detail(console, data)
        output = console.file.getvalue()

        assert "1001" in output
        assert "prod" in output
        assert "error" in output
        assert "keboola.ex-db-snowflake" in output
        assert "123" in output
        assert "run" in output
        assert "standard" in output
        assert "45s" in output
        assert "538" in output
        assert "queue.keboola.com" in output
        assert "Validation Error" in output
        assert "user" in output

    def test_job_detail_success_job(self) -> None:
        """format_job_detail renders a successful job without error section."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "id": "2001",
            "status": "success",
            "component": "keboola.wr-db-snowflake",
            "config": "456",
            "mode": "run",
            "type": "standard",
            "createdTime": "2026-02-26T12:00:00Z",
            "durationSeconds": 120,
            "result": {"message": ""},
            "project_alias": "dev",
        }

        format_job_detail(console, data)
        output = console.file.getvalue()

        assert "2001" in output
        assert "dev" in output
        assert "success" in output
        assert "2m 0s" in output

    def test_job_detail_minimal_data(self) -> None:
        """format_job_detail handles minimal job data without crashing."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "id": "3001",
            "status": "processing",
            "component": "comp",
            "project_alias": "test",
        }

        format_job_detail(console, data)
        output = console.file.getvalue()

        assert "3001" in output
        assert "processing" in output


# ---------------------------------------------------------------------------
# _format_duration and _seconds_to_human tests
# ---------------------------------------------------------------------------


class TestFormatDuration:
    """Tests for _format_duration helper."""

    def test_duration_from_duration_seconds(self) -> None:
        """_format_duration uses durationSeconds when available."""
        job = {"durationSeconds": 90}
        assert _format_duration(job) == "1m 30s"

    def test_duration_from_start_end_times(self) -> None:
        """_format_duration calculates from startTime/endTime when no durationSeconds."""
        job = {
            "startTime": "2026-02-26T10:00:00+00:00",
            "endTime": "2026-02-26T10:02:30+00:00",
        }
        assert _format_duration(job) == "2m 30s"

    def test_duration_missing_both(self) -> None:
        """_format_duration returns '-' when neither durationSeconds nor times are present."""
        job = {}
        assert _format_duration(job) == "-"

    def test_duration_zero_seconds(self) -> None:
        """_format_duration handles zero-second duration."""
        job = {"durationSeconds": 0}
        assert _format_duration(job) == "0s"

    def test_duration_invalid_time_format(self) -> None:
        """_format_duration returns '-' for invalid time formats."""
        job = {"startTime": "not-a-date", "endTime": "also-not-a-date"}
        assert _format_duration(job) == "-"


class TestSecondsToHuman:
    """Tests for _seconds_to_human helper."""

    def test_seconds_only(self) -> None:
        """Durations under 60s show as Ns."""
        assert _seconds_to_human(0) == "0s"
        assert _seconds_to_human(1) == "1s"
        assert _seconds_to_human(59) == "59s"

    def test_minutes_and_seconds(self) -> None:
        """Durations 1m-59m show as Nm Ns."""
        assert _seconds_to_human(60) == "1m 0s"
        assert _seconds_to_human(90) == "1m 30s"
        assert _seconds_to_human(3599) == "59m 59s"

    def test_hours_and_minutes(self) -> None:
        """Durations >= 1h show as Nh Nm."""
        assert _seconds_to_human(3600) == "1h 0m"
        assert _seconds_to_human(3661) == "1h 1m"
        assert _seconds_to_human(7200) == "2h 0m"
        assert _seconds_to_human(7260) == "2h 1m"


# ---------------------------------------------------------------------------
# format_lineage_table tests
# ---------------------------------------------------------------------------


class TestFormatLineageTable:
    """Tests for format_lineage_table Rich output."""

    def test_lineage_table_with_edges(self) -> None:
        """format_lineage_table renders data flow table with edge details."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "edges": [
                {
                    "source_project_alias": "prod",
                    "source_project_id": 258,
                    "source_project_name": "Production",
                    "source_bucket_id": "in.c-shared",
                    "source_bucket_name": "shared",
                    "sharing_type": "organization-project",
                    "target_project_alias": "dev",
                    "target_project_id": 7012,
                    "target_project_name": "Development",
                    "target_bucket_id": "in.c-linked",
                },
            ],
            "shared_buckets": [
                {
                    "project_alias": "prod",
                    "project_id": 258,
                    "bucket_id": "in.c-shared",
                    "bucket_name": "shared",
                    "sharing_type": "organization-project",
                },
            ],
            "linked_buckets": [],
            "summary": {
                "total_shared_buckets": 1,
                "total_linked_buckets": 0,
                "total_edges": 1,
                "projects_queried": 2,
            },
            "errors": [],
        }

        format_lineage_table(console, data)
        output = console.file.getvalue()

        assert "Data Flow" in output
        assert "in.c-shared" in output
        assert "in.c-linked" in output
        # Rich may truncate long strings in table columns
        assert "organization" in output
        assert "1" in output  # edge count in summary

    def test_lineage_table_no_edges_no_buckets(self) -> None:
        """format_lineage_table shows 'no bucket sharing' when nothing found."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "edges": [],
            "shared_buckets": [],
            "linked_buckets": [],
            "summary": {
                "total_shared_buckets": 0,
                "total_linked_buckets": 0,
                "total_edges": 0,
                "projects_queried": 2,
            },
            "errors": [],
        }

        format_lineage_table(console, data)
        output = console.file.getvalue()

        assert "No bucket sharing" in output

    def test_lineage_table_with_errors(self) -> None:
        """format_lineage_table renders warnings for per-project errors."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "edges": [],
            "shared_buckets": [],
            "linked_buckets": [],
            "summary": {
                "total_shared_buckets": 0,
                "total_linked_buckets": 0,
                "total_edges": 0,
                "projects_queried": 1,
            },
            "errors": [
                {
                    "project_alias": "bad",
                    "message": "Token expired",
                },
            ],
        }

        format_lineage_table(console, data)
        output = console.file.getvalue()

        assert "Warning" in output
        assert "bad" in output
        assert "Token expired" in output

    def test_lineage_table_shared_buckets_no_edges(self) -> None:
        """format_lineage_table shows shared buckets table when edges are empty."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "edges": [],
            "shared_buckets": [
                {
                    "project_alias": "prod",
                    "project_id": 258,
                    "bucket_id": "in.c-shared",
                    "bucket_name": "shared",
                    "sharing_type": "organization-project",
                },
            ],
            "linked_buckets": [],
            "summary": {
                "total_shared_buckets": 1,
                "total_linked_buckets": 0,
                "total_edges": 0,
                "projects_queried": 1,
            },
            "errors": [],
        }

        format_lineage_table(console, data)
        output = console.file.getvalue()

        assert "Shared Buckets" in output
        assert "in.c-shared" in output


# ---------------------------------------------------------------------------
# format_tool_result tests
# ---------------------------------------------------------------------------


class TestFormatToolResult:
    """Tests for format_tool_result Rich output."""

    def test_tool_result_single_success(self) -> None:
        """format_tool_result renders a single successful result."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "results": [
                {
                    "project_alias": "prod",
                    "isError": False,
                    "content": ["Table loaded: 1000 rows"],
                },
            ],
            "errors": [],
        }

        format_tool_result(console, data)
        output = console.file.getvalue()

        assert "prod" in output
        assert "OK" in output
        assert "1000 rows" in output

    def test_tool_result_all_same_error_consolidated(self) -> None:
        """format_tool_result consolidates identical errors across projects."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "results": [
                {
                    "project_alias": "prod",
                    "isError": True,
                    "content": ["Missing parameter: bucket_id"],
                },
                {
                    "project_alias": "dev",
                    "isError": True,
                    "content": ["Missing parameter: bucket_id"],
                },
            ],
            "errors": [],
        }

        format_tool_result(console, data)
        output = console.file.getvalue()

        assert "Tool Error" in output
        assert "same error across 2 projects" in output
        assert "prod" in output
        assert "dev" in output

    def test_tool_result_empty(self) -> None:
        """format_tool_result handles empty results."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {"results": [], "errors": []}

        format_tool_result(console, data)
        output = console.file.getvalue()

        assert "No results" in output

    def test_tool_result_with_dict_content(self) -> None:
        """format_tool_result renders dict content items as JSON."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "results": [
                {
                    "project_alias": "prod",
                    "isError": False,
                    "content": [{"table_id": "in.c-test.data", "rows": 100}],
                },
            ],
            "errors": [],
        }

        format_tool_result(console, data)
        output = console.file.getvalue()

        assert "prod" in output
        assert "table_id" in output
        assert "in.c-test.data" in output


# ---------------------------------------------------------------------------
# format_tools_table tests
# ---------------------------------------------------------------------------


class TestFormatToolsTable:
    """Tests for format_tools_table Rich output."""

    def test_tools_table_with_tools(self) -> None:
        """format_tools_table renders table with tool information."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "tools": [
                {
                    "name": "list_tables",
                    "multi_project": True,
                    "description": "List all tables in the project",
                },
                {
                    "name": "create_table",
                    "multi_project": False,
                    "description": "Create a new table",
                },
            ],
            "errors": [],
        }

        format_tools_table(console, data)
        output = console.file.getvalue()

        assert "MCP Tools" in output
        assert "list_tables" in output
        assert "create_table" in output
        assert "yes" in output
        assert "no" in output

    def test_tools_table_empty(self) -> None:
        """format_tools_table shows message when no tools found."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {"tools": [], "errors": []}

        format_tools_table(console, data)
        output = console.file.getvalue()

        assert "No MCP tools found" in output

    def test_tools_table_with_errors(self) -> None:
        """format_tools_table shows warnings for failed projects."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "tools": [],
            "errors": [
                {
                    "project_alias": "bad",
                    "message": "Connection refused",
                },
            ],
        }

        format_tools_table(console, data)
        output = console.file.getvalue()

        assert "Warning" in output
        assert "bad" in output
        assert "Connection refused" in output


# ---------------------------------------------------------------------------
# format_config_detail tests
# ---------------------------------------------------------------------------


class TestFormatConfigDetail:
    """Tests for format_config_detail Rich output."""

    def test_config_detail_renders_all_fields(self) -> None:
        """format_config_detail renders all key fields in a panel."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "project_alias": "prod",
            "name": "Production Load",
            "id": "101",
            "description": "Loads production data",
            "component_id": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {"db": "prod"}},
            "rows": [
                {"name": "row-1"},
                {"name": "row-2"},
            ],
        }

        format_config_detail(console, data)
        output = console.file.getvalue()

        assert "Configuration Detail" in output
        assert "Production Load" in output
        assert "101" in output
        assert "keboola.ex-db-snowflake" in output
        assert "Loads production data" in output
        assert "2 row(s)" in output
        assert "row-1" in output
        assert "row-2" in output

    def test_config_detail_minimal(self) -> None:
        """format_config_detail handles minimal data without crashing."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "name": "Minimal",
            "id": "1",
        }

        format_config_detail(console, data)
        output = console.file.getvalue()

        assert "Minimal" in output
        assert "1" in output


# ---------------------------------------------------------------------------
# format_configs_table tests
# ---------------------------------------------------------------------------


class TestFormatConfigsTable:
    """Tests for format_configs_table Rich output."""

    def test_configs_table_grouped_by_project(self) -> None:
        """format_configs_table groups configs by project alias."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "configs": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.ex-db-snowflake",
                    "component_type": "extractor",
                    "config_id": "101",
                    "config_name": "Production Load",
                    "config_description": "Loads production data",
                },
                {
                    "project_alias": "prod",
                    "component_id": "keboola.wr-db-snowflake",
                    "component_type": "writer",
                    "config_id": "201",
                    "config_name": "Write to DWH",
                    "config_description": "",
                },
            ],
            "errors": [],
        }

        format_configs_table(console, data)
        output = console.file.getvalue()

        assert "Configurations" in output
        assert "prod" in output
        assert "Production Load" in output
        assert "Write to DWH" in output

    def test_configs_table_empty(self) -> None:
        """format_configs_table shows helpful message when no configs found."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {"configs": [], "errors": []}

        format_configs_table(console, data)
        output = console.file.getvalue()

        assert "No configurations found" in output


# ---------------------------------------------------------------------------
# format_doctor_panel tests
# ---------------------------------------------------------------------------


class TestFormatDoctorPanel:
    """Tests for format_doctor_panel Rich output."""

    def test_doctor_panel_with_checks(self) -> None:
        """format_doctor_panel renders checks with status indicators."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "checks": [
                {
                    "check": "config_file",
                    "status": "pass",
                    "name": "Config File",
                    "message": "Found at ~/.config/kbagent/config.json",
                },
                {
                    "check": "config_valid",
                    "status": "pass",
                    "name": "Config Valid",
                    "message": "1 project configured",
                },
                {
                    "check": "version",
                    "status": "pass",
                    "name": "Version",
                    "message": "kbagent v0.1.0",
                },
            ],
            "summary": {
                "total": 3,
                "passed": 3,
                "failed": 0,
                "warnings": 0,
            },
        }

        format_doctor_panel(console, data)
        output = console.file.getvalue()

        assert "kbagent doctor" in output
        assert "PASS" in output
        assert "Config File" in output
        assert "3 checks" in output
        assert "3 passed" in output

    def test_doctor_panel_with_failures(self) -> None:
        """format_doctor_panel renders failure and warning statuses."""
        console = Console(file=StringIO(), no_color=True, force_terminal=False)
        data = {
            "checks": [
                {
                    "check": "config_file",
                    "status": "fail",
                    "name": "Config File",
                    "message": "Not found",
                },
                {
                    "check": "connectivity",
                    "status": "warn",
                    "name": "Connectivity",
                    "message": "Timeout",
                },
            ],
            "summary": {
                "total": 2,
                "passed": 0,
                "failed": 1,
                "warnings": 1,
            },
        }

        format_doctor_panel(console, data)
        output = console.file.getvalue()

        assert "FAIL" in output
        assert "WARN" in output
        assert "1 failed" in output
        assert "1 warnings" in output
