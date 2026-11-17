r"""Find SQL statements a transformation runtime silently skips.

``keboola/snowflake-transformation`` and ``keboola/google-bigquery-transformation``
do not run a ``script[]`` element whose comment-free text starts with ``SELECT``:
the job succeeds even when the table does not exist. Both remove the comments
with ``SqlFormatter::removeComments()`` first and then ignore leading whitespace:

- Snowflake (``SnowflakeTransformation::isSkippedSelect``, since
  keboola/snowflake-transformation#42): ``preg_match('/^\s*SELECT\b/i', ...)``.
  ``SELECT`` must be a whole word, so ``SELECTT ...`` runs (and fails).
- BigQuery (``Transformation::executeQueries``): ``removeComments($query)``
  reformats the SQL, which drops leading whitespace, then compares the first 6
  characters with ``SELECT``. No whole-word check, so ``SELECTT ...`` is skipped.

``WITH ... SELECT`` and ``CREATE TABLE ... AS SELECT`` run in both.

A SELECT has no effect inside a transformation anyway (nothing reads its
result), so kbagent warns about it and never blocks the write.

Comment rules mirror SqlFormatter's tokenizer: ``--`` and ``#`` start a line
comment that ends before the newline, ``/* ... */`` is a block comment (an
unclosed one runs to the end of the text). ``//`` is not a comment for
SqlFormatter, so ``// note\nSELECT ...`` runs. Only the comments and the
whitespace in front of the first real token matter, so only those are
removed. A quoted string or any other character ends the scan. A comment in
the middle of the first word (``SEL/**/ECT``) is not handled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# The components whose runtime skips a leading SELECT, verified in their
# source (see the module docstring). Other SQL transformation components
# (Redshift, Synapse, ...) are not checked: their runtime was not verified.
SELECT_SKIPPING_COMPONENTS: frozenset[str] = frozenset(
    {
        "keboola.snowflake-transformation",
        "keboola.google-bigquery-transformation",
    }
)

# The ``code`` of the ``warnings[]`` entry kbagent writes for each finding.
SQL_SELECT_SKIPPED_CODE = "SQL_SELECT_SKIPPED_BY_RUNTIME"

# Characters of the statement quoted in a finding.
EXCERPT_MAX_CHARS = 80

SNOWFLAKE_COMPONENT = "keboola.snowflake-transformation"

# Snowflake requires SELECT as a whole word; BigQuery compares 6 characters.
_SNOWFLAKE_SELECT = re.compile(r"SELECT\b", re.IGNORECASE)
_SKIPPED_PREFIX = "SELECT"


@dataclass(frozen=True)
class SkippedSelect:
    """One ``script[]`` element the runtime skips without running it."""

    block: str
    code: str
    index: int
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape of the finding."""
        return {
            "block": self.block,
            "code": self.code,
            "index": self.index,
            "excerpt": self.excerpt,
        }


def strip_leading_comments(statement: str) -> str:
    """Remove the whitespace and comments in front of the first token.

    Both runtimes ignore them: comments are removed by SqlFormatter, and the
    whitespace that remains does not change the decision.
    """
    text = statement.lstrip()
    while True:
        if text.startswith(("--", "#")):
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline:].lstrip()
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            text = "" if end == -1 else text[end + 2 :].lstrip()
        else:
            return text


def is_skipped_by_runtime(component_id: str, statement: str) -> bool:
    """Whether the runtime of *component_id* skips *statement* as a SELECT."""
    text = strip_leading_comments(statement)
    if component_id == SNOWFLAKE_COMPONENT:
        return _SNOWFLAKE_SELECT.match(text) is not None
    return text[:6].upper() == _SKIPPED_PREFIX


def skipped_select_statements(component_id: str, blocks: Any) -> list[SkippedSelect]:
    """Every statement of ``parameters.blocks`` the runtime skips as a SELECT.

    Args:
        component_id: The configuration's component. Components outside
            :data:`SELECT_SKIPPING_COMPONENTS` return no findings.
        blocks: ``configuration.parameters.blocks`` as sent to the API: a list
            of ``{name, codes: [{name, script: [statement, ...]}]}``. A string
            ``script`` counts as one statement; malformed parts are ignored.

    Returns:
        The findings in block, code, statement order.
    """
    if component_id not in SELECT_SKIPPING_COMPONENTS or not isinstance(blocks, list):
        return []
    found: list[SkippedSelect] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for code in block.get("codes") or []:
            if not isinstance(code, dict):
                continue
            script = code.get("script")
            statements = [script] if isinstance(script, str) else script
            if not isinstance(statements, list):
                continue
            for index, statement in enumerate(statements):
                if isinstance(statement, str) and is_skipped_by_runtime(component_id, statement):
                    found.append(
                        SkippedSelect(
                            block=str(block.get("name", "")),
                            code=str(code.get("name", "")),
                            index=index,
                            excerpt=_excerpt(statement),
                        )
                    )
    return found


def skipped_select_warnings(
    component_id: str, configuration: Any, **identity: str
) -> list[dict[str, Any]]:
    """``warnings[]`` entries for the skipped SELECTs of one configuration.

    Args:
        component_id: The configuration's component.
        configuration: The configuration body (``parameters.blocks`` is read).
        **identity: Fields that name the configuration in the entry
            (``config_id``, ``path``, ...); empty values are left out.

    Returns:
        One ``{"code", "message", "component_id", ..., "statement"}`` entry
        per finding; empty when nothing is skipped.
    """
    parameters = configuration.get("parameters") if isinstance(configuration, dict) else None
    blocks = parameters.get("blocks") if isinstance(parameters, dict) else None
    named = {key: value for key, value in identity.items() if value}
    label = named.get("config_id") or named.get("path") or "the new configuration"
    return [
        {
            "code": SQL_SELECT_SKIPPED_CODE,
            "message": select_skip_message(finding, f"{component_id}/{label}"),
            "component_id": component_id,
            **named,
            "statement": finding.to_dict(),
        }
        for finding in skipped_select_statements(component_id, blocks)
    ]


def select_skip_message(finding: SkippedSelect, where: str) -> str:
    """The human sentence for one finding."""
    return (
        f"{where}: block '{finding.block}', code '{finding.code}', statement "
        f"#{finding.index + 1} starts with SELECT, so the transformation runtime skips "
        f"it without running it (the job succeeds even if the SQL is invalid). A SELECT "
        f"has no effect in a transformation; to test a failure use e.g. "
        f"CREATE TABLE ... AS SELECT. Statement: {finding.excerpt}"
    )


def _excerpt(statement: str) -> str:
    """The statement on one line, cut to :data:`EXCERPT_MAX_CHARS`."""
    flat = " ".join(statement.split())
    if len(flat) <= EXCERPT_MAX_CHARS:
        return flat
    return flat[: EXCERPT_MAX_CHARS - 3] + "..."
