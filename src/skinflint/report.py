"""Text rendering for CLI output. Every function takes data and returns a string."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from skinflint import fmt
from skinflint.model import Price, Record, Usage

NO_DATA = "no requests recorded yet; start the proxy with `skinflint serve`"
NOTIONAL = "subscription traffic: costs are API-equivalent, not billed"
ESTIMATED = "~ estimated: no exact price for the model, or a stream cut off before its usage"


def cost(usd: float, estimated: bool = False) -> str:
    return fmt.money(usd) + ("~" if estimated else "")


def short_id(value: str | None, n: int = 8) -> str:
    if not value:
        return fmt.DASH
    if value.startswith("fp:"):
        return value
    return value[:n]


def _with_total(text: str) -> str:
    """Repeat the header rule above the last row (the total)."""
    lines = text.split("\n")
    if len(lines) >= 4:
        lines.insert(len(lines) - 1, lines[1])
    return "\n".join(lines)


def _footnotes(estimated: bool, notional: bool) -> list[str]:
    out = []
    if estimated:
        out.append(ESTIMATED)
    if notional:
        out.append(NOTIONAL)
    return out


# -- report ---------------------------------------------------------------------------------


@dataclass(slots=True)
class ReportRow:
    key: str | None
    requests: int
    blocked: int
    errors: int
    usage: Usage
    cost_usd: float
    cost_estimated: bool = False
    plan: bool = False

    @property
    def cache_hit_rate(self) -> float | None:
        return self.usage.cache_hit_rate

    def to_dict(self) -> dict[str, Any]:
        u = self.usage
        return {
            "key": self.key,
            "requests": self.requests,
            "blocked": self.blocked,
            "errors": self.errors,
            "prompt_tokens": u.prompt_tokens,
            "input_tokens": u.input_tokens,
            "cache_read": u.cache_read,
            "cache_write": u.cache_write,
            "output_tokens": u.output_tokens,
            "cache_hit_rate": self.cache_hit_rate,
            "cost_usd": self.cost_usd,
            "cost_estimated": self.cost_estimated,
            "plan": self.plan,
        }


@dataclass(slots=True)
class Report:
    by: str
    since: float | None
    until: float | None
    rows: list[ReportRow]
    total: ReportRow
    filters: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "by": self.by,
            "since": self.since,
            "until": self.until,
            "filters": self.filters,
            "rows": [r.to_dict() for r in self.rows],
            "total": self.total.to_dict(),
        }


def sum_usage(usages: Iterable[Usage]) -> Usage:
    out = Usage()
    for u in usages:
        out.input_tokens += u.input_tokens
        out.cache_write_5m += u.cache_write_5m
        out.cache_write_1h += u.cache_write_1h
        out.cache_read += u.cache_read
        out.output_tokens += u.output_tokens
        out.reasoning_tokens += u.reasoning_tokens
        out.web_search_requests += u.web_search_requests
    return out


def total_row(rows: Sequence[ReportRow]) -> ReportRow:
    return ReportRow(
        key="total",
        requests=sum(r.requests for r in rows),
        blocked=sum(r.blocked for r in rows),
        errors=sum(r.errors for r in rows),
        usage=sum_usage(r.usage for r in rows),
        cost_usd=sum(r.cost_usd for r in rows),
        cost_estimated=any(r.cost_estimated for r in rows),
        plan=any(r.plan for r in rows),
    )


def _period(since: float | None, until: float | None) -> str:
    start = "all time" if since is None else f"since {fmt.timestamp(since)}"
    return start + ("" if until is None else f" until {fmt.timestamp(until)}")


def render_report(rep: Report) -> str:
    title = f"by {rep.by}, {_period(rep.since, rep.until)}"
    if rep.filters:
        title += " (" + ", ".join(f"{k} {v}" for k, v in rep.filters.items()) + ")"
    if not rep.rows:
        return title + "\nno requests in this period"
    key_name = rep.by
    rows = []
    for r in [*rep.rows, rep.total]:
        key = r.key if r.key is not None else "(none)"
        if rep.by in ("session", "agent") and r is not rep.total:
            key = short_id(r.key) if r.key else "(none)"
        rows.append(
            [
                key,
                r.requests,
                r.blocked or fmt.DASH,
                fmt.tokens(r.usage.prompt_tokens),
                fmt.percent(r.cache_hit_rate),
                fmt.tokens(r.usage.output_tokens),
                cost(r.cost_usd, r.cost_estimated),
            ]
        )
    text = fmt.table(
        rows,
        [key_name, "requests", "blocked", "prompt", "cached", "output", "cost"],
        align="lrrrrrr",
        shrink=0,
    )
    out = [title, _with_total(text)]
    notes = _footnotes(rep.total.cost_estimated, rep.total.plan)
    if notes:
        out.append("")
        out.extend(notes)
    return "\n".join(out)


# -- sessions -------------------------------------------------------------------------------


def render_sessions(sessions: Sequence[Any], now: float | None = None) -> str:
    """Rows of store.SessionSummary."""
    if not sessions:
        return "no sessions in this period"
    now = time.time() if now is None else now
    rows = [
        [
            short_id(s.id),
            s.scope,
            s.client or fmt.DASH,
            fmt.relative(s.last_ts, now),
            s.requests,
            fmt.tokens(s.usage.prompt_tokens),
            fmt.percent(s.usage.cache_hit_rate),
            cost(s.cost_usd, s.cost_estimated),
        ]
        for s in sessions
    ]
    text = fmt.table(
        rows,
        ["session", "scope", "client", "last seen", "requests", "prompt", "cached", "cost"],
        align="llllrrrr",
        shrink=1,
    )
    notes = _footnotes(any(s.cost_estimated for s in sessions), any(s.plan for s in sessions))
    return "\n".join([text, *([""] + notes if notes else [])])


# -- profile --------------------------------------------------------------------------------


def _group_table(groups: Sequence[Any], cost_col: bool = True) -> str:
    rows = []
    for g in groups:
        row = [g.group, fmt.tokens(g.tokens), fmt.percent(g.share)]
        if cost_col:
            row.append(fmt.money(g.cost_usd))
        rows.append(row)
    headers = ["group", "tokens", "share"] + (["cost"] if cost_col else [])
    return fmt.table(rows, headers, align="lrrr", shrink=0)


def render_request_profile(record: Record, prof: Any) -> str:
    u = record.usage
    head = (
        f"request #{record.id}  {fmt.timestamp(record.ts)}  {record.model}  "
        f"prompt {fmt.tokens(u.prompt_tokens)}"
    )
    if u.cache_hit_rate is not None:
        head += f" ({fmt.percent(u.cache_hit_rate)} cached)"
    head += (
        f"  output {fmt.tokens(u.output_tokens)}  {cost(record.cost_usd, record.cost_estimated)}"
    )
    lines = [head]
    if record.session:
        lines.append(f"session {record.session}  scope {record.scope}")
    lines.append("")
    if prof.groups:
        lines.append(_group_table(prof.groups))
    else:
        lines.append("no segments")
    lines.append(f"\nprompt cost {fmt.money(prof.cost_usd)} of {fmt.money(record.cost_usd)} total")
    if record.plan:
        lines.append(NOTIONAL)
    return "\n".join(lines)


def render_session_profile(session: str, prof: Any) -> str:
    lines = [
        f"session {session}  {prof.requests} requests  prompt {fmt.tokens(prof.total_tokens)}  "
        f"prompt cost {fmt.money(prof.cost_usd)}"
    ]
    if prof.skipped:
        lines[0] += f"  ({prof.skipped} without a profile)"
    if not prof.groups:
        lines.append("no profiled requests in this session")
        return "\n".join(lines)
    lines += ["", _group_table(prof.groups)]
    if prof.unused_mcp_servers:
        rows = [
            [
                s.server,
                s.tools,
                fmt.tokens(s.tokens_per_request),
                s.requests,
                fmt.tokens(s.tokens),
                fmt.money(s.cost_usd),
            ]
            for s in prof.unused_mcp_servers
        ]
        lines += [
            "",
            fmt.table(
                rows,
                ["unused MCP server", "tools", "per req", "requests", "tokens", "cost"],
                align="lrrrrr",
                shrink=0,
            ),
        ]
    if prof.unused_tools:
        top = prof.unused_tools[:15]
        rows = [
            [t.name, fmt.tokens(t.tokens_per_request), fmt.tokens(t.tokens), fmt.money(t.cost_usd)]
            for t in top
        ]
        title = None
        if len(prof.unused_tools) > len(top):
            title = f"{len(prof.unused_tools)} unused tools, largest {len(top)}:"
        lines += [
            "",
            fmt.table(
                rows,
                ["unused tool", "per req", "tokens", "cost"],
                title=title,
                align="lrrr",
                shrink=0,
            ),
        ]
    if prof.largest_tool_results:
        rows = [
            [
                r.tool,
                fmt.tokens(r.tokens),
                f"#{r.record_id}" if r.record_id else fmt.DASH,
                r.carried,
            ]
            for r in prof.largest_tool_results
        ]
        lines += [
            "",
            fmt.table(
                rows, ["tool result", "tokens", "first in", "carried"], align="lrrr", shrink=0
            ),
        ]
    if prof.instruction_files:
        rows = [
            [i.label, fmt.tokens(i.tokens), f"{i.chars:,}", i.requests]
            for i in prof.instruction_files
        ]
        lines += [
            "",
            fmt.table(
                rows, ["instruction file", "tokens", "chars", "requests"], align="lrrr", shrink=0
            ),
        ]
    if prof.suggestions:
        lines += ["", "suggestions:"]
        lines += [f"  - {s}" for s in prof.suggestions]
    return "\n".join(lines)


def _signed(n: int) -> str:
    return ("+" if n > 0 else "") + fmt.tokens(n)


def render_diff(a: Record, b: Record, d: Any) -> str:
    lines = [
        f"#{a.id} -> #{b.id}: {fmt.tokens(d.tokens_before)} -> {fmt.tokens(d.tokens_after)} "
        f"({_signed(d.delta)} tokens, estimated)"
    ]
    if d.groups:
        rows = [
            [g.group, fmt.tokens(g.tokens_before), fmt.tokens(g.tokens_after), _signed(g.delta)]
            for g in d.groups
        ]
        lines += ["", fmt.table(rows, ["group", "before", "after", "delta"], align="lrrr")]
    changes = (
        [("+", c) for c in d.added] + [("-", c) for c in d.removed] + [("~", c) for c in d.changed]
    )
    if changes:
        rows = [
            [mark, c.group, c.label, fmt.tokens(c.tokens_before), fmt.tokens(c.tokens_after)]
            for mark, c in changes
        ]
        lines += [
            "",
            fmt.table(rows, ["", "group", "segment", "before", "after"], align="lllrr", shrink=2),
        ]
    if not d.groups and not changes:
        lines.append("no differences")
    return "\n".join(lines)


# -- cache ----------------------------------------------------------------------------------


def render_cache(session: str | None, events: Sequence[Any], summary: Any) -> str:
    head = f"session {session}" if session else "requests"
    if not events:
        return f"{head}: no completed requests with usage"
    rows = []
    for i, e in enumerate(events, 1):
        rows.append(
            [
                i,
                fmt.timestamp(e.ts),
                short_id(e.agent),
                fmt.tokens(e.prompt_tokens),
                fmt.tokens(e.cache_read),
                fmt.tokens(e.cache_write),
                e.verdict,
                e.cause or "",
                fmt.money(e.extra_cost_usd) if e.extra_cost_usd else "",
            ]
        )
    text = fmt.table(
        rows,
        ["#", "time", "agent", "prompt", "read", "write", "verdict", "cause", "extra"],
        align="rllrrrllr",
        shrink=7,
        min_shrink=16,
    )
    counts = ", ".join(f"{n} {v}" for v, n in summary.counts.items())
    plural = "s" if summary.requests != 1 else ""
    lines = [
        f"{head}: {summary.requests} request{plural}, hit rate {fmt.percent(summary.hit_rate)}",
        "",
        text,
        "",
        f"verdicts: {counts}",
        f"extra cost from misses: {fmt.money(summary.extra_cost_usd)} "
        f"({fmt.tokens(summary.missed_tokens)} tokens not read from cache)",
    ]
    if summary.top_causes:
        lines.append("top causes:")
        for c in summary.top_causes:
            lines.append(
                f"  {c.count} x {c.cause}"
                + (f"  {fmt.money(c.extra_cost_usd)}" if c.extra_cost_usd else "")
            )
    return "\n".join(lines)


# -- budget ---------------------------------------------------------------------------------


def _limit_text(kind: str, v: float) -> str:
    if kind == "usd":
        return fmt.money(v)
    if kind == "tokens":
        return fmt.tokens(v)
    return f"{int(v):,}"


def render_budget(statuses: Sequence[Any], now: float | None = None) -> str:
    if not statuses:
        return "no budgets configured"
    now = time.time() if now is None else now
    rows = []
    for st in statuses:
        rule = st.rule
        name = rule.name + (" (warn)" if str(rule.action) == "warn" else "")
        resets = "never" if st.end is None else fmt.relative(st.end, now)
        if st.spend is None:
            limits = ", ".join(_limit_text(k, v) for k, v in st.limits.items())
            rows.append([name, rule.window, rule.per, f"- / {limits}", st.note or "", resets])
            continue
        used = {"usd": st.spend.usd, "tokens": st.spend.tokens, "requests": st.spend.requests}
        for i, (kind, limit) in enumerate(st.limits.items()):
            spent = f"{_limit_text(kind, used[kind])} / {_limit_text(kind, limit)}"
            remaining = _limit_text(kind, st.remaining.get(kind, 0))
            first = i == 0
            rows.append(
                [
                    name if first else "",
                    rule.window if first else "",
                    rule.per if first else "",
                    spent,
                    remaining,
                    resets if first else "",
                ]
            )
    return fmt.table(
        rows,
        ["budget", "window", "per", "spent / limit", "remaining", "resets"],
        align="lllrrl",
    )


# -- prices ---------------------------------------------------------------------------------


def _rate(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") if v else "0"


def render_prices(models: Sequence[tuple[Any, str, Price]], as_of: str | None) -> str:
    rows = [
        [
            str(p),
            m,
            _rate(pr.input),
            _rate(pr.output),
            _rate(pr.cache_write_5m),
            _rate(pr.cache_write_1h),
            _rate(pr.cache_read),
        ]
        for p, m, pr in models
    ]
    text = fmt.table(
        rows,
        ["provider", "model", "input", "output", "write 5m", "write 1h", "read"],
        align="llrrrrr",
    )
    return f"USD per million tokens, as of {as_of or 'unknown'}\n{text}"


def render_price(query: str, provider: Any, model: str, how: str, price: Price) -> str:
    lines = [f"{query} -> {provider} {model} ({how})", "USD per million tokens:"]

    def block(label: str, p: Price) -> None:
        rows = [
            ["input", _rate(p.input)],
            ["output", _rate(p.output)],
            ["cache write 5m", _rate(p.cache_write_5m)],
            ["cache write 1h", _rate(p.cache_write_1h)],
            ["cache read", _rate(p.cache_read)],
        ]
        if label:
            lines.append(label)
        lines.append("\n".join("  " + ln for ln in fmt.table(rows, align="lr").split("\n")))

    block("", price)
    if price.long is not None:
        block(f"prompts over {price.long_context_threshold:,} tokens:", price.long)
    if price.fast is not None:
        block("fast mode:", price.fast)
    if price.web_search_per_1k:
        lines.append(f"web search: ${_rate(price.web_search_per_1k)} per 1,000 searches")
    return "\n".join(lines)


# -- run / serve ----------------------------------------------------------------------------


def run_summary(
    scope: str,
    requests: int,
    usage: Usage,
    cost_usd: float,
    estimated: bool,
    blocked: int,
    cap: float | None = None,
) -> str:
    parts = [f"skinflint: {scope}", f"{requests} request{'s' if requests != 1 else ''}"]
    prompt = f"prompt {fmt.tokens(usage.prompt_tokens)}"
    if usage.cache_hit_rate is not None:
        prompt += f" ({fmt.percent(usage.cache_hit_rate)} cached)"
    parts += [prompt, f"output {fmt.tokens(usage.output_tokens)}"]
    spent = cost(cost_usd, estimated)
    if cap is not None:
        spent += f" of {fmt.money(cap)}"
    parts.append(spent)
    if blocked:
        parts.append(f"{blocked} blocked")
    return "  ".join(parts)


def banner(
    version: str,
    url: str,
    config: str | None,
    budgets: int,
    ledger: str,
    env_lines: Sequence[str],
    warning: str | None = None,
) -> str:
    lines = [f"skinflint {version} listening on {url}"]
    if config:
        lines.append(f"  config  {config} ({budgets} budget{'s' if budgets != 1 else ''})")
    else:
        lines.append("  config  none: no budgets")
    lines.append(f"  ledger  {ledger}")
    if warning:
        lines.append(f"  warning: {warning}")
    lines.append("point agents at it:")
    lines += [f"  {ln}" for ln in env_lines]
    return "\n".join(lines)
