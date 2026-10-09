"""Plain-text formatting helpers shared by the CLI, reports and the statusline."""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

DASH = "-"
_NUMERIC = re.compile(r"^[-+]?<?\$?(?:[\d,]*\.?\d+[a-zA-Z%]{0,2})+$")


def tokens(n: int | float | None) -> str:
    """950, 57.8k, 250k, 1.2M."""
    if n is None:
        return DASH
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 1000:
        return f"{sign}{int(round(n))}"
    for unit, div in (("k", 1e3), ("M", 1e6), ("B", 1e9)):
        v = n / div
        s = f"{v:.1f}" if v < 99.95 else f"{v:.0f}"
        if float(s) < 1000 or unit == "B":
            return f"{sign}{s.removesuffix('.0')}{unit}"
    raise AssertionError("unreachable")


def money(usd: float | None) -> str:
    """$0 for zero, 4 decimals under $1 ($0.0731), else 2 ($12.40)."""
    if usd is None:
        return DASH
    if usd == 0:
        return "$0"
    sign = "-" if usd < 0 else ""
    v = abs(usd)
    if v < 0.00005:
        return f"{sign}<$0.0001"
    if round(v, 4) < 1:
        return f"{sign}${v:.4f}"
    return f"{sign}${v:,.2f}"


def percent(fraction: float | None, digits: int = 0) -> str:
    """0.423 -> 42%."""
    if fraction is None:
        return DASH
    return f"{fraction * 100:.{digits}f}%"


def duration(seconds: float | None) -> str:
    """350ms, 1.9s, 42s, 7m12s, 3h05m, 2d03h."""
    if seconds is None:
        return DASH
    s = max(0.0, float(seconds))
    if s < 1:
        return f"{round(s * 1000)}ms"
    if s < 10:
        return f"{s:.1f}s"
    total = round(s)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    if total < 86400:
        return f"{total // 3600}h{total % 3600 // 60:02d}m"
    return f"{total // 86400}d{total % 86400 // 3600:02d}h"


def timestamp(ts: float | None, now: float | None = None) -> str:
    """Local time: 'HH:MM' today, 'Mon DD HH:MM' this year, else 'YYYY-MM-DD HH:MM'."""
    if ts is None:
        return DASH
    t = datetime.fromtimestamp(ts)
    n = datetime.fromtimestamp(time.time() if now is None else now)
    if t.date() == n.date():
        return t.strftime("%H:%M")
    if t.year == n.year:
        return t.strftime("%b %d %H:%M")
    return t.strftime("%Y-%m-%d %H:%M")


def relative(ts: float | None, now: float | None = None) -> str:
    """'just now', '5m ago', 'in 3h05m'."""
    if ts is None:
        return DASH
    delta = (time.time() if now is None else now) - ts
    if abs(delta) < 5:
        return "just now"
    span = duration(abs(delta)) if abs(delta) >= 60 else f"{round(abs(delta))}s"
    return f"{span} ago" if delta > 0 else f"in {span}"


def _is_numeric(value: Any, text: str) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int | float) or bool(_NUMERIC.match(text))


def table(
    rows: Sequence[Sequence[Any]],
    headers: Sequence[str] | None = None,
    *,
    title: str | None = None,
    align: str | None = None,
    shrink: int | None = None,
    width: int | None = None,
    min_shrink: int = 8,
) -> str:
    """Render rows as aligned plain text.

    Numeric-looking columns are right-aligned unless `align` ("l"/"r" per column) says
    otherwise. Column `shrink` is truncated so lines fit `width` (default: terminal width).
    """
    cells = [["" if v is None else str(v) for v in row] for row in rows]
    head = [str(h) for h in headers] if headers else None
    ncols = max([len(head or [])] + [len(r) for r in cells]) if (cells or head) else 0
    for r in cells:
        r.extend([""] * (ncols - len(r)))
    if align is None:
        align = ""
        for c in range(ncols):
            vals = [(row[c] if c < len(row) else None) for row in rows]
            pairs = [(v, cells[i][c]) for i, v in enumerate(vals) if cells[i][c] not in ("", DASH)]
            right = bool(pairs) and all(_is_numeric(v, t) for v, t in pairs)
            align += "r" if right else "l"
    align = align.ljust(ncols, "l")
    widths = [max([len(r[c]) for r in cells] + [len(head[c]) if head else 0]) for c in range(ncols)]
    if shrink is not None and 0 <= shrink < ncols:
        limit = width if width is not None else shutil.get_terminal_size((100, 24)).columns
        excess = sum(widths) + 2 * (ncols - 1) - limit
        if excess > 0:
            widths[shrink] = max(min_shrink, widths[shrink] - excess)
            for r in cells:
                r[shrink] = _clip(r[shrink], widths[shrink])
            if head:
                head[shrink] = _clip(head[shrink], widths[shrink])

    def line(values: list[str]) -> str:
        parts = [
            v.rjust(widths[c]) if align[c] == "r" else v.ljust(widths[c])
            for c, v in enumerate(values)
        ]
        return "  ".join(parts).rstrip()

    out = []
    if title:
        out.append(title)
    if head:
        out.append(line(head))
        out.append("  ".join("-" * w for w in widths))
    out.extend(line(r) for r in cells)
    return "\n".join(out)


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..." if width > 3 else text[:width]
