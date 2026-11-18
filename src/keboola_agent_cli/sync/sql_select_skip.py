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
_SNOWFLAKE_SELECT = re.compile(r"SELECT\b", re.IGNORECASE | re.ASCII)
# PCRE ``\s`` without the /u flag, and PHP's trim(): ASCII whitespace only. A
# non-breaking space in front of SELECT is not whitespace for the runtime.
_ASCII_WHITESPACE = " \t\n\r\f\v"
_SKIPPED_PREFIX = "SELECT"


@dataclass(frozen=True)
class SkippedSelect:
    """One ``script[]`` element the runtime skips without running it."""

    block: str
    code: str
    index: int
    excerpt: str
    # True when the Snowflake runtime before keboola/snowflake-transformation#42
    # (component 1.7.1) ran this statement: a comment, a newline or a space in front
    # of SELECT made it run. A guard written like that (``-- check`` + ``SELECT 1/0``)
    # stopped the job until 1.7.1 and is silently skipped since.
    ran_before_fix: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape of the finding."""
        return {
            "block": self.block,
            "code": self.code,
            "index": self.index,
            "excerpt": self.excerpt,
        }


def _strip_comments_keep_whitespace(statement: str) -> str:
    """Remove the leading comments only, as the runtime before 1.7.1 did."""
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


def ran_before_fix(component_id: str, statement: str) -> bool:
    """Whether a statement skipped today still ran before Snowflake 1.7.1.

    Only Snowflake changed: BigQuery always ignored the leading whitespace.
    """
    if component_id != SNOWFLAKE_COMPONENT:
        return False
    return _strip_comments_keep_whitespace(statement)[:6].upper() != _SKIPPED_PREFIX


def strip_leading_comments(statement: str) -> str:
    """Remove the whitespace and comments in front of the first token.

    Both runtimes ignore them: comments are removed by SqlFormatter, and the
    whitespace that remains does not change the decision.
    """
    text = statement.lstrip(_ASCII_WHITESPACE)
    while True:
        if text.startswith(("--", "#")):
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline:].lstrip(_ASCII_WHITESPACE)
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            text = "" if end == -1 else text[end + 2 :].lstrip(_ASCII_WHITESPACE)
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
                            ran_before_fix=ran_before_fix(component_id, statement),
                        )
                    )
    return found


def skipped_selects_in_configs(component_id: str, configs: Any) -> list[dict[str, Any]]:
    """The skipped SELECTs of every configuration in a component's config listing.

    Args:
        component_id: The component the listing belongs to.
        configs: ``GET .../components/{id}/configs`` as returned by the API
            (each item carries ``configuration``); anything else gives no findings.

    Returns:
        One ``{component_id, config_id, config_name, ran_before_fix, statement}``
        entry per skipped statement.
    """
    if not isinstance(configs, list):
        return []
    found: list[dict[str, Any]] = []
    for config in configs:
        if not isinstance(config, dict):
            continue
        configuration = config.get("configuration")
        parameters = configuration.get("parameters") if isinstance(configuration, dict) else None
        blocks = parameters.get("blocks") if isinstance(parameters, dict) else None
        for finding in skipped_select_statements(component_id, blocks):
            found.append(
                {
                    "component_id": component_id,
                    "config_id": str(config.get("id", "")),
                    "config_name": str(config.get("name", "")),
                    "ran_before_fix": finding.ran_before_fix,
                    "statement": finding.to_dict(),
                }
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
        f"has no effect in a transformation. To stop the job on purpose (a data check), "
        f"use SET ABORT_TRANSFORMATION = (SELECT IFF(<ok>, '', '<message>') ...); to "
        f"keep a result, use CREATE TABLE ... AS SELECT. Statement: {finding.excerpt}"
    )


def _excerpt(statement: str) -> str:
    """The statement on one line, cut to :data:`EXCERPT_MAX_CHARS`."""
    flat = " ".join(statement.split())
    if len(flat) <= EXCERPT_MAX_CHARS:
        return flat
    return flat[: EXCERPT_MAX_CHARS - 3] + "..."
