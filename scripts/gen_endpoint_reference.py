#!/usr/bin/env python3
"""Generate docs/web-server-endpoints.md by introspecting the live FastAPI app.

Zero-drift by construction: the output is derived from ``create_app().openapi()``
-- the same spec ``kbagent serve`` publishes at ``/openapi.json`` -- so the
committed reference cannot disagree with the shipped server. This exists because
the hand-maintained table in docs/web-server.md drifted badly (issue #656): it
claimed "150+ endpoints" across 17 rows while the app served 226 operations
across 29 routers, with ten routers missing entirely. Nothing caught it, because
no OpenAPI spec is committed and only a running server exposes the truth.

Section structure is NOT invented here. Every operation is tagged, and
``server/app.py::OPENAPI_TAGS`` already carries a hand-written description per
tag whose first bold span is a category ("**Data.**", "**Execution.**") and
whose tail maps the tag onto its CLI surface ("Mirrors `kbagent config *`").
This script groups by that existing metadata instead of duplicating it, so the
prose stays authored by a human and only the enumeration is mechanical.

Deliberately NOT in the output: the kbagent version. ``gen_command_reference.py``
stamps one because it publishes a release asset pinned to a version; this file is
git-tracked and gated by ``make endpoints-check``, so a version stamp would make
every release bump the doc and fail the gate for reasons unrelated to the API.

Usage (run from repo root):
    python scripts/gen_endpoint_reference.py                 # write the doc
    python scripts/gen_endpoint_reference.py --stdout        # print instead
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from keboola_agent_cli.server.app import OPENAPI_TAGS, create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "web-server-endpoints.md"

# The HTTP methods that count as documented operations. FastAPI also registers
# HEAD/OPTIONS handlers implicitly; they carry no API meaning and are excluded so
# the totals match what a client can actually call.
METHODS = ("get", "post", "put", "patch", "delete")

# Order sections by the categories used in OPENAPI_TAGS. Anything whose category
# is not listed here still renders -- appended in declaration order under its own
# heading -- so adding a category to app.py can never silently drop its routes.
CATEGORY_ORDER = (
    "Project Management",
    "Configurations",
    "Data",
    "Execution",
    "Development",
    "AI & Tools",
    "Read-only",
    "System",
)

_CATEGORY_RE = re.compile(r"^\*\*(?P<category>[^*]+?)\.?\*\*\s*(?P<rest>.*)$", re.DOTALL)


@dataclass(frozen=True)
class Operation:
    """One documented HTTP operation."""

    method: str
    path: str
    summary: str


@dataclass(frozen=True)
class TagSection:
    """One OpenAPI tag with its category, prose, and operations."""

    name: str
    category: str
    description: str
    operations: tuple[Operation, ...]


def _split_category(description: str) -> tuple[str, str]:
    """Split a tag description into its leading bold category and the rest.

    ``"**Data.** Browse buckets..."`` becomes ``("Data", "Browse buckets...")``.
    A description without the bold prefix degrades to the "Other" category with
    its text intact rather than being dropped or mangled.
    """
    match = _CATEGORY_RE.match(description.strip())
    if not match:
        return "Other", description.strip()
    return match.group("category").strip(), match.group("rest").strip()


def _collect_operations(spec: dict) -> dict[str, list[Operation]]:
    """Map tag name -> operations, preserving route-registration order."""
    by_tag: dict[str, list[Operation]] = {}
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            summary = (operation.get("summary") or "").strip()
            for tag in operation.get("tags") or ["(untagged)"]:
                by_tag.setdefault(tag, []).append(
                    Operation(method=method.upper(), path=path, summary=summary)
                )
    return by_tag


def build_sections(spec: dict) -> list[TagSection]:
    """Build one section per tag, ordered by category then by declaration order.

    Tags are enumerated from ``OPENAPI_TAGS`` first so the authored order wins;
    any tag that appears on a route without being declared there is appended
    afterwards (with an empty description) rather than being silently omitted.
    """
    by_tag = _collect_operations(spec)
    declared = {tag["name"]: tag.get("description", "") for tag in OPENAPI_TAGS}

    sections: list[TagSection] = []
    for name, description in declared.items():
        category, prose = _split_category(description)
        sections.append(
            TagSection(
                name=name,
                category=category,
                description=prose,
                operations=tuple(by_tag.get(name, ())),
            )
        )
    for name in by_tag:
        if name not in declared:
            sections.append(
                TagSection(
                    name=name, category="Other", description="", operations=tuple(by_tag[name])
                )
            )

    def sort_key(entry: tuple[int, TagSection]) -> tuple[int, int]:
        position, section = entry
        try:
            rank = CATEGORY_ORDER.index(section.category)
        except ValueError:
            rank = len(CATEGORY_ORDER)
        # Declaration order is the tie-break, so tags keep the sequence a human
        # chose in OPENAPI_TAGS instead of being re-sorted alphabetically.
        return (rank, position)

    return [section for _, section in sorted(enumerate(sections), key=sort_key)]


def _render_section(section: TagSection) -> list[str]:
    count = len(section.operations)
    plural = "operation" if count == 1 else "operations"
    lines = [f"### `{section.name}` ({count} {plural})", ""]
    if section.description:
        lines += [section.description, ""]
    if not section.operations:
        return [*lines, "_No operations registered._", ""]
    lines += ["| Method | Path | Summary |", "|---|---|---|"]
    lines += [f"| `{op.method}` | `{op.path}` | {op.summary} |" for op in section.operations]
    lines.append("")
    return lines


def build_reference() -> str:
    """Render the whole endpoint reference as markdown."""
    # A throwaway config dir keeps the run hermetic: create_app() resolves a
    # project registry from disk, and reading the developer's real config would
    # make the generator's behaviour depend on whose machine it ran on.
    with tempfile.TemporaryDirectory() as config_dir:
        spec = create_app(config_dir=config_dir, auth_token="generator").openapi()

    sections = build_sections(spec)
    operations = sum(len(section.operations) for section in sections)
    paths = len([p for p, item in spec.get("paths", {}).items() if any(m in item for m in METHODS)])

    out = [
        "# `kbagent serve` — REST endpoint reference",
        "",
        "<!-- GENERATED FILE -- DO NOT EDIT BY HAND.",
        "     Regenerate with `make endpoints-gen`; `make endpoints-check` gates it in CI. -->",
        "",
        "Generated from the live FastAPI app by `scripts/gen_endpoint_reference.py`,",
        "so this file cannot disagree with the server it documents. Architecture,",
        "auth, and the concepts behind these routes live in",
        "[`web-server.md`](web-server.md); a running server serves the same spec",
        "interactively at `/docs` (Swagger) and `/openapi.json`.",
        "",
        f"**{operations} operations** across **{paths} paths** and **{len(sections)} routers**.",
        "",
        "Paths are shown as the server registers them. Reaching them through the",
        "Node BFF (or single-process `--ui` mode) prefixes every path with `/api`.",
        "",
    ]

    current_category = None
    for section in sections:
        if section.category != current_category:
            current_category = section.category
            out += [f"## {current_category}", ""]
        out += _render_section(section)

    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the serve endpoint reference.")
    parser.add_argument(
        "--stdout", action="store_true", help="Print to stdout instead of writing the doc"
    )
    args = parser.parse_args()

    reference = build_reference()
    if args.stdout:
        sys.stdout.write(reference)
        return 0
    OUTPUT.write_text(reference, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
