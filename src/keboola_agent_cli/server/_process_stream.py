"""Read line-oriented output from subprocess pipes."""

from __future__ import annotations

from asyncio import StreamReader
from collections.abc import AsyncIterator


async def iter_pipe_lines(stream: StreamReader) -> AsyncIterator[bytes]:
    """Yield lines without LF, even when they exceed StreamReader's readline limit."""
    pending = bytearray()
    while chunk := await stream.read(64 * 1024):
        *lines, tail = chunk.split(b"\n")
        for line in lines:
            pending.extend(line)
            yield bytes(pending)
            pending.clear()
        pending.extend(tail)
    if pending:
        yield bytes(pending)
