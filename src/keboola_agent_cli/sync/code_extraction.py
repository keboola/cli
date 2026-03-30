"""Extract and merge embedded code from Keboola configurations.

On pull: extracts SQL/Python code from config parameters into separate files.
On push: reads code files back into config parameters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _strip_trailing_empty(lines: list[str]) -> list[str]:
    """Remove trailing empty lines but preserve leading whitespace."""
    result = list(lines)
    while result and result[-1].strip() == "":
        result.pop()
    return result


def _lines_to_script(lines: list[str]) -> list[str]:
    """Convert collected lines into a single-string script element.

    The Keboola transformation runner treats each element of the ``script``
    array as a separate executable statement.  Joining all lines of a CODE
    block into one string ensures multi-line SQL/Python is executed as a
    single unit.
    """
    stripped = _strip_trailing_empty(lines)
    if not stripped:
        return []
    return ["\n".join(stripped)]


# Component patterns that contain SQL transformations
SQL_TRANSFORMATION_COMPONENTS: set[str] = {
    "keboola.snowflake-transformation",
    "keboola.synapse-transformation",
    "keboola.oracle-transformation",
    "keboola.redshift-sql-transformation",
}

# Component patterns that contain Python transformations
PYTHON_TRANSFORMATION_COMPONENTS: set[str] = {
    "keboola.python-transformation-v2",
}

# Components with embedded Python code (custom apps)
PYTHON_APP_COMPONENTS: set[str] = {
    "kds-team.app-custom-python",
}

SQL_BLOCK_MARKER = "/* ===== BLOCK: {name} ===== */"
SQL_CODE_MARKER = "/* ===== CODE: {name} ===== */"
PYTHON_BLOCK_MARKER = "# ===== BLOCK: {name} ====="
PYTHON_CODE_MARKER = "# ===== CODE: {name} ====="
DESCRIPTION_FILENAME = "_description.md"


def _extract_description(config_data: dict[str, Any], config_dir: Path) -> None:
    """Extract description field into _description.md."""
    description = config_data.get("description", "")
    if not description:
        return
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / DESCRIPTION_FILENAME).write_text(description, encoding="utf-8")
    del config_data["description"]


def _merge_description(config_data: dict[str, Any], config_dir: Path) -> None:
    """Read _description.md back into config_data."""
    desc_file = config_dir / DESCRIPTION_FILENAME
    if desc_file.exists():
        config_data["description"] = desc_file.read_text(encoding="utf-8")


def extract_code_files(
    component_id: str,
    config_data: dict[str, Any],
    config_dir: Path,
) -> dict[str, Any]:
    """Extract embedded code from config into separate files.

    Modifies config_data in place to remove extracted code.
    Writes code files to config_dir.
    Returns the modified config_data.
    """
    _extract_description(config_data, config_dir)
    if component_id in SQL_TRANSFORMATION_COMPONENTS:
        return _extract_sql_transformation(config_data, config_dir)
    if component_id in PYTHON_TRANSFORMATION_COMPONENTS:
        return _extract_python_transformation(config_data, config_dir)
    if component_id in PYTHON_APP_COMPONENTS:
        return _extract_python_app(config_data, config_dir)
    return config_data


def merge_code_files(
    component_id: str,
    config_data: dict[str, Any],
    config_dir: Path,
) -> dict[str, Any]:
    """Read code files and merge them back into config_data.

    Reverse of extract_code_files. Called before push.
    Returns the modified config_data.
    """
    _merge_description(config_data, config_dir)
    if component_id in SQL_TRANSFORMATION_COMPONENTS:
        return _merge_sql_transformation(config_data, config_dir)
    if component_id in PYTHON_TRANSFORMATION_COMPONENTS:
        return _merge_python_transformation(config_data, config_dir)
    if component_id in PYTHON_APP_COMPONENTS:
        return _merge_python_app(config_data, config_dir)
    return config_data


# ---- SQL Transformations ----


def _extract_sql_transformation(config_data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Extract SQL blocks from parameters.blocks into transform.sql."""
    parameters = config_data.get("parameters", {})
    blocks = parameters.get("blocks", [])

    if not blocks:
        return config_data

    lines: list[str] = []
    for block in blocks:
        block_name = block.get("name", "unnamed")
        lines.append(SQL_BLOCK_MARKER.format(name=block_name))
        lines.append("")

        for code in block.get("codes", []):
            code_name = code.get("name", "unnamed")
            lines.append(SQL_CODE_MARKER.format(name=code_name))

            scripts = code.get("script") or []
            for script in scripts:
                if isinstance(script, str) and "\n" in script:
                    lines.extend(script.split("\n"))
                else:
                    lines.append(script)
            lines.append("")

    sql_content = "\n".join(lines).rstrip() + "\n"

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "transform.sql").write_text(sql_content, encoding="utf-8")

    # Remove blocks from parameters (they're now in the SQL file)
    parameters.pop("blocks", None)

    return config_data


def _merge_sql_transformation(config_data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Read transform.sql and parse block markers back into parameters.blocks."""
    sql_file = config_dir / "transform.sql"
    if not sql_file.exists():
        return config_data

    content = sql_file.read_text(encoding="utf-8")
    blocks = _parse_sql_blocks(content)

    parameters = config_data.setdefault("parameters", {})
    parameters["blocks"] = blocks

    return config_data


def _parse_sql_blocks(content: str) -> list[dict[str, Any]]:
    """Parse SQL content with block/code markers into blocks structure."""
    blocks: list[dict[str, Any]] = []
    current_block: dict[str, Any] | None = None
    current_code: dict[str, Any] | None = None
    current_script_lines: list[str] = []

    for line in content.split("\n"):
        stripped = line.strip()

        # Check for block marker
        if stripped.startswith("/* ===== BLOCK:") and stripped.endswith("===== */"):
            # Save previous code if any
            if current_code is not None and current_block is not None:
                current_code["script"] = _lines_to_script(current_script_lines)
                current_block.setdefault("codes", []).append(current_code)
                current_code = None
                current_script_lines = []

            block_name = stripped[len("/* ===== BLOCK:") :].rstrip(" =*/").strip()
            current_block = {"name": block_name, "codes": []}
            blocks.append(current_block)
            continue

        # Check for code marker
        if stripped.startswith("/* ===== CODE:") and stripped.endswith("===== */"):
            # Save previous code if any
            if current_code is not None and current_block is not None:
                current_code["script"] = _lines_to_script(current_script_lines)
                current_block.setdefault("codes", []).append(current_code)
                current_script_lines = []

            code_name = stripped[len("/* ===== CODE:") :].rstrip(" =*/").strip()
            current_code = {"name": code_name}
            continue

        # Regular line - add to current code's script
        if current_code is not None:
            current_script_lines.append(line)

    # Don't forget the last code block
    if current_code is not None and current_block is not None:
        current_code["script"] = _lines_to_script(current_script_lines)
        current_block.setdefault("codes", []).append(current_code)

    # If no markers found, treat entire content as single block/code
    if not blocks and content.strip():
        blocks = [
            {
                "name": "Block 1",
                "codes": [{"name": "Code 1", "script": _lines_to_script(content.split("\n"))}],
            }
        ]

    return blocks


# ---- Python Transformations ----


def _extract_python_transformation(config_data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Extract Python blocks from parameters.blocks into transform.py, packages into pyproject.toml."""
    parameters = config_data.get("parameters", {})
    blocks = parameters.get("blocks", [])

    if blocks:
        lines: list[str] = []
        for block in blocks:
            block_name = block.get("name", "unnamed")
            lines.append(PYTHON_BLOCK_MARKER.format(name=block_name))
            lines.append("")

            for code in block.get("codes", []):
                code_name = code.get("name", "unnamed")
                lines.append(PYTHON_CODE_MARKER.format(name=code_name))

                scripts = code.get("script") or []
                for script in scripts:
                    if isinstance(script, str) and "\n" in script:
                        lines.extend(script.split("\n"))
                    else:
                        lines.append(script)
                lines.append("")

        py_content = "\n".join(lines).rstrip() + "\n"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "transform.py").write_text(py_content, encoding="utf-8")

        # Remove blocks from parameters
        parameters.pop("blocks", None)

    # Extract packages to pyproject.toml
    packages = parameters.get("packages", [])
    if packages:
        config_name = config_data.get("name", "transformation")
        _write_pyproject_toml(config_dir, config_name, packages, component_id=None, config_id=None)
        parameters.pop("packages", None)

    return config_data


def _merge_python_transformation(config_data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Read transform.py and pyproject.toml back into config_data."""
    py_file = config_dir / "transform.py"
    if py_file.exists():
        content = py_file.read_text(encoding="utf-8")
        blocks = _parse_python_blocks(content)
        parameters = config_data.setdefault("parameters", {})
        parameters["blocks"] = blocks

    # Read packages from pyproject.toml
    packages = _read_pyproject_packages(config_dir)
    if packages:
        parameters = config_data.setdefault("parameters", {})
        parameters["packages"] = packages

    return config_data


def _parse_python_blocks(content: str) -> list[dict[str, Any]]:
    """Parse Python content with block/code markers into blocks structure."""
    blocks: list[dict[str, Any]] = []
    current_block: dict[str, Any] | None = None
    current_code: dict[str, Any] | None = None
    current_script_lines: list[str] = []

    for line in content.split("\n"):
        stripped = line.strip()

        if stripped.startswith("# ===== BLOCK:") and stripped.endswith("====="):
            if current_code is not None and current_block is not None:
                current_code["script"] = _lines_to_script(current_script_lines)
                current_block.setdefault("codes", []).append(current_code)
                current_code = None
                current_script_lines = []

            block_name = stripped[len("# ===== BLOCK:") :].rstrip(" =").strip()
            current_block = {"name": block_name, "codes": []}
            blocks.append(current_block)
            continue

        if stripped.startswith("# ===== CODE:") and stripped.endswith("====="):
            if current_code is not None and current_block is not None:
                current_code["script"] = _lines_to_script(current_script_lines)
                current_block.setdefault("codes", []).append(current_code)
                current_script_lines = []

            code_name = stripped[len("# ===== CODE:") :].rstrip(" =").strip()
            current_code = {"name": code_name}
            continue

        if current_code is not None:
            current_script_lines.append(line)

    if current_code is not None and current_block is not None:
        current_code["script"] = _lines_to_script(current_script_lines)
        current_block.setdefault("codes", []).append(current_code)

    if not blocks and content.strip():
        blocks = [
            {
                "name": "Block 1",
                "codes": [{"name": "Code 1", "script": _lines_to_script(content.split("\n"))}],
            }
        ]

    return blocks


# ---- Python Apps ----


def _extract_python_app(config_data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Extract parameters.code into code.py and packages into pyproject.toml."""
    parameters = config_data.get("parameters", {})

    code = parameters.get("code")
    if code and isinstance(code, str):
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "code.py").write_text(code, encoding="utf-8")
        parameters.pop("code", None)

    packages = parameters.get("packages", [])
    if packages:
        keboola_meta = config_data.get("_keboola", {})
        _write_pyproject_toml(
            config_dir,
            config_data.get("name", "app"),
            packages,
            component_id=keboola_meta.get("component_id"),
            config_id=keboola_meta.get("config_id"),
        )
        parameters.pop("packages", None)

    return config_data


def _merge_python_app(config_data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Read code.py and pyproject.toml back into config_data."""
    code_file = config_dir / "code.py"
    if code_file.exists():
        parameters = config_data.setdefault("parameters", {})
        parameters["code"] = code_file.read_text(encoding="utf-8")

    packages = _read_pyproject_packages(config_dir)
    if packages:
        parameters = config_data.setdefault("parameters", {})
        parameters["packages"] = packages

    return config_data


# ---- pyproject.toml helpers ----


def _write_pyproject_toml(
    config_dir: Path,
    name: str,
    packages: list[str],
    component_id: str | None = None,
    config_id: str | None = None,
) -> None:
    """Write packages to a pyproject.toml file."""
    config_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize name for pyproject
    safe_name = name.lower().replace(" ", "-").replace("_", "-")

    lines = [
        "[project]",
        f'name = "{safe_name}"',
        'requires-python = ">=3.11"',
        "dependencies = [",
    ]
    for pkg in packages:
        lines.append(f'    "{pkg}",')
    lines.append("]")

    if component_id or config_id:
        lines.append("")
        lines.append("[tool.keboola]")
        if component_id:
            lines.append(f'component_id = "{component_id}"')
        if config_id:
            lines.append(f'config_id = "{config_id}"')

    lines.append("")  # trailing newline
    (config_dir / "pyproject.toml").write_text("\n".join(lines), encoding="utf-8")


def _read_pyproject_packages(config_dir: Path) -> list[str]:
    """Read packages from pyproject.toml dependencies."""
    toml_file = config_dir / "pyproject.toml"
    if not toml_file.exists():
        return []

    content = toml_file.read_text(encoding="utf-8")
    packages: list[str] = []
    in_deps = False

    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "dependencies = [":
            in_deps = True
            continue
        if in_deps:
            if stripped == "]":
                break
            # Strip quotes and trailing comma
            pkg = stripped.strip('", ')
            if pkg:
                packages.append(pkg)

    return packages
