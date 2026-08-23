"""Tests for scripts/gen_endpoint_reference.py (serve endpoint reference generator).

The generator exists because a hand-maintained endpoint table drifted from 17
rows to a 226-operation server without anything noticing (issue #656). These
tests guard the two properties that make the replacement trustworthy: the
output enumerates *every* operation the app serves, and it is stable enough to
gate on (deterministic, and free of the version stamp that would make every
release bump the doc).
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the endpoint reference needs the `server` extra")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "gen_endpoint_reference", Path("scripts") / "gen_endpoint_reference.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: the script's @dataclass under
    # `from __future__ import annotations` resolves its module via
    # sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load_script()


@pytest.fixture(scope="module")
def reference(script) -> str:
    return script.build_reference()


@pytest.fixture(scope="module")
def spec(script) -> dict:
    from keboola_agent_cli.server.app import create_app

    with tempfile.TemporaryDirectory() as config_dir:
        return create_app(config_dir=config_dir, auth_token="test").openapi()


def _spec_operations(script, spec: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in spec.get("paths", {}).items()
        for method in item
        if method in script.METHODS
    }


def _rendered_operations(reference: str) -> set[tuple[str, str]]:
    return set(re.findall(r"^\| `([A-Z]+)` \| `([^`]+)` \|", reference, re.MULTILINE))


class TestGenEndpointReference:
    def test_deterministic(self, script, reference: str) -> None:
        """Two runs are byte-identical -- otherwise the CI gate would flap."""
        assert reference == script.build_reference()

    def test_renders_every_operation_the_app_serves(
        self, script, reference: str, spec: dict
    ) -> None:
        """The whole point: no route can be served without appearing here.

        This is the assertion the old hand-written table could not make. Ten
        routers were missing from it and nothing failed.
        """
        assert _rendered_operations(reference) == _spec_operations(script, spec)

    def test_totals_line_matches_what_is_rendered(self, reference: str) -> None:
        """The headline count is derived, not asserted -- so it cannot lie."""
        match = re.search(
            r"\*\*(\d+) operations\*\* across \*\*(\d+) paths\*\* and \*\*(\d+) routers\*\*",
            reference,
        )
        assert match, "totals line missing or reworded"
        operations, paths, routers = (int(g) for g in match.groups())
        rendered = _rendered_operations(reference)
        assert operations == len(rendered)
        assert paths == len({path for _, path in rendered})
        assert routers == len(re.findall(r"^### `", reference, re.MULTILINE))

    def test_carries_no_version_stamp(self, reference: str) -> None:
        """A version in a gated file makes every release bump it for no reason.

        gen_command_reference.py deliberately does stamp one -- it publishes a
        release asset. This file is git-tracked and gated, so it must not.
        """
        from keboola_agent_cli import __version__

        assert __version__ not in reference

    def test_committed_doc_is_up_to_date(self, script, reference: str) -> None:
        """Mirrors `make endpoints-check`, so a stale doc fails pytest too."""
        committed = script.OUTPUT.read_text(encoding="utf-8")
        assert committed == reference, (
            "docs/web-server-endpoints.md is stale -- run `make endpoints-gen` and commit."
        )


class TestCategorySplitting:
    def test_splits_bold_category_prefix(self, script) -> None:
        category, rest = script._split_category("**Data.** Browse buckets and tables.")
        assert category == "Data"
        assert rest == "Browse buckets and tables."

    def test_description_without_prefix_keeps_its_text(self, script) -> None:
        """Degrade to 'Other' rather than dropping or mangling the prose."""
        category, rest = script._split_category("Just a description.")
        assert category == "Other"
        assert rest == "Just a description."

    def test_every_declared_tag_has_a_known_category(self, script) -> None:
        """A new tag whose category is unlisted still renders, but flag the typo.

        CATEGORY_ORDER is a sort key, not a filter -- an unknown category sorts
        last instead of disappearing. This test catches the likelier mistake: a
        category miswritten in app.py that silently forms its own section.
        """
        from keboola_agent_cli.server.app import OPENAPI_TAGS

        unknown = {
            tag["name"]: script._split_category(tag.get("description", ""))[0]
            for tag in OPENAPI_TAGS
            if script._split_category(tag.get("description", ""))[0] not in script.CATEGORY_ORDER
        }
        assert not unknown, f"tags with an unrecognised category prefix: {unknown}"


class TestSectionBuilding:
    def test_undeclared_tag_is_not_dropped(self, script) -> None:
        """A route tagged with something app.py never declared must still show."""
        fake_spec = {
            "paths": {
                "/made-up": {"get": {"tags": ["not-declared"], "summary": "Invented"}},
            }
        }
        sections = script.build_sections(fake_spec)
        by_name = {section.name: section for section in sections}
        assert "not-declared" in by_name
        assert by_name["not-declared"].category == "Other"
        assert by_name["not-declared"].operations[0].path == "/made-up"

    def test_declared_tag_with_no_routes_renders_empty(self, script) -> None:
        """An empty router is reported, not omitted -- that is usually a bug."""
        sections = script.build_sections({"paths": {}})
        assert sections, "declared tags should still produce sections"
        assert all(section.operations == () for section in sections)
        rendered = script._render_section(sections[0])
        assert "_No operations registered._" in rendered

    def test_sections_are_grouped_by_category_in_order(self, script, spec: dict) -> None:
        """Categories appear contiguously and in CATEGORY_ORDER."""
        sections = script.build_sections(spec)
        seen: list[str] = []
        for section in sections:
            if not seen or seen[-1] != section.category:
                seen.append(section.category)
        assert len(seen) == len(set(seen)), f"category interleaved across sections: {seen}"
        ranks = [script.CATEGORY_ORDER.index(c) for c in seen if c in script.CATEGORY_ORDER]
        assert ranks == sorted(ranks)
