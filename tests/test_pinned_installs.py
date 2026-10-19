"""No install of kbagent from the git source without a release tag (#791, CLI-39).

``main`` carries the code of the next major before that major is released. An
install from ``git+https://github.com/keboola/cli`` without ``@v<version>`` builds
``main``, so it can install unreleased code. Documented installs use ``install.sh``
(the highest stable version from PyPI) or a pinned tag. This test scans the
repository text files and fails on a new untagged git install.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# A git source URL of this repository; group "ref" is the text after "@", if any.
GIT_SOURCE = re.compile(
    r"git\+(?:https|ssh)://(?:git@)?github\.com[/:]keboola/cli(?:\.git)?"
    r"(?![\w.-])(?:@(?P<ref>[^\s'\"`)]+))?"
)
# A release tag (v0.99.1) or a placeholder for one ({git_ref}, v${version}, v<version>, <tag>).
# Any other ref (a branch, a commit) is refused.
TAGGED_REF = re.compile(r"v?[{$<]|v\d")

SCANNED_FILES = (
    "README.md", "CLAUDE.md", "CONTRIBUTING.md", "install.sh", "pyproject.toml", "Makefile",
)  # fmt: skip
SCANNED_DIRS = ("docs", "plugins", "src", "scripts", ".github", "build", "examples", "web")
SKIPPED_DIRS = {"node_modules", "dist", "_ui_dist", "__pycache__"}
TEXT_SUFFIXES = {
    ".md", ".py", ".sh", ".yml", ".yaml", ".json", ".toml", ".rb", ".tmpl",
    ".ps1", ".txt", ".ts", ".tsx", ".js", ".html", ".nuspec",
}  # fmt: skip

# Intended exceptions: (path, text of the line). Keep this list short.
ALLOWED = {
    # The base URL; every use appends "@v<version>" (services/version_service.py).
    (
        "src/keboola_agent_cli/constants.py",
        'KBAGENT_INSTALL_SOURCE: str = "git+https://github.com/keboola/cli"',
    ),
}
# Records of the past that must stay as they were written.
ALLOWED_FILES = {
    "src/keboola_agent_cli/changelog.py",  # published release notes
    "docs/adr/0003-release-distribution-cicd.md",  # describes the state before the ADR
}


def _scanned_paths() -> list[Path]:
    paths = [REPO_ROOT / name for name in SCANNED_FILES]
    paths += sorted(REPO_ROOT.glob("Dockerfile*"))
    for top in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / top).rglob("*")):
            relative = path.relative_to(REPO_ROOT)
            if SKIPPED_DIRS.intersection(relative.parts):
                continue
            # A file without a suffix is a script (scripts/pre-commit) or similar.
            if path.is_file() and (path.suffix in TEXT_SUFFIXES or not path.suffix):
                paths.append(path)
    return paths


def find_untagged_installs(text: str) -> list[tuple[int, str]]:
    """Return (line number, line) for each untagged git source URL in ``text``."""
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in GIT_SOURCE.finditer(line):
            ref = match.group("ref")
            if ref is None or not TAGGED_REF.match(ref):
                hits.append((number, line.strip()))
                break
    return hits


@pytest.mark.parametrize(
    "line",
    [
        "uv tool install git+https://github.com/keboola/cli",
        "uv tool install 'git+https://github.com/keboola/cli'",
        'pip install "git+https://github.com/keboola/cli.git"',
        "uvx --from git+https://github.com/keboola/cli@main kbagent",
        "pip install git+ssh://git@github.com/keboola/cli.git@master",
        "uv tool install 'git+https://github.com/keboola/cli@release/0.105'",
        "uv tool install 'git+https://github.com/keboola/cli@3f2a9c1'",
    ],
)
def test_untagged_form_is_found(line: str) -> None:
    assert find_untagged_installs(line) == [(1, line)]


@pytest.mark.parametrize(
    "line",
    [
        "uv tool install 'git+https://github.com/keboola/cli@v0.99.1'",
        "uv tool install 'git+https://github.com/keboola/cli@v<version>'",
        'spec = f"git+https://github.com/keboola/cli@{git_ref}"',
        'uv tool install "${spec} @ git+https://github.com/${REPO}@v${version}"',
        "git+https://github.com/keboola/cli-other",
    ],
)
def test_tagged_form_is_accepted(line: str) -> None:
    assert find_untagged_installs(line) == []


def test_no_untagged_git_install_in_the_repository() -> None:
    found = []
    for path in _scanned_paths():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWED_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in find_untagged_installs(text):
            if (relative, line) not in ALLOWED:
                found.append(f"{relative}:{number}: {line}")
    assert not found, (
        "Install from git without a release tag builds `main`, which can carry "
        "unreleased code of the next major. Use install.sh or "
        "'git+https://github.com/keboola/cli@v<version>':\n" + "\n".join(found)
    )


def _install_source_uses(tree: ast.AST) -> list[tuple[int, bool]]:
    """Return (line, followed by "@v") for each read of ``KBAGENT_INSTALL_SOURCE``."""
    tagged: dict[int, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part, following in zip(node.values, [*node.values[1:], None], strict=True):
                if (
                    isinstance(part, ast.FormattedValue)
                    and isinstance(part.value, ast.Name)
                    and part.value.id == "KBAGENT_INSTALL_SOURCE"
                ):
                    tagged[id(part.value)] = (
                        isinstance(following, ast.Constant)
                        and isinstance(following.value, str)
                        and following.value.startswith("@v")
                    )
    return [
        (node.lineno, tagged.get(id(node), False))
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "KBAGENT_INSTALL_SOURCE"
        and isinstance(node.ctx, ast.Load)
    ]


def test_install_source_check_finds_an_untagged_use() -> None:
    tree = ast.parse(
        'a = f"{KBAGENT_INSTALL_SOURCE}@v{version}"\n'
        'b = f"{KBAGENT_INSTALL_SOURCE}"\n'
        "c = KBAGENT_INSTALL_SOURCE\n"
    )
    assert sorted(_install_source_uses(tree)) == [(1, True), (2, False), (3, False)]


def test_every_runtime_install_source_is_tagged() -> None:
    """A URL built from ``KBAGENT_INSTALL_SOURCE`` always continues with ``@v``."""
    found = []
    uses = 0
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, tagged in _install_source_uses(tree):
            uses += 1
            if not tagged:
                found.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}")
    assert uses, "KBAGENT_INSTALL_SOURCE is no longer used; update this test"
    assert not found, 'KBAGENT_INSTALL_SOURCE used without "@v<version>":\n' + "\n".join(found)
