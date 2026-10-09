"""Incremental Server-Sent Events parser."""

from __future__ import annotations

import re
from dataclasses import dataclass

_EOL = re.compile(rb"\r\n|\r|\n")
_CR = 0x0D
_COLON = 0x3A


@dataclass(slots=True)
class SSEEvent:
    event: str | None  # "event:" field, None if absent
    data: str  # data lines joined with "\n"
    raw: bytes  # exact bytes of this event including its terminating blank line


class SSEParser:
    """Incremental SSE parser.

    Handles \\n, \\r\\n and \\r line endings, events split across chunks at any byte,
    comments, multi-line data and a final event without a trailing blank line (via close()).

    Every input byte lands in exactly one returned event's ``raw``, so joining the raws of
    everything feed() and close() return reproduces the input. Blocks without fields
    (comments, stray blank lines) are returned as events with ``event=None`` and ``data=""``.
    """

    __slots__ = ("_buf", "_start", "_scan", "_search", "_event", "_data")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._start = 0  # offset of the current event's first byte
        self._scan = 0  # offset of the first unprocessed line
        self._search = 0  # offset to resume the end-of-line search from
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        if not chunk:
            return []
        self._buf += chunk
        out = self._drain(final=False)
        self._compact()
        return out

    def close(self) -> list[SSEEvent]:
        out = self._drain(final=True)
        buf = self._buf
        end = len(buf)
        if self._scan < end:
            self._field(buf[self._scan : end])
            self._scan = end
        if self._start < end:
            out.append(self._dispatch(end))
        self._buf = bytearray()
        self._start = self._scan = self._search = 0
        return out

    def _drain(self, final: bool) -> list[SSEEvent]:
        buf = self._buf
        n = len(buf)
        pos = self._scan
        search = max(pos, self._search)
        out: list[SSEEvent] = []
        while True:
            m = _EOL.search(buf, search)
            if m is None:
                search = n
                break
            start, end = m.span()
            if end == n and buf[start] == _CR and end - start == 1 and not final:
                search = start  # a lone trailing \r may be the first half of \r\n
                break
            if start == pos:
                out.append(self._dispatch(end))
            else:
                self._field(buf[pos:start])
            pos = search = end
        self._scan = pos
        self._search = search
        return out

    def _field(self, line: bytearray) -> None:
        if line[0] == _COLON:
            return
        i = line.find(b":")
        if i < 0:
            name, value = bytes(line), b""
        else:
            name = bytes(line[:i])
            value = bytes(line[i + 1 :])
            if value[:1] == b" ":
                value = value[1:]
        if name == b"data":
            self._data.append(value.decode("utf-8", "replace"))
        elif name == b"event":
            self._event = value.decode("utf-8", "replace")

    def _dispatch(self, end: int) -> SSEEvent:
        ev = SSEEvent(self._event, "\n".join(self._data), bytes(self._buf[self._start : end]))
        self._event = None
        self._data = []
        self._start = end
        return ev

    def _compact(self) -> None:
        start = self._start
        if start:
            del self._buf[:start]
            self._start = 0
            self._scan -= start
            self._search -= start
