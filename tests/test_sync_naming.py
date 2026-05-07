"""Tests for sync naming module -- path generation from templates."""

import pytest

from keboola_agent_cli.sync.naming import (
    config_path,
    config_row_path,
    sanitize_name,
    sanitize_path_segment,
)


class TestConfigPath:
    """Tests for config_path()."""

    def test_config_path_basic(self) -> None:
        """Standard template produces correct directory path."""
        template = "{component_type}/{component_id}/{config_name}"
        result = config_path(template, "extractor", "keboola.ex-http", "My API Source")

        assert result == "extractor/keboola.ex-http/my-api-source"

    def test_config_path_sanitizes_name(self) -> None:
        """Config name with spaces and special characters is sanitized."""
        template = "{component_type}/{component_id}/{config_name}"
        result = config_path(template, "writer", "keboola.wr-db-snowflake", "Sales Data (v2)!")

        assert result == "writer/keboola.wr-db-snowflake/sales-data-v2"


class TestConfigRowPath:
    """Tests for config_row_path()."""

    def test_config_row_path(self) -> None:
        """Row naming template produces correct path segment."""
        template = "rows/{config_row_name}"
        result = config_row_path(template, "First Row")

        assert result == "rows/first-row"


class TestSanitizeName:
    """Tests for sanitize_name()."""

    def test_sanitize_name_lowercase(self) -> None:
        """Name is converted to lowercase."""
        assert sanitize_name("My Config") == "my-config"

    def test_sanitize_name_special_chars(self) -> None:
        """Non-alphanumeric characters (except hyphens) become hyphens."""
        assert sanitize_name("API (v2) Config!") == "api-v2-config"

    def test_sanitize_name_collapse_hyphens(self) -> None:
        """Multiple consecutive hyphens are collapsed into one."""
        assert sanitize_name("a---b") == "a-b"

    def test_sanitize_name_strip_hyphens(self) -> None:
        """Leading and trailing hyphens are stripped."""
        assert sanitize_name("-leading-") == "leading"

    def test_sanitize_name_max_length(self) -> None:
        """Names exceeding max length are truncated."""
        long_name = "a" * 200
        result = sanitize_name(long_name)
        assert len(result) == 100

    def test_sanitize_name_already_clean(self) -> None:
        """Clean lowercase name passes through unchanged."""
        assert sanitize_name("simple-name") == "simple-name"

    def test_sanitize_name_numbers(self) -> None:
        """Numbers are preserved."""
        assert sanitize_name("Config 42 Test") == "config-42-test"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("UPPERCASE", "uppercase"),
            ("MiXeD CaSe", "mixed-case"),
            ("with.dots.here", "with-dots-here"),
            ("under_scores", "under-scores"),
            ("  spaces  ", "spaces"),
        ],
    )
    def test_sanitize_name_various(self, name: str, expected: str) -> None:
        """Various input patterns are sanitized correctly."""
        assert sanitize_name(name) == expected


class TestSanitizePathSegment:
    """Tests for sanitize_path_segment() -- issue #269 sec-01/sec-07."""

    def test_preserves_legitimate_component_id(self) -> None:
        """Realistic component IDs with dots and hyphens pass through."""
        assert sanitize_path_segment("keboola.ex-db-mysql") == "keboola.ex-db-mysql"
        assert sanitize_path_segment("kds-team.app-custom-python") == "kds-team.app-custom-python"
        assert (
            sanitize_path_segment("keboola.python-transformation-v2")
            == "keboola.python-transformation-v2"
        )

    def test_preserves_simple_component_type(self) -> None:
        """Plain component_type values pass through unchanged."""
        assert sanitize_path_segment("extractor") == "extractor"
        assert sanitize_path_segment("transformation") == "transformation"
        assert sanitize_path_segment("application") == "application"

    @pytest.mark.parametrize(
        "evil",
        [
            "../../etc",
            "..",
            "../",
            "foo/../bar",
            "foo/bar",
            "foo\\bar",
            "/etc/passwd",
            ".",
            "./relative",
            "..//..//etc",
            "../../../tmp/foo",
            "with spaces",
        ],
    )
    def test_neutralizes_traversal(self, evil: str) -> None:
        """Path-traversal payloads are neutralized -- the resulting segment
        contains no slash, backslash, or ``..`` parent reference."""
        result = sanitize_path_segment(evil)
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result
        assert result, f"sanitize_path_segment({evil!r}) returned empty"

    def test_empty_input_returns_underscore(self) -> None:
        """Empty input returns ``_`` so the path always has a non-empty segment."""
        assert sanitize_path_segment("") == "_"
        assert sanitize_path_segment("/") == "_"
        assert sanitize_path_segment("...") == "_"


class TestConfigPathTraversalRegression:
    """Issue #269 sec-01/sec-07 regression tests for config_path()."""

    def test_path_traversal_in_component_id_neutralized(self) -> None:
        """API-controlled component_id with ``../`` cannot escape the template."""
        template = "{component_type}/{component_id}/{config_name}"
        result = config_path(template, "other", "../../../etc", "passwd")
        assert "/.." not in result
        assert ".." not in result.split("/")[1]  # the component_id segment

    def test_path_traversal_in_component_type_neutralized(self) -> None:
        """component_type with ``../`` is also sanitized (sec-07)."""
        template = "{component_type}/{component_id}/{config_name}"
        result = config_path(template, "other/../../../tmp", "keboola.ex", "test")
        # sanitize_path_segment turns the slashes into hyphens -> single segment
        first_segment = result.split("/")[0]
        assert ".." not in first_segment
        assert "/" not in first_segment
