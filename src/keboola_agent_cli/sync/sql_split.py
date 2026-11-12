"""Split and join SQL statements using a state machine.

Splits on semicolons while respecting:
- Single-quoted strings ('...')
- Double-quoted identifiers ("...")
- Dollar-quoted blocks ($$...$$)
- Line comments (--, #, //)
- Block comments (/* ... */)

A procedural block (``BEGIN ... END``, ``IF ... THEN ... END IF``,
``LOOP``, ``WHILE ... DO``, ``FOR ... DO``, ``REPEAT ... UNTIL``, a ``CASE``
statement) stays one statement: its inner semicolons do not split it
(issue #954 -- BigQuery and Snowflake Scripting run such a block as one
statement, and its fragments are invalid SQL). A block that never closes is
split on every semicolon, as before. Otherwise compatible with the Keboola UI
splitter and the old keboola-as-code CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto


class _State(Enum):
    NORMAL = auto()
    SINGLE_QUOTE = auto()
    DOUBLE_QUOTE = auto()
    DOLLAR_QUOTE = auto()
    LINE_COMMENT = auto()
    BLOCK_COMMENT = auto()


@dataclass(frozen=True)
class _Piece:
    """Text up to and including one top-level ``;``, and the same text with strings and comments blanked."""

    text: str
    code: str


def split_statements(sql: str) -> list[str]:
    """Split SQL text into individual statements on semicolons.

    Returns a list of stripped, non-empty statements.  Trailing
    semicolons are preserved on each statement.  A procedural block is
    one statement (see the module docstring).
    """
    sql = sql.rstrip()
    if not sql:
        return []
    statements: list[str] = []
    for text in _join_blocks(_scan(sql)):
        stmt = text.strip()
        if stmt.strip(";"):
            statements.append(stmt)
    return statements


def _scan(sql: str) -> list[_Piece]:
    """Cut ``sql`` after every semicolon outside strings and comments."""
    state = _State.NORMAL
    pieces: list[_Piece] = []
    buf: list[str] = []
    code: list[str] = []
    i = 0
    n = len(sql)

    def emit(text: str, *, is_code: bool) -> None:
        buf.append(text)
        code.append(text if is_code else " " * len(text))

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        two = ch + nxt

        if state == _State.NORMAL:
            if ch == ";":
                emit(ch, is_code=True)
                pieces.append(_Piece("".join(buf), "".join(code)))
                buf, code = [], []
            elif ch == "'":
                emit(ch, is_code=False)
                state = _State.SINGLE_QUOTE
            elif ch == '"':
                emit(ch, is_code=False)
                state = _State.DOUBLE_QUOTE
            elif two == "$$":
                emit(two, is_code=False)
                state = _State.DOLLAR_QUOTE
                i += 1
            elif two in ("--", "//"):
                emit(two, is_code=False)
                state = _State.LINE_COMMENT
                i += 1
            elif ch == "#":
                emit(ch, is_code=False)
                state = _State.LINE_COMMENT
            elif two == "/*":
                emit(two, is_code=False)
                state = _State.BLOCK_COMMENT
                i += 1
            else:
                emit(ch, is_code=True)

        elif state in (_State.SINGLE_QUOTE, _State.DOUBLE_QUOTE):
            quote = "'" if state == _State.SINGLE_QUOTE else '"'
            if ch == "\\" and nxt:
                emit(two, is_code=False)
                i += 1
            else:
                emit(ch, is_code=False)
                if ch == quote:
                    state = _State.NORMAL

        elif state == _State.DOLLAR_QUOTE:
            if two == "$$":
                emit(two, is_code=False)
                state = _State.NORMAL
                i += 1
            else:
                emit(ch, is_code=False)

        elif state == _State.LINE_COMMENT:
            emit(ch, is_code=ch == "\n")
            if ch == "\n":
                state = _State.NORMAL

        elif state == _State.BLOCK_COMMENT:
            if two == "*/":
                emit(two, is_code=False)
                state = _State.NORMAL
                i += 1
            else:
                emit(ch, is_code=False)

        i += 1

    if buf:
        pieces.append(_Piece("".join(buf), "".join(code)))
    return pieces


def _join_blocks(pieces: list[_Piece]) -> list[str]:
    """Join the pieces of each procedural block into one text.

    A block still open at the end keeps its pieces separate: a false
    opener must not swallow the rest of the script. A real block after
    such an opener is then split too, as before this pass existed.
    """
    texts: list[str] = []
    open_pieces: list[str] = []
    depth = 0
    for piece in pieces:
        delta = _block_delta(piece.code)
        if depth == 0 and delta <= 0:
            texts.append(piece.text)
            continue
        depth = max(0, depth + delta)
        open_pieces.append(piece.text)
        if depth == 0:
            texts.append("".join(open_pieces))
            open_pieces = []
    texts.extend(open_pieces)
    return texts


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*|::|[():]")
# The word after BEGIN that makes it a transaction statement, not a block.
_BEGIN_STATEMENTS = {
    "TRANSACTION",
    "TRAN",
    "WORK",
    "DISTRIBUTED",
    "TRY",
    "CATCH",
    "READ",
    "ISOLATION",
    "DEFERRED",
    "IMMEDIATE",
    "EXCLUSIVE",
    "NAME",
}
# CREATE ... <kind> ... BEGIN opens the body of a stored routine.
_ROUTINE_KINDS = {"PROCEDURE", "FUNCTION", "TRIGGER"}
# The kind word after END ("END IF", "END LOOP", ...).
_BLOCK_KINDS = {"IF", "LOOP", "WHILE", "REPEAT", "FOR", "CASE"}
# A statement word between IF/WHILE/FOR and THEN/DO: the THEN/DO belongs to
# something else (T-SQL "IF x SELECT CASE WHEN a THEN b END" has no END IF).
_STATEMENT_WORDS = {
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "CREATE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "EXEC",
    "EXECUTE",
    "SET",
    "PRINT",
    "DECLARE",
    "RETURN",
    "BEGIN",
    "WITH",
    "CALL",
    "RAISE",
    "GOTO",
}


def _top_level_words(code: str) -> list[str]:
    """Upper-cased words outside parentheses; a label ``name:`` becomes ``:``."""
    words: list[str] = []
    depth = 0
    for token in _TOKEN.findall(code):
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and token == ":" and words and words[-1] != ":":
            words[-1] = ":"
        elif depth == 0 and token not in (":", "::"):
            words.append(token.upper())
    return words


def _block_delta(code: str) -> int:
    """How many blocks one piece opens (positive) or closes (-1).

    Only words at a statement start count: the first word, and the word after
    ``BEGIN``, ``THEN``, ``DO``, ``ELSE``, ``LOOP``, ``REPEAT`` or a label.
    ``CREATE ... PROCEDURE|FUNCTION|TRIGGER ... BEGIN`` opens the routine body.
    """
    words = _top_level_words(code)
    delta = 0
    i = 0
    while i < len(words):
        word = words[i]
        if word == ":":
            i += 1
        elif word == "END":
            return delta - 1
        elif word == "BEGIN":
            following = words[i + 1] if i + 1 < len(words) else None
            if following is None or following in _BEGIN_STATEMENTS:
                return delta
            delta += 1
            i += 1
        elif word in ("LOOP", "REPEAT"):
            delta += 1
            i += 1
        elif word in ("ELSE", "EXCEPTION"):
            i += 1
        elif word in ("IF", "ELSEIF", "ELSIF", "WHEN", "WHILE", "FOR", "CASE"):
            # Snowflake Scripting takes LOOP where BigQuery takes DO.
            body = _body_start(words, i, {"DO", "LOOP"} if word in ("WHILE", "FOR") else {"THEN"})
            if body is None:
                return delta
            if word in ("IF", "WHILE", "FOR", "CASE"):
                delta += 1
            i = body
        elif word == "CREATE":
            body = _routine_body_start(words, i)
            if body is None:
                return delta
            delta += 1
            i = body
        elif word == "UNTIL":
            ends = [j for j in range(i, len(words) - 1) if words[j] == "END"]
            return delta - 1 if any(words[j + 1] == "REPEAT" for j in ends) else delta
        else:
            return delta
    return delta


def _routine_body_start(words: list[str], create: int) -> int | None:
    """Index after the ``BEGIN`` of ``CREATE ... PROCEDURE|FUNCTION|TRIGGER ... BEGIN``."""
    kinds = [j for j in range(create + 1, len(words)) if words[j] in _ROUTINE_KINDS]
    if not kinds:
        return None
    begins = [j for j in range(kinds[0] + 1, len(words)) if words[j] == "BEGIN"]
    return begins[0] + 1 if begins else None


def _body_start(words: list[str], opener: int, keywords: set[str]) -> int | None:
    """Index after the ``THEN``/``DO``/``LOOP`` of the opener at ``opener``; None when it has none."""
    for j in range(opener + 1, len(words)):
        if words[j] in keywords:
            return j + 1
        if words[j] in _STATEMENT_WORDS or words[j] == "END":
            return None
    return None


def join_statements(statements: list[str]) -> str:
    """Join SQL statements with double newlines (matching Keboola convention)."""
    if not statements:
        return ""
    return "\n\n".join(s.rstrip() for s in statements)
