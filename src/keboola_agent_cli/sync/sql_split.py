"""Split and join SQL statements using a state machine.

Splits on semicolons while respecting:
- Single-quoted strings ('...')
- Double-quoted identifiers ("...")
- Dollar-quoted blocks ($$...$$)
- Line comments (--, #, //)
- Block comments (/* ... */)

Compatible with the Keboola UI splitter and the old keboola-as-code CLI.
"""

from __future__ import annotations

from enum import Enum, auto


class _State(Enum):
    NORMAL = auto()
    SINGLE_QUOTE = auto()
    DOUBLE_QUOTE = auto()
    DOLLAR_QUOTE = auto()
    LINE_COMMENT = auto()
    BLOCK_COMMENT = auto()


def split_statements(sql: str) -> list[str]:
    """Split SQL text into individual statements on semicolons.

    Returns a list of stripped, non-empty statements.  Trailing
    semicolons are preserved on each statement.
    """
    sql = sql.rstrip()
    if not sql:
        return []

    state = _State.NORMAL
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        two = ch + nxt

        if state == _State.NORMAL:
            if ch == ";":
                buf.append(ch)
                stmt = "".join(buf).strip()
                if stmt.strip(";"):
                    statements.append(stmt)
                buf = []
            elif ch == "'":
                buf.append(ch)
                state = _State.SINGLE_QUOTE
            elif ch == '"':
                buf.append(ch)
                state = _State.DOUBLE_QUOTE
            elif two == "$$":
                buf.append(two)
                state = _State.DOLLAR_QUOTE
                i += 1
            elif two in ("--", "//"):
                buf.append(two)
                state = _State.LINE_COMMENT
                i += 1
            elif ch == "#":
                buf.append(ch)
                state = _State.LINE_COMMENT
            elif two == "/*":
                buf.append(two)
                state = _State.BLOCK_COMMENT
                i += 1
            else:
                buf.append(ch)

        elif state == _State.SINGLE_QUOTE:
            if ch == "\\" and nxt:
                buf.append(two)
                i += 1
            elif ch == "'":
                buf.append(ch)
                state = _State.NORMAL
            else:
                buf.append(ch)

        elif state == _State.DOUBLE_QUOTE:
            if ch == "\\" and nxt:
                buf.append(two)
                i += 1
            elif ch == '"':
                buf.append(ch)
                state = _State.NORMAL
            else:
                buf.append(ch)

        elif state == _State.DOLLAR_QUOTE:
            if two == "$$":
                buf.append(two)
                state = _State.NORMAL
                i += 1
            else:
                buf.append(ch)

        elif state == _State.LINE_COMMENT:
            buf.append(ch)
            if ch == "\n":
                state = _State.NORMAL

        elif state == _State.BLOCK_COMMENT:
            if two == "*/":
                buf.append(two)
                state = _State.NORMAL
                i += 1
            else:
                buf.append(ch)

        i += 1

    # Remaining content without trailing semicolon
    remaining = "".join(buf).strip()
    if remaining:
        statements.append(remaining)

    return statements


def join_statements(statements: list[str]) -> str:
    """Join SQL statements with double newlines (matching Keboola convention)."""
    if not statements:
        return ""
    return "\n\n".join(s.rstrip() for s in statements)
