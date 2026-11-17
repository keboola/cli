"""Find SQL statements a transformation runtime silently skips.

``keboola/snowflake-transformation`` (``SnowflakeTransformation::executeQueries``)
and ``keboola/google-bigquery-transformation`` run each ``script[]`` element
like this::

    $uncommentedQuery = SqlFormatter::removeComments($query, false);
    if (strlen(trim($uncommentedQuery)) === 0) continue;               // empty
    if (strtoupper(substr($uncommentedQuery, 0, 6)) === 'SELECT') continue;

``removeComments(..., false)`` drops the comment tokens only: no trim, no
formatting. So a statement whose comment-free text starts with ``SELECT`` at
its very first character never runs -- the job succeeds even when the table
does not exist. Leading whitespace keeps the statement (``substr`` then sees
the whitespace), and so does a leading line comment: its token ends before
the newline, which stays. ``WITH ... SELECT`` runs.

A SELECT has no effect inside a transformation anyway (nothing reads its
result), so kbagent warns about it and never blocks the write.

Comment rules mirror SqlFormatter's tokenizer: ``--`` and ``#`` start a line
comment that ends before the newline, ``/* ... */`` is a block comment (an
unclosed one runs to the end of the text). Only the comments in front of the
first real token matter for the 6-character check, so only those are
removed. A quoted string or any other character ends the scan. A comment in
the middle of the first word (``SEL/**/ECT``) is not handled.
"""

from __future__ import annotations

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
    """Remove the comments in front of the first token, like SqlFormatter does.

    Whitespace between or before comments is kept: the runtime keeps it too,
    and it is what makes a statement run.
    """
    text = statement
    while True:
        if text.startswith(("--", "#")):
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline:]
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            text = "" if end == -1 else text[end + 2 :]
        else:
            return text


def is_skipped_by_runtime(statement: str) -> bool:
    """Whether the runtime skips *statement* as a SELECT."""
    return strip_leading_comments(statement)[:6].upper() == _SKIPPED_PREFIX


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
                if isinstance(statement, str) and is_skipped_by_runtime(statement):
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
