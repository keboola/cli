"""Extract and merge embedded code from Keboola configurations.

On pull: extracts SQL/Python code from config parameters into separate files.
On push: reads code files back into config parameters.
"""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from keboola_agent_cli.sync.sql_split import split_statements

logger = logging.getLogger(__name__)

# Explicit statement boundary written into ``transform.sql`` when semicolons
# alone cannot recover the canonical ``script[]`` array -- i.e. when the API's
# elements carry no trailing ``;`` (issue #686 part 3). Without it,
# ``merge_code_files`` collapses several statements into one element and
# ``sync push`` silently rewrites production into the
# ``MULTI_STATEMENT_COUNT=1`` crash shape of issues #119/#120/#274.
#
# Emission is CONDITIONAL: a ``;``-terminated script round-trips on its own, so
# existing trees stay byte-identical. Recognition is an exact full-line match
# after stripping. Auto-appending the missing ``;`` was rejected as the
# alternative: it changes content, and Oracle (ODBC) rejects a trailing
# semicolon outright (ORA-00911). The marker never changes content and is
# backend-neutral.
SQL_STATEMENT_MARKER = "/* ===== STATEMENT ===== */"


def _strip_trailing_empty(lines: list[str]) -> list[str]:
    """Remove trailing empty lines but preserve leading whitespace."""
    result = list(lines)
    while result and result[-1].strip() == "":
        result.pop()
    return result


def canonical_sql_script(script: list[Any]) -> list[str]:
    """Return the canonical ``script[]`` array for a SQL transformation code.

    ONE array element = ONE executable statement (the Keboola runtime's own
    semantics -- the premise of issues #119/#120/#274). Each existing element
    is split independently and the results are flattened; the array is NEVER
    joined first, so two elements without trailing semicolons stay two
    statements instead of silently merging into one (issue #686).

    This is the single producer of that shape: the API side
    (``config_format._normalize_scripts``) and the file side
    (:func:`_lines_to_script`) both agree with it, which is what makes the
    stored ``pull_config_hash`` comparable across pull, push and diff.
    """
    canonical: list[str] = []
    for element in script:
        if isinstance(element, str):
            canonical.extend(split_statements(element))
        elif element is not None:
            canonical.append(element)
    return canonical


def _split_on_statement_markers(lines: list[str]) -> list[list[str]]:
    """Split collected code lines into segments on marker lines."""
    segments: list[list[str]] = [[]]
    for line in lines:
        if line.strip() == SQL_STATEMENT_MARKER:
            segments.append([])
            continue
        segments[-1].append(line)
    return segments


def _lines_to_script(lines: list[str], *, is_sql: bool = False) -> list[str]:
    """Convert collected lines back into the ``script[]`` array.

    For SQL transformations: splits on semicolons using a state machine,
    producing one element per statement (matching Keboola runtime semantics).
    :data:`SQL_STATEMENT_MARKER` lines, when present, are GUARANTEED
    boundaries -- but :func:`split_statements` still runs *within* each
    segment, so a user who types ``; SELECT ...`` inside a marked segment
    still gets it split correctly.
    For Python/other: joins all lines into a single element.
    """
    stripped = _strip_trailing_empty(lines)
    if not stripped:
        return []
    if is_sql and any(line.strip() == SQL_STATEMENT_MARKER for line in stripped):
        script: list[str] = []
        for segment in _split_on_statement_markers(stripped):
            script.extend(split_statements("\n".join(segment)))
        return script
    content = "\n".join(stripped)
    if is_sql:
        return split_statements(content)
    return [content]


# Component patterns that contain SQL transformations.
# Used for exact-match dispatch in pull/push code-file extraction.
# For runtime-shape detection (i.e. "does this component's script[]
# need to be split on statement boundaries?") prefer
# :func:`is_sql_transformation_component` -- it adds fragment-based
# fallback so newer/variant SQL backends are covered without code edits.
SQL_TRANSFORMATION_COMPONENTS: set[str] = {
    "keboola.snowflake-transformation",
    "keboola.synapse-transformation",
    "keboola.oracle-transformation",
    "keboola.redshift-sql-transformation",
    "keboola.google-bigquery-transformation",
    "keboola.duckdb-transformation",
}

# Component patterns that contain Python transformations
PYTHON_TRANSFORMATION_COMPONENTS: set[str] = {
    "keboola.python-transformation-v2",
}

# Components with embedded Python code (custom apps)
PYTHON_APP_COMPONENTS: set[str] = {
    "kds-team.app-custom-python",
}

# Fragment-based detection for SQL transformations. Keeps
# is_sql_transformation_component robust against newer/variant
# component IDs (e.g. ``keboola.snowflake-transformation-v2``,
# self-hosted ``keboola.exasol-transformation``) without requiring
# an edit to SQL_TRANSFORMATION_COMPONENTS for every new backend.
_SQL_TRANSFORMATION_FRAGMENTS: tuple[str, ...] = (
    "snowflake-transformation",
    "synapse-transformation",
    "oracle-transformation",
    "redshift-sql-transformation",
    "redshift-transformation",
    "google-bigquery-transformation",
    "bigquery-transformation",
    "duckdb-transformation",
    "exasol-transformation",
    "teradata-transformation",
)

SQL_BLOCK_MARKER = "/* ===== BLOCK: {name} ===== */"
SQL_CODE_MARKER = "/* ===== CODE: {name} ===== */"
PYTHON_BLOCK_MARKER = "# ===== BLOCK: {name} ====="
PYTHON_CODE_MARKER = "# ===== CODE: {name} ====="
DESCRIPTION_FILENAME = "_description.md"
PYPROJECT_FILENAME = "pyproject.toml"


def is_sql_transformation_component(component_id: str) -> bool:
    """Return True if the component's script[] elements are SQL statements.

    The Keboola runtime treats each ``parameters.blocks[].codes[].script``
    element of a SQL transformation as ONE logical statement. Pushing a
    string instead of an array passes the Storage API (lax validator) but
    crashes at job runtime ("Expected array, got string").

    Combines exact-match (``SQL_TRANSFORMATION_COMPONENTS``) with a
    fragment fallback so newer/variant SQL backends -- including ones
    not yet enumerated in the exact set -- still get correct treatment.
    """
    if component_id in SQL_TRANSFORMATION_COMPONENTS:
        return True
    return any(fragment in component_id for fragment in _SQL_TRANSFORMATION_FRAGMENTS)


def normalize_blocks_codes_script(
    component_id: str,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize ``parameters.blocks[].codes[].script`` for runtime safety.

    Closes two gaps between the Storage API (lax shape validator) and the
    runtime (strict per-element semantics):

    1. **String -> array** (since 0.28.0). The Keboola runtime schema
       validator requires ``script`` to be an array. The Storage API
       silently accepts a string and the runtime crashes at job
       execution with::

            Invalid type for path "root.parameters.blocks.0.codes.X.script".
            Expected "array", but got "string"

       For SQL transformations the string is split on statement
       boundaries via :func:`split_statements` (state machine respecting
       comments and string literals); for Python / R / custom-Python
       apps and other components with the same schema, the string is
       wrapped as a single-element array.

    2. **Per-element re-split for SQL** (since 0.30.8, closes issue
       #274). When ``script`` is already a list but an element packs
       multiple ``;``-separated statements, the Snowflake/BigQuery/etc.
       runtime crashes at ``odbc_prepare`` with ``Actual statement
       count N did not match the desired statement count 1`` (SQL
       state ``0A000``). Storage API accepts this shape silently
       because it only checks "list of strings". This pass re-splits
       each SQL-transformation element through :func:`split_statements`
       and replaces in place when the element produces more than one
       statement.

    Components that don't match either case are passed through
    unchanged.

    Args:
        component_id: The configuration's component ID. Used to choose
            split-vs-wrap-vs-resplit and to passthrough components that
            don't have this schema at all.
        config: The configuration dict to normalize. Mutated in place.

    Returns:
        ``(config, normalizations)``. ``normalizations`` is a list of
        per-element change records of shape::

            # case 1 (string -> array):
            {
                "path": "parameters.blocks[0].codes[1].script",
                "action": "sql_split" | "wrap_array",
                "before_type": "str",
                "after_type": "list",
                "after_length": 3,
            }
            # case 2 (list element -> multiple list elements):
            {
                "path": "parameters.blocks[0].codes[1].script[2]",
                "action": "sql_resplit",
                "before_type": "str",
                "after_type": "list",
                "before_length": 1,
                "after_length": 2,
            }

        Empty when nothing was normalized (already-valid input).

    The caller is responsible for surfacing the normalization records to
    the user (stderr in human mode, JSON envelope in JSON mode) so the
    silent fix is observable.
    """
    normalizations: list[dict[str, Any]] = []
    if not isinstance(config, dict):
        return config, normalizations
    parameters = config.get("parameters")
    if not isinstance(parameters, dict):
        return config, normalizations
    blocks = parameters.get("blocks")
    if not isinstance(blocks, list):
        return config, normalizations

    is_sql = is_sql_transformation_component(component_id)

    for block_idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_d = cast("dict[str, Any]", block)
        codes = block_d.get("codes")
        if not isinstance(codes, list):
            continue
        for code_idx, code in enumerate(codes):
            if not isinstance(code, dict):
                continue
            code_d = cast("dict[str, Any]", code)
            script = code_d.get("script")
            if isinstance(script, str):
                if is_sql:
                    new_script = split_statements(script)
                    action = "sql_split"
                else:
                    # Single-element wrap for Python / R / custom-Python
                    # apps and any unknown component sharing the schema.
                    # Empty strings collapse to ``[]`` -- the runtime
                    # treats both as no-op.
                    new_script = [script] if script.strip() else []
                    action = "wrap_array"
                code_d["script"] = new_script
                normalizations.append(
                    {
                        "path": f"parameters.blocks[{block_idx}].codes[{code_idx}].script",
                        "action": action,
                        "before_type": "str",
                        "after_type": "list",
                        "after_length": len(new_script),
                    }
                )
            elif is_sql and isinstance(script, list):
                rebuilt: list[Any] = []
                resplits: list[tuple[int, int]] = []
                for el_idx, element in enumerate(script):
                    if isinstance(element, str):
                        parts = split_statements(element)
                        if len(parts) > 1:
                            rebuilt.extend(parts)
                            resplits.append((el_idx, len(parts)))
                            continue
                    rebuilt.append(element)
                if resplits:
                    code_d["script"] = rebuilt
                    for el_idx, after_count in resplits:
                        normalizations.append(
                            {
                                "path": (
                                    f"parameters.blocks[{block_idx}]"
                                    f".codes[{code_idx}].script[{el_idx}]"
                                ),
                                "action": "sql_resplit",
                                "before_type": "str",
                                "after_type": "list",
                                "before_length": 1,
                                "after_length": after_count,
                            }
                        )

    return config, normalizations


@dataclass(frozen=True)
class Companion:
    """One companion file of a component's configs and the payload field it carries.

    Attributes:
        filename: The file next to ``_config.yml``.
        payload_field: The dotted config field the file holds (for readers).
        render: Pull side: pops the value from the local config dict and
            returns the file content, or ``None`` when pull writes no file.
        merge: Push side: puts the file's content back into the local config
            dict. Called only when the file exists, and the file always wins
            over the same value inline -- an empty file is an empty value.
    """

    filename: str
    payload_field: str
    render: Callable[[dict[str, Any]], str | None]
    merge: Callable[[dict[str, Any], Path], None]


def owned_companions(component_id: str) -> tuple[str, ...]:
    """The companion files *component_id*'s configs own (the D1 registry).

    One answer for extraction, merge, the manifest baseline and the pull
    cleanup: a companion file is part of a config's representation only when
    its component extracts it. Any other file in the directory -- a
    ``pyproject.toml`` a user keeps beside an extractor, a ``code.py`` next to
    a SQL transformation -- is never hashed, compared or deleted by sync.
    Dispatch is by exact component id.
    """
    return tuple(companion.filename for companion in companions_of(component_id))


def companions_of(component_id: str) -> tuple[Companion, ...]:
    """The registry entries of *component_id*, in extraction order."""
    if component_id in SQL_TRANSFORMATION_COMPONENTS:
        return (_DESCRIPTION, _SQL_CODE)
    if component_id in PYTHON_TRANSFORMATION_COMPONENTS:
        return (_DESCRIPTION, _PYTHON_CODE, _PYTHON_PACKAGES)
    if component_id in PYTHON_APP_COMPONENTS:
        return (_DESCRIPTION, _APP_CODE, _APP_PACKAGES)
    return (_DESCRIPTION,)


def _parameters(config_data: dict[str, Any]) -> dict[str, Any] | None:
    """The ``parameters`` dict a render reads, or ``None`` when there is none."""
    parameters = config_data.get("parameters")
    return parameters if isinstance(parameters, dict) else None


def _render_description(config_data: dict[str, Any]) -> str | None:
    """A non-empty description goes to ``_description.md``.

    An empty description stays inline as ``description: ''`` -- the layout a
    fresh pull writes and push writeback restores (L2).
    """
    description = config_data.get("description", "")
    if not description:
        return None
    del config_data["description"]
    return description


def _merge_description(config_data: dict[str, Any], path: Path) -> None:
    config_data["description"] = path.read_text(encoding="utf-8")


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
    for name, content in render_code_files(component_id, config_data).items():
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / name).write_text(content, encoding="utf-8")
    return config_data


def render_code_files(component_id: str, config_data: dict[str, Any]) -> dict[str, str]:
    """The companion files a pull writes for *config_data*, by file name.

    Removes the extracted values from *config_data* in place, exactly like
    :func:`extract_code_files`, but writes nothing: the keys are the complete
    desired companion set of the config, which is what lets pull remove a
    companion the remote no longer has (a cleared description or package
    list) instead of leaving it to be pushed back.
    """
    files: dict[str, str] = {}
    for companion in companions_of(component_id):
        content = companion.render(config_data)
        if content is not None:
            files[companion.filename] = content
    return files


def merge_code_files(
    component_id: str,
    config_data: dict[str, Any],
    config_dir: Path,
) -> dict[str, Any]:
    """Read code files and merge them back into config_data.

    Reverse of extract_code_files. Called before push. A present companion
    always wins over the same value inline; a missing one leaves the inline
    value (or none). With neither the file nor a ``description`` key the
    description is empty, which is what push sends and what pull writes --
    spelling it out keeps an untracked file whose ``_description.md`` was
    deleted equal to its remote (#792 E adopt guard).
    Returns the modified config_data.
    """
    for companion in companions_of(component_id):
        path = config_dir / companion.filename
        if path.exists():
            companion.merge(config_data, path)
    config_data.setdefault("description", "")
    return config_data


# ---- SQL Transformations ----


def _render_sql_script_lines(scripts: list[Any], *, with_markers: bool) -> list[str]:
    """Render one code's ``script[]`` as ``transform.sql`` lines."""
    lines: list[str] = []
    for si, script in enumerate(scripts):
        if si > 0:
            lines.append("")  # blank line between statements
            if with_markers:
                lines.append(SQL_STATEMENT_MARKER)
                lines.append("")
        if isinstance(script, str) and "\n" in script:
            lines.extend(script.split("\n"))
        else:
            lines.append(script)
    return lines


def marker_less_roundtrip(script: list[Any]) -> list[str]:
    """What a ``transform.sql`` written WITHOUT markers merges back into.

    The pre-#686 rendering, kept as a predicate: a working tree pulled before
    boundary markers existed holds exactly this array, so comparing against it
    identifies a config whose only difference from the remote is the lost
    statement boundaries (see ``_sync_baseline.raise_on_legacy_boundary``).
    """
    return _lines_to_script(_render_sql_script_lines(script, with_markers=False), is_sql=True)


def _render_sql_code(scripts: list[Any], code_name: str) -> list[str]:
    """Render one code block, adding statement markers only when needed.

    Markers are emitted only when the plain rendering cannot be parsed back
    into the canonical statement array (:func:`canonical_sql_script`) -- i.e.
    when the elements carry no trailing semicolons. The ``;``-terminated case
    (the overwhelming majority) renders exactly as before, so existing trees
    are byte-stable and produce no spurious diff.

    Collision guard: if a statement's own text already contains a
    marker-identical line, emitting boundaries would make the file ambiguous.
    Markers are then suppressed for that code and a warning is logged -- the
    round-trip degrades to the pre-#686 semicolon-only behaviour for it.
    """
    plain = _render_sql_script_lines(scripts, with_markers=False)
    canonical = canonical_sql_script(scripts)
    if _lines_to_script(plain, is_sql=True) == canonical:
        return plain
    if any(
        isinstance(s, str) and any(line.strip() == SQL_STATEMENT_MARKER for line in s.split("\n"))
        for s in scripts
    ):
        logger.warning(
            "Code %r contains a line identical to the statement-boundary marker; "
            "writing transform.sql without boundary markers. Statements without a "
            "trailing semicolon may be merged on the next push.",
            code_name,
        )
        return plain
    return _render_sql_script_lines(scripts, with_markers=True)


def _render_sql_transformation(config_data: dict[str, Any]) -> str | None:
    """Move SQL blocks from parameters.blocks into transform.sql."""
    parameters = _parameters(config_data)
    blocks = parameters.get("blocks", []) if parameters is not None else []
    if parameters is None or not blocks:
        return None

    lines: list[str] = []
    for block in blocks:
        block_name = block.get("name", "unnamed")
        lines.append(SQL_BLOCK_MARKER.format(name=block_name))
        lines.append("")

        for code in block.get("codes", []):
            code_name = code.get("name", "unnamed")
            lines.append(SQL_CODE_MARKER.format(name=code_name))
            lines.extend(_render_sql_code(code.get("script") or [], code_name))
            lines.append("")

    # Remove blocks from parameters (they're now in the SQL file)
    parameters.pop("blocks", None)
    return "\n".join(lines).rstrip() + "\n"


def _merge_sql_transformation(config_data: dict[str, Any], path: Path) -> None:
    """Parse transform.sql's block markers back into parameters.blocks."""
    blocks = _parse_sql_blocks(path.read_text(encoding="utf-8"))
    config_data.setdefault("parameters", {})["blocks"] = blocks


def _parse_sql_blocks(content: str) -> list[dict[str, Any]]:
    """Parse SQL content with block/code markers into blocks structure."""
    blocks: list[dict[str, Any]] = []
    current_block: dict[str, Any] | None = None
    current_code: dict[str, Any] | None = None
    current_script_lines: list[str] = []
    orphan_lines: list[str] = []

    for line in content.split("\n"):
        stripped = line.strip()

        # Check for block marker
        if stripped.startswith("/* ===== BLOCK:") and stripped.endswith("===== */"):
            # Save previous code if any
            if current_code is not None and current_block is not None:
                current_code["script"] = _lines_to_script(current_script_lines, is_sql=True)
                current_block.setdefault("codes", []).append(current_code)
                current_code = None
                current_script_lines = []

            block_name = stripped[len("/* ===== BLOCK:") :].rstrip(" =*/").strip()
            current_block = {"name": block_name, "codes": []}
            blocks.append(current_block)
            orphan_lines = []
            continue

        # Check for code marker
        if stripped.startswith("/* ===== CODE:") and stripped.endswith("===== */"):
            # Save previous code if any
            if current_code is not None and current_block is not None:
                current_code["script"] = _lines_to_script(current_script_lines, is_sql=True)
                current_block.setdefault("codes", []).append(current_code)
                current_script_lines = []

            code_name = stripped[len("/* ===== CODE:") :].rstrip(" =*/").strip()
            current_code = {"name": code_name}
            # Prepend any orphan lines collected between BLOCK and this CODE marker
            if orphan_lines:
                current_script_lines = list(orphan_lines)
                orphan_lines = []
            continue

        # Regular line
        if current_code is not None:
            current_script_lines.append(line)
        elif current_block is not None and stripped:
            # Lines between BLOCK marker and first CODE marker — preserve them
            # so they're not silently discarded on roundtrip.
            orphan_lines.append(line)

    # Don't forget the last code block
    if current_code is not None and current_block is not None:
        current_code["script"] = _lines_to_script(current_script_lines, is_sql=True)
        current_block.setdefault("codes", []).append(current_code)

    # If no markers found, treat entire content as single block/code
    if not blocks and content.strip():
        blocks = [
            {
                "name": "Block 1",
                "codes": [
                    {"name": "Code 1", "script": _lines_to_script(content.split("\n"), is_sql=True)}
                ],
            }
        ]

    return blocks


# ---- Python Transformations ----


def _render_python_blocks(config_data: dict[str, Any]) -> str | None:
    """Move Python blocks into transform.py."""
    parameters = _parameters(config_data)
    blocks = parameters.get("blocks", []) if parameters is not None else []
    if parameters is None or not blocks:
        return None
    lines: list[str] = []
    for block in blocks:
        block_name = block.get("name", "unnamed")
        lines.append(PYTHON_BLOCK_MARKER.format(name=block_name))
        lines.append("")

        for code in block.get("codes", []):
            code_name = code.get("name", "unnamed")
            lines.append(PYTHON_CODE_MARKER.format(name=code_name))

            scripts = code.get("script") or []
            for si, script in enumerate(scripts):
                if si > 0:
                    lines.append("")  # blank line between script elements
                if isinstance(script, str) and "\n" in script:
                    lines.extend(script.split("\n"))
                else:
                    lines.append(script)
            lines.append("")

    parameters.pop("blocks", None)
    return "\n".join(lines).rstrip() + "\n"


def _merge_python_blocks(config_data: dict[str, Any], path: Path) -> None:
    blocks = _parse_python_blocks(path.read_text(encoding="utf-8"))
    config_data.setdefault("parameters", {})["blocks"] = blocks


def _render_python_packages(config_data: dict[str, Any]) -> str | None:
    """Move a transformation's packages into pyproject.toml."""
    parameters = _parameters(config_data)
    packages = parameters.get("packages", []) if parameters is not None else []
    if parameters is None or not packages:
        return None
    parameters.pop("packages", None)
    return _render_pyproject_toml(config_data.get("name", "transformation"), packages)


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


def _render_app_code(config_data: dict[str, Any]) -> str | None:
    """Move parameters.code into code.py."""
    parameters = _parameters(config_data)
    code = parameters.get("code") if parameters is not None else None
    if parameters is None or not code or not isinstance(code, str):
        return None
    parameters.pop("code", None)
    return code


def _merge_app_code(config_data: dict[str, Any], path: Path) -> None:
    config_data.setdefault("parameters", {})["code"] = path.read_text(encoding="utf-8")


def _render_app_packages(config_data: dict[str, Any]) -> str | None:
    """Move an app's packages into pyproject.toml, with its ``[tool.keboola]`` ids."""
    parameters = _parameters(config_data)
    packages = parameters.get("packages", []) if parameters is not None else []
    if parameters is None or not packages:
        return None
    keboola_meta = config_data.get("_keboola", {})
    parameters.pop("packages", None)
    return _render_pyproject_toml(
        config_data.get("name", "app"),
        packages,
        component_id=keboola_meta.get("component_id"),
        config_id=keboola_meta.get("config_id"),
    )


def _merge_packages(config_data: dict[str, Any], path: Path) -> None:
    """pyproject.toml's ``[project].dependencies``, when it states them, ARE the packages.

    Parsed as TOML (a one-line ``dependencies = [...]`` included). Only a
    dependency list the file actually states replaces the package list --
    an explicit ``dependencies = []`` is an empty one. A file without the key,
    or one that is not valid TOML, says nothing: the inline value (or none)
    applies, so such a file can never clear the remote's packages. For a
    file not valid TOML that an older kbagent wrote (a package spec with a
    quote broke its output), the old line reader's non-empty answer is
    used, as that version did; ``sync diff`` reports such a file
    (``COMPANION_UNREADABLE``).
    """
    text = path.read_text(encoding="utf-8")
    declared = declared_dependencies(text)
    if isinstance(declared, list):
        config_data.setdefault("parameters", {})["packages"] = declared
        return
    if declared is _UNREADABLE and not _has_inline_packages(config_data):
        legacy = _parse_pyproject_packages(text)
        if legacy:
            config_data.setdefault("parameters", {})["packages"] = legacy


_UNREADABLE = object()


def declared_dependencies(text: str) -> list[str] | object | None:
    """``[project].dependencies`` of a pyproject.toml text.

    A list of strings when the file states one; ``None`` when it states none
    (no ``[project]`` or no ``dependencies``); :data:`_UNREADABLE` when the
    text is not valid TOML or the value is not a list of strings.
    """
    try:
        project = tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError:
        return _UNREADABLE
    if not isinstance(project, dict) or "dependencies" not in project:
        return None
    dependencies = project["dependencies"]
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        return _UNREADABLE
    return list(dependencies)


def pyproject_unreadable(text: str) -> bool:
    """True iff a pyproject.toml text cannot say which packages it lists."""
    return declared_dependencies(text) is _UNREADABLE


def _has_inline_packages(config_data: dict[str, Any]) -> bool:
    parameters = config_data.get("parameters")
    return isinstance(parameters, dict) and "packages" in parameters


# ---- pyproject.toml helpers ----


def _render_pyproject_toml(
    name: str,
    packages: list[str],
    component_id: str | None = None,
    config_id: str | None = None,
) -> str:
    """Render packages as a pyproject.toml file."""
    # Sanitize name for pyproject
    safe_name = name.lower().replace(" ", "-").replace("_", "-")

    # Every value as a TOML basic string (JSON escaping is valid there): a
    # package spec with a quote, e.g. a marker ``python_version < "3.12"``,
    # used to make the file invalid TOML. Plain values render as before.
    lines = [
        "[project]",
        f"name = {_toml_string(safe_name)}",
        'requires-python = ">=3.11"',
        "dependencies = [",
    ]
    for pkg in packages:
        lines.append(f"    {_toml_string(str(pkg))},")
    lines.append("]")

    if component_id or config_id:
        lines.append("")
        lines.append("[tool.keboola]")
        if component_id:
            lines.append(f"component_id = {_toml_string(str(component_id))}")
        if config_id:
            lines.append(f"config_id = {_toml_string(str(config_id))}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse_pyproject_packages(content: str) -> list[str]:
    """The pre-TOML line reader of a pyproject.toml's dependencies (fallback only)."""
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


# The registry (D1): every companion file and the payload field it carries.
# Extraction (``render_code_files``), merge (``merge_code_files``), the
# manifest baseline and the pull cleanup all read it through
# :func:`companions_of`; adding a companion here is the whole change.
_DESCRIPTION = Companion(
    DESCRIPTION_FILENAME, "description", _render_description, _merge_description
)
_SQL_CODE = Companion(
    "transform.sql", "parameters.blocks", _render_sql_transformation, _merge_sql_transformation
)
_PYTHON_CODE = Companion(
    "transform.py", "parameters.blocks", _render_python_blocks, _merge_python_blocks
)
_PYTHON_PACKAGES = Companion(
    PYPROJECT_FILENAME, "parameters.packages", _render_python_packages, _merge_packages
)
_APP_CODE = Companion("code.py", "parameters.code", _render_app_code, _merge_app_code)
_APP_PACKAGES = Companion(
    PYPROJECT_FILENAME, "parameters.packages", _render_app_packages, _merge_packages
)
