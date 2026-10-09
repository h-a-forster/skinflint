"""One status line for Claude Code's `statusLine` command. Never raises; stays fast.

Claude Code pipes a JSON object on stdin (session_id, model, workspace, cost, ...). Only
`session_id` is used; everything else comes from the ledger, opened read-only.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

NO_DATA = "skinflint: no data"
SEP = " · "


def _money(usd: float) -> str:
    if usd == 0:
        return "$0"
    if usd < 0.005:
        return "<$0.01"
    return f"${usd:,.2f}"


def _limit(usd: float) -> str:
    return f"${usd:,.0f}" if float(usd).is_integer() else f"${usd:,.2f}"


def parse_input(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _ratelimit_parts(store: Any, now: float) -> list[str]:
    from skinflint.model import Provider

    saved = store.ratelimit(Provider.ANTHROPIC)
    if not saved:
        return []
    _ts, headers = saved
    parts = []
    for window in ("5h", "7d"):
        prefix = f"anthropic-ratelimit-unified-{window}-"
        try:
            used = float(headers[prefix + "utilization"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            if float(headers.get(prefix + "reset", "inf")) < now:
                continue
        except (TypeError, ValueError):
            pass
        parts.append(f"{window} {used * 100:.0f}%")
    return parts


def _tightest(statuses: list[Any], window: str, per: str) -> Any | None:
    best = None
    for st in statuses:
        rule = st.rule
        if st.spend is None or rule.usd is None or rule.usd <= 0:
            continue
        if str(rule.window) != window or str(rule.per) != per:
            continue
        frac = st.spend.usd / rule.usd
        if best is None or frac > best[0]:
            best = (frac, st)
    return best[1] if best else None


def render(stdin_text: str, now: float | None = None, config_path: Any = None) -> str:
    """The status line for this stdin payload."""
    from datetime import datetime

    from skinflint import config
    from skinflint.store import Store

    now = time.time() if now is None else now
    data = parse_input(stdin_text)
    session = data.get("session_id")
    session = session if isinstance(session, str) and session else None
    try:
        cfg = config.load(config_path)
    except Exception:
        cfg = config.Config()
    if not cfg.db_path.exists():
        return NO_DATA
    store = Store.open_readonly(cfg.db_path)
    try:
        statuses: list[Any] = []
        if cfg.budgets:
            try:
                from skinflint.budget import Budget

                statuses = Budget(cfg.budgets, store, None).status(session=session, now=now)
            except Exception:
                statuses = []
        parts: list[str] = []
        hit = None
        if session:
            agg = store.aggregate("session", session=session)
            if agg and (agg[0].requests or agg[0].blocked):
                text = _money(agg[0].cost_usd)
                rule = _tightest(statuses, "total", "session")
                if rule is not None:
                    text = f"{_money(rule.spend.usd)}/{_limit(rule.rule.usd)}"
                parts.append(f"{text} session")
                hit = agg[0].usage.cache_hit_rate
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        today = store.aggregate("provider", since=midnight.timestamp())
        day_cost = sum(a.cost_usd for a in today)
        day_rule = _tightest(statuses, "day", "all")
        if day_rule is not None:
            parts.append(f"{_money(day_rule.spend.usd)}/{_limit(day_rule.rule.usd)} today")
        elif today:
            parts.append(f"{_money(day_cost)} today")
        if hit is None and today:
            prompt = sum(a.usage.prompt_tokens for a in today)
            hit = sum(a.usage.cache_read for a in today) / prompt if prompt else None
        if hit is not None:
            parts.append(f"cache {hit * 100:.0f}%")
        parts += _ratelimit_parts(store, now)
    finally:
        store.close()
    return "skinflint " + SEP.join(parts) if parts else NO_DATA


def main(stdin: Any = None, stdout: Any = None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        text = stdin.read() if stdin is not None and not stdin.isatty() else ""
    except Exception:
        text = ""
    try:
        line = render(text)
    except Exception:
        line = NO_DATA
    try:
        buffer = getattr(stdout, "buffer", None)
        if buffer is not None:
            stdout.flush()
            buffer.write((line + "\n").encode("utf-8"))
            buffer.flush()
        else:
            stdout.write(line + "\n")
            stdout.flush()
    except Exception:
        pass
    return 0
