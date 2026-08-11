"""The declared licence must match the LICENSE file, everywhere it is declared.

kbagent declares its licence in five places that nothing kept in sync: the
Python distribution metadata, the deb/rpm package, the Homebrew formula, the
Claude Code plugin manifest and the README. #544 added an Apache 2.0 `LICENSE`
file while all five still said MIT, and v0.79.0 shipped that contradiction --
the wheel told PyPI one licence while the file in the same repo said another.
No CI check noticed, because each file is individually valid.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
LICENSE_PATH = REPO_ROOT / "LICENSE"

#: SPDX identifier the repository is licensed under. Change this ONLY together
#: with the LICENSE file itself -- every assertion below hangs off it.
EXPECTED_SPDX = "Apache-2.0"

#: How each packaging file spells the same licence.
_DECLARATIONS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", rf'^license = "{re.escape(EXPECTED_SPDX)}"$'),
    ("build/package/nfpm.yaml", rf'^license: "{re.escape(EXPECTED_SPDX)}"$'),
    (
        "build/package/homebrew/keboola-cli2.rb.tmpl",
        rf'^\s*license "{re.escape(EXPECTED_SPDX)}"$',
    ),
)


def test_license_file_is_the_expected_licence() -> None:
    """Anchor: the other assertions are only meaningful against the real file."""
    text = LICENSE_PATH.read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0" in text


@pytest.mark.parametrize(("relative_path", "pattern"), _DECLARATIONS)
def test_packaging_file_declares_the_same_licence(relative_path: str, pattern: str) -> None:
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert re.search(pattern, text, re.MULTILINE), (
        f"{relative_path} does not declare {EXPECTED_SPDX}; it must match the LICENSE file"
    )


def test_plugin_manifest_declares_the_same_licence() -> None:
    manifest = json.loads(
        (REPO_ROOT / "plugins/kbagent/.claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["license"] == EXPECTED_SPDX


def test_readme_does_not_still_claim_mit() -> None:
    """The README is what a human reads before the metadata."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## License", 1)
    assert len(section) == 2, "README has no License section"
    body = section[1][:200]
    assert "MIT" not in body
    assert "Apache" in body


def test_chocolatey_points_its_license_url_at_the_repo_license() -> None:
    """The nuspec has no SPDX field; its licenceUrl is the whole declaration.

    A URL to the LICENSE file self-updates with the repository, which is why
    this channel never went stale -- but only as long as it points THERE and
    not at a hard-coded licence page.
    """
    nuspec = (REPO_ROOT / "build/package/chocolatey/keboola-cli2.nuspec").read_text(
        encoding="utf-8"
    )
    assert "<licenseUrl>https://github.com/keboola/cli/blob/main/LICENSE</licenseUrl>" in nuspec


def test_no_packaging_file_still_says_mit() -> None:
    """Catch a sixth declaration site added later without updating this test."""
    offenders = []
    for relative_path in (
        "pyproject.toml",
        "build/package/nfpm.yaml",
        "build/package/homebrew/keboola-cli2.rb.tmpl",
        "plugins/kbagent/.claude-plugin/plugin.json",
        "README.md",
    ):
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        if re.search(r"\bMIT\b", text):
            offenders.append(relative_path)
    assert not offenders, f"still declaring MIT: {offenders}"
