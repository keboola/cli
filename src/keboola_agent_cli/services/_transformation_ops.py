"""Positional block/code update engine for SQL transformations.

Faithful port of keboola-mcp-server ``tools/components/tf_update.py`` +
``tools/components/model.py`` (Tf* operation models) for issue #396, with
two deliberate implementation differences:

- No ``jsonpath-ng`` dependency: elements are located by a direct index
  walk over the parameters dict (IDs are synthetic and positional, so a
  linear scan over ``blocks[]`` / ``codes[]`` is exact).
- Multi-value returns use dataclasses (:class:`OpResult`,
  :class:`BatchResult`) instead of bare tuples, per CONTRIBUTING.md.

The engine operates on the *simplified* parameters shape::

    {"blocks": [{"id": "b0", "name": ..., "codes": [
        {"id": "b0.c0", "name": ..., "script": "<SQL text>"}]}]}

where ``script`` is a single SQL text string (statements joined). IDs are
assigned by :func:`add_ids` -- blocks numbered ``b{i}`` from 0, codes
``b{i}.c{j}`` within each block -- and re-derived after every batch, so
they always reflect current positions. Within one batch, operations apply
sequentially against the mutating structure and IDs keep referring to the
structure as it was at batch start (elements added mid-batch carry no ID
until the batch finishes -- same semantics as the MCP server).

Conversion between this simplified shape and the raw Storage API shape
(``script`` as a list of statements) is provided by
:func:`raw_to_simplified` / :func:`simplified_to_raw`, built on the
existing SQL statement splitter in :mod:`keboola_agent_cli.sync.sql_split`.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from ..sync.sql_split import join_statements, split_statements

# Operations that change the structure of the transformation (trigger ID
# re-derivation and a structure summary). Mirrors tf_update.STRUCTURAL_OPS.
STRUCTURAL_OPS: frozenset[str] = frozenset({"add_block", "add_code", "remove_block", "remove_code"})

TfPosition = Literal["start", "end"]


class TfCode(BaseModel, frozen=True):
    """A code entry inside a transformation block (simplified shape)."""

    name: str = Field(description="A descriptive name for the code block")
    script: str = Field(description="The SQL script of the code block")


class TfBlock(BaseModel, frozen=True):
    """A transformation block (simplified shape)."""

    name: str = Field(description="A descriptive name for the block")
    codes: list[TfCode] = Field(default_factory=list, description="SQL code sub-blocks")


class TfAddBlock(BaseModel, frozen=True):
    """Add a new block to the transformation."""

    op: Literal["add_block"]
    block: TfBlock = Field(description="The block to add")
    position: TfPosition = Field(default="end", description="Where to insert the block")


class TfRemoveBlock(BaseModel, frozen=True):
    """Remove an existing block from the transformation."""

    op: Literal["remove_block"]
    block_id: str = Field(description="The ID of the block to remove")


class TfRenameBlock(BaseModel, frozen=True):
    """Rename an existing block in the transformation."""

    op: Literal["rename_block"]
    block_id: str = Field(description="The ID of the block to rename")
    block_name: str = Field(description="The new name of the block")


class TfAddCode(BaseModel, frozen=True):
    """Add a new code to an existing block in the transformation."""

    op: Literal["add_code"]
    block_id: str = Field(description="The ID of the block to add the code to")
    code: TfCode = Field(description="The code to add")
    position: TfPosition = Field(default="end", description="Where to insert the code")


class TfRemoveCode(BaseModel, frozen=True):
    """Remove an existing code from an existing block in the transformation."""

    op: Literal["remove_code"]
    block_id: str = Field(description="The ID of the block to remove the code from")
    code_id: str = Field(description="The ID of the code to remove")


class TfRenameCode(BaseModel, frozen=True):
    """Rename an existing code in an existing block in the transformation."""

    op: Literal["rename_code"]
    block_id: str = Field(description="The ID of the block containing the code")
    code_id: str = Field(description="The ID of the code to rename")
    code_name: str = Field(description="The new name of the code")


class TfSetCode(BaseModel, frozen=True):
    """Set the SQL script of an existing code in an existing block."""

    op: Literal["set_code"]
    block_id: str = Field(description="The ID of the block containing the code")
    code_id: str = Field(description="The ID of the code to set")
    script: str = Field(description="The SQL script of the code to set")


class TfAddScript(BaseModel, frozen=True):
    """Append or prepend SQL script text to an existing code."""

    op: Literal["add_script"]
    block_id: str = Field(description="The ID of the block containing the code")
    code_id: str = Field(description="The ID of the code to add the script to")
    script: str = Field(description="The SQL script to add")
    position: TfPosition = Field(default="end", description="Where to add the script")


class TfStrReplace(BaseModel, frozen=True):
    """Replace a substring in SQL scripts in the transformation."""

    op: Literal["str_replace"]
    block_id: str | None = Field(
        default=None,
        description=(
            "The ID of the block to replace substrings in. If not provided, all blocks are updated."
        ),
    )
    code_id: str | None = Field(
        default=None,
        description=(
            "The ID of the code to replace substrings in. "
            "If not provided, all codes in the block are updated."
        ),
    )
    search_for: str = Field(description="Substring to search for (non-empty)")
    replace_with: str = Field(description="Replacement string (can be empty for deletion)")

    @model_validator(mode="after")
    def validate_code_id_requires_block_id(self) -> TfStrReplace:
        """code_id can only be specified together with block_id."""
        if self.block_id is None and self.code_id is not None:
            raise ValueError("code_id must be None if block_id is None")
        return self


TfOp = Annotated[
    TfAddBlock
    | TfRemoveBlock
    | TfRenameBlock
    | TfAddCode
    | TfRemoveCode
    | TfRenameCode
    | TfSetCode
    | TfAddScript
    | TfStrReplace,
    Field(discriminator="op"),
]

_OPS_ADAPTER: TypeAdapter[list[TfOp]] = TypeAdapter(list[TfOp])


def parse_ops(raw_ops: Sequence[dict[str, Any]]) -> list[TfOp]:
    """Validate raw op dicts into typed operation models.

    Args:
        raw_ops: Sequence of dicts, each with an ``op`` discriminator key
            (``add_block``, ``remove_block``, ``rename_block``, ``add_code``,
            ``remove_code``, ``rename_code``, ``set_code``, ``add_script``,
            ``str_replace``).

    Returns:
        List of validated operation models, in input order.

    Raises:
        ValueError: With a readable summary when any op fails validation.
    """
    try:
        return _OPS_ADAPTER.validate_python(list(raw_ops))
    except ValidationError as exc:
        parts = []
        for err in exc.errors()[:5]:
            loc = ".".join(str(item) for item in err["loc"])
            parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        raise ValueError("Invalid operation(s): " + "; ".join(parts)) from exc


@dataclass
class OpResult:
    """Result of applying a single operation (params mutated in place)."""

    params: dict[str, Any]
    message: str


@dataclass
class BatchResult:
    """Result of applying a batch of operations.

    Attributes:
        params: The updated simplified parameters, with positional IDs
            re-derived so they reflect the final structure.
        messages: Human-readable per-op change summaries, in apply order.
        structural: True when any op in the batch changed the block/code
            structure (add/remove of a block or code).
    """

    params: dict[str, Any]
    messages: list[str]
    structural: bool


def add_ids(parameters: dict[str, Any]) -> dict[str, Any]:
    """Assign synthetic positional IDs to blocks and codes (in place).

    Blocks are numbered sequentially from 0 (``b0``, ``b1``, ...); codes are
    numbered from 0 within each block and prefixed with the block ID
    (``b0.c0``, ``b0.c1``, ...). Mirrors the MCP server's ``add_ids``.
    """
    for bidx, block in enumerate(parameters.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        block["id"] = f"b{bidx}"
        for cidx, code in enumerate(block.get("codes") or []):
            if not isinstance(code, dict):
                continue
            code["id"] = f"b{bidx}.c{cidx}"
    return parameters


def _valid_block_ids(params: dict[str, Any]) -> list[str]:
    return [
        block["id"]
        for block in params.get("blocks") or []
        if isinstance(block, dict) and "id" in block
    ]


def _valid_code_ids(block: dict[str, Any]) -> list[str]:
    return [
        code["id"] for code in block.get("codes") or [] if isinstance(code, dict) and "id" in code
    ]


def _find_block(params: dict[str, Any], block_id: str) -> dict[str, Any]:
    """Locate a block by its synthetic ID or raise with the valid IDs listed."""
    for block in params.get("blocks") or []:
        if isinstance(block, dict) and block.get("id") == block_id:
            return block
    valid = ", ".join(_valid_block_ids(params)) or "(none)"
    raise ValueError(f"Block with id '{block_id}' does not exist. Valid block ids: {valid}")


def _find_code(block: dict[str, Any], block_id: str, code_id: str) -> dict[str, Any]:
    """Locate a code within a block by ID or raise with the valid IDs listed."""
    for code in block.get("codes") or []:
        if isinstance(code, dict) and code.get("id") == code_id:
            return code
    valid = ", ".join(_valid_code_ids(block)) or "(none)"
    raise ValueError(
        f"Code with id '{code_id}' in block '{block_id}' does not exist. Valid code ids: {valid}"
    )


def add_block(params: dict[str, Any], op: TfAddBlock) -> OpResult:
    """Add a new block at the start or end of the transformation."""
    if "blocks" not in params:
        raise ValueError("Invalid parameters: must contain 'blocks' key")
    if not op.block.name.strip():
        raise ValueError("Invalid operation: block name cannot be empty")

    new_block_dict = op.block.model_dump()
    if op.position == "start":
        params["blocks"].insert(0, new_block_dict)
    else:  # "end"
        params["blocks"].append(new_block_dict)

    return OpResult(params, f'Added block with name "{op.block.name}"')


def remove_block(params: dict[str, Any], op: TfRemoveBlock) -> OpResult:
    """Remove an existing block from the transformation."""
    block = _find_block(params, op.block_id)
    params["blocks"].remove(block)
    return OpResult(params, f'Removed block "{op.block_id}"')


def rename_block(params: dict[str, Any], op: TfRenameBlock) -> OpResult:
    """Rename an existing block in the transformation."""
    if not op.block_name.strip():
        raise ValueError("Invalid operation: block name cannot be empty")
    block = _find_block(params, op.block_id)
    block["name"] = op.block_name
    return OpResult(params, f'Renamed block "{op.block_id}" to "{op.block_name}"')


def add_code(params: dict[str, Any], op: TfAddCode) -> OpResult:
    """Add a new code to an existing block."""
    if not op.code.name.strip():
        raise ValueError("Invalid operation: code name cannot be empty")
    block = _find_block(params, op.block_id)
    codes = block.setdefault("codes", [])

    new_code_dict = op.code.model_dump()
    if op.position == "start":
        codes.insert(0, new_code_dict)
    else:  # "end"
        codes.append(new_code_dict)

    return OpResult(params, f'Added code with name "{op.code.name}"')


def remove_code(params: dict[str, Any], op: TfRemoveCode) -> OpResult:
    """Remove an existing code from an existing block."""
    block = _find_block(params, op.block_id)
    code = _find_code(block, op.block_id, op.code_id)
    block["codes"].remove(code)
    return OpResult(params, f'Removed code "{op.code_id}" from block "{op.block_id}"')


def rename_code(params: dict[str, Any], op: TfRenameCode) -> OpResult:
    """Rename an existing code in an existing block."""
    if not op.code_name.strip():
        raise ValueError("Invalid operation: code name cannot be empty")
    block = _find_block(params, op.block_id)
    code = _find_code(block, op.block_id, op.code_id)
    code["name"] = op.code_name
    return OpResult(params, f'Renamed code "{op.code_id}" to "{op.code_name}"')


def set_code(params: dict[str, Any], op: TfSetCode) -> OpResult:
    """Replace the SQL script of an existing code."""
    if not op.script.strip():
        raise ValueError("Invalid operation: script cannot be empty")
    block = _find_block(params, op.block_id)
    code = _find_code(block, op.block_id, op.code_id)
    code["script"] = op.script
    return OpResult(params, f"Changed code with id '{op.code_id}' in block '{op.block_id}'")


def add_script(params: dict[str, Any], op: TfAddScript) -> OpResult:
    """Append or prepend SQL text to an existing code's script.

    Joins with a single space, matching the MCP server's behavior; the
    statement splitter re-segments on push so ``...; SELECT 2;`` still
    lands as separate statements in the raw shape.
    """
    if not op.script.strip():
        raise ValueError("Invalid operation: script cannot be empty")
    block = _find_block(params, op.block_id)
    code = _find_code(block, op.block_id, op.code_id)

    current_script = code.get("script") or ""
    if op.position == "start":
        new_script = f"{op.script} {current_script}" if current_script else op.script
    else:  # "end"
        new_script = f"{current_script} {op.script}" if current_script else op.script
    code["script"] = new_script

    return OpResult(params, f"Added script to code with id '{op.code_id}' in block '{op.block_id}'")


def str_replace(params: dict[str, Any], op: TfStrReplace) -> OpResult:
    """Replace a substring in SQL scripts, scoped by optional block/code ID."""
    if not op.search_for:
        raise ValueError("Invalid operation: search string is empty")
    if op.search_for == op.replace_with:
        raise ValueError(
            f'Invalid operation: search string and replace string are the same: "{op.search_for}"'
        )

    if op.block_id is None:
        codes = [
            code
            for block in params.get("blocks") or []
            if isinstance(block, dict)
            for code in block.get("codes") or []
            if isinstance(code, dict)
        ]
        scope = "the transformation"
    elif op.code_id is None:
        block = _find_block(params, op.block_id)
        codes = [code for code in block.get("codes") or [] if isinstance(code, dict)]
        scope = f'block "{op.block_id}"'
    else:
        block = _find_block(params, op.block_id)
        codes = [_find_code(block, op.block_id, op.code_id)]
        scope = f'code "{op.code_id}", block "{op.block_id}"'

    if not codes:
        raise ValueError(f"No scripts found in {scope}")

    replace_cnt = 0
    for code in codes:
        script = code.get("script")
        if isinstance(script, str) and op.search_for in script:
            replace_cnt += script.count(op.search_for)
            code["script"] = script.replace(op.search_for, op.replace_with)

    if replace_cnt == 0:
        raise ValueError(f'Search string "{op.search_for}" not found in {scope}')

    occurrence_word = "occurrence" if replace_cnt == 1 else "occurrences"
    return OpResult(
        params,
        f'Replaced {replace_cnt} {occurrence_word} of "{op.search_for}" in {scope}',
    )


def _apply_op(params: dict[str, Any], op: Any) -> OpResult:
    """Dispatch a single validated op to its applier function."""
    appliers = {
        "add_block": add_block,
        "remove_block": remove_block,
        "rename_block": rename_block,
        "add_code": add_code,
        "remove_code": remove_code,
        "rename_code": rename_code,
        "set_code": set_code,
        "add_script": add_script,
        "str_replace": str_replace,
    }
    return appliers[op.op](params, op)


def apply_ops(parameters: dict[str, Any], ops: Sequence[Any]) -> BatchResult:
    """Apply a batch of operations to simplified parameters.

    The input dict is not modified (a deep copy is taken). IDs are derived
    at batch start; operations apply sequentially against the mutating
    structure (so an ID keeps pointing at the element it identified at
    batch start, and elements added mid-batch are not addressable until
    the next batch -- MCP-server semantics). After the batch, IDs are
    re-derived so the returned parameters carry positionally-correct IDs.

    Args:
        parameters: Simplified parameters (``script`` as text string).
        ops: Validated operation models (from :func:`parse_ops`).

    Returns:
        BatchResult with the updated parameters, per-op messages, and a
        structural-change flag.

    Raises:
        ValueError: When any op is invalid against the current structure
            (unknown ID, empty name/script, search string not found, ...).
    """
    params = copy.deepcopy(parameters)
    params.setdefault("blocks", [])
    add_ids(params)

    structural = any(op.op in STRUCTURAL_OPS for op in ops)
    messages: list[str] = []
    for op in ops:
        result = _apply_op(params, op)
        params = result.params
        if result.message:
            messages.append(result.message)

    # Re-derive IDs so the output reflects final positions. For
    # non-structural batches this is a no-op (structure unchanged).
    add_ids(params)
    return BatchResult(params=params, messages=messages, structural=structural)


def raw_to_simplified(parameters: dict[str, Any]) -> dict[str, Any]:
    """Convert raw (Storage API) parameters to the simplified shape.

    ``script`` statement arrays are joined into a single SQL text string
    (double-newline separator, Keboola convention). A ``script`` that is
    already a plain string (legacy configs; the Storage API accepts it)
    is kept as-is, so both shapes round-trip.
    """
    blocks_out: list[dict[str, Any]] = []
    for block in parameters.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        codes_out: list[dict[str, Any]] = []
        for code in block.get("codes") or []:
            if not isinstance(code, dict):
                continue
            script = code.get("script")
            if isinstance(script, list):
                text = join_statements([s for s in script if isinstance(s, str)])
            elif isinstance(script, str):
                text = script
            else:
                text = ""
            codes_out.append({"name": code.get("name", ""), "script": text})
        blocks_out.append({"name": block.get("name", ""), "codes": codes_out})
    return {"blocks": blocks_out}


def simplified_to_raw(parameters: dict[str, Any]) -> dict[str, Any]:
    """Convert simplified parameters back to the raw (Storage API) shape.

    Each SQL text string is split into individual statements via the
    state-machine splitter (one statement per ``script[]`` element --
    required by the Keboola runtime). Synthetic ``id`` keys are stripped:
    they are positional view-model artifacts and must never be persisted.
    """
    blocks_out: list[dict[str, Any]] = []
    for block in parameters.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        codes_out: list[dict[str, Any]] = []
        for code in block.get("codes") or []:
            if not isinstance(code, dict):
                continue
            script = code.get("script")
            statements = split_statements(script) if isinstance(script, str) else []
            codes_out.append({"name": code.get("name", ""), "script": statements})
        blocks_out.append({"name": block.get("name", ""), "codes": codes_out})
    return {"blocks": blocks_out}
