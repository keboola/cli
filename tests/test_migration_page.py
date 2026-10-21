"""The migration page that the 0.N.x deprecation notice links to exists.

The notice prints https://github.com/keboola/cli/blob/main/docs/migrate-to-1.0.md
and tells the reader to move early, stay on 0.x or check the changes. A moved or
renamed file, or a dropped section, breaks that link for every 0.x install. A
broken link inside the page sends the reader nowhere at the moment they need it.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION_PAGE = Path(__file__).resolve().parent.parent / "docs" / "migrate-to-1.0.md"

NOTICE_SECTIONS = (
    "## What happens and when",
    "## Move to 1.0 early",
    "## Stay on 0.x",
    "## Go back to 0.x",
    "## Changes in 1.0.0",
)

# The rest of the content that section 4.5 of the #791 proposal names.
CONTENT_SECTIONS = (
    "## Envelope and exit codes",
    "## Changed defaults",
    "## REST API",
    "## SDK",
    "## Plugin",
    "## Install channels",
)

_LINK = re.compile(r"\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\s-]", "", heading.replace("`", "").lower())
    return text.replace(" ", "-")


def _text() -> str:
    return MIGRATION_PAGE.read_text(encoding="utf-8")


def _outside_fences(text: str) -> list[str]:
    lines, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            lines.append(line)
    return lines


def test_migration_page_exists() -> None:
    assert MIGRATION_PAGE.is_file()


def test_migration_page_has_the_sections_the_notice_relies_on() -> None:
    headings = set(_text().splitlines())
    missing = [s for s in (*NOTICE_SECTIONS, *CONTENT_SECTIONS) if s not in headings]
    assert not missing, f"docs/migrate-to-1.0.md lacks: {missing}"


def test_change_rows_have_unique_numbers_in_order() -> None:
    """Other pages and the changelog cite a change by its row number."""
    section = _text().split("## Changes in 1.0.0", 1)[1].split("\n## ", 1)[0]
    cells = [line.split("|")[1].strip() for line in section.splitlines() if line.startswith("| ")]
    numbers = [cell for cell in cells[2:] if cell != "---"]
    assert all(n.isdigit() for n in numbers), numbers
    assert [int(n) for n in numbers] == sorted({int(n) for n in numbers})


def test_migration_page_links_resolve() -> None:
    lines = _outside_fences(_text())
    anchors = {_slug(m.group(1)) for line in lines if (m := _HEADING.match(line))}
    broken = []
    for line in lines:
        for target in _LINK.findall(line):
            if target.startswith(("http://", "https://")):
                continue
            path, _, anchor = target.partition("#")
            resolves = (MIGRATION_PAGE.parent / path).exists() if path else anchor in anchors
            if not resolves:
                broken.append(target)
    assert not broken, f"docs/migrate-to-1.0.md has broken links: {broken}"


def test_slug_matches_github() -> None:
    assert _slug("Changes in 1.0.0") == "changes-in-100"
    assert _slug("Stay on 0.x") == "stay-on-0x"
