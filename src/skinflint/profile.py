"""Where the prompt tokens of a request or a session go, and what they cost."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from skinflint import fmt
from skinflint.model import Price, Record, Segment, State, Usage
from skinflint.segments import estimate, mcp_server


@dataclass(slots=True)
class GroupRow:
    group: str
    segments: int
    tokens: int
    share: float
    cost_usd: float


@dataclass(slots=True)
class RequestProfile:
    total_tokens: int
    cost_usd: float
    groups: list[GroupRow]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class UnusedTool:
    name: str
    group: str
    tokens_per_request: int
    requests: int
    tokens: int
    cost_usd: float


@dataclass(slots=True)
class UnusedServer:
    server: str
    tools: int
    tokens_per_request: int
    requests: int
    tokens: int
    cost_usd: float


@dataclass(slots=True)
class ToolResultRow:
    tool: str
    tokens: int
    record_id: int | None
    message: int
    carried: int  # requests that contained this exact result


@dataclass(slots=True)
class InstructionRow:
    label: str
    chars: int
    tokens: int
    requests: int


@dataclass(slots=True)
class SessionProfile:
    requests: int
    skipped: int  # records without segments or usage
    total_tokens: int
    cost_usd: float
    groups: list[GroupRow]
    unused_tools: list[UnusedTool]
    unused_mcp_servers: list[UnusedServer]
    largest_tool_results: list[ToolResultRow]
    instruction_files: list[InstructionRow]
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class SegmentChange:
    section: str
    label: str
    group: str
    tokens_before: int
    tokens_after: int
    delta: int


@dataclass(slots=True)
class GroupDelta:
    group: str
    tokens_before: int
    tokens_after: int
    delta: int


@dataclass(slots=True)
class ProfileDiff:
    tokens_before: int
    tokens_after: int
    delta: int
    added: list[SegmentChange]
    removed: list[SegmentChange]
    changed: list[SegmentChange]
    groups: list[GroupDelta]

    def to_dict(self) -> dict:
        return asdict(self)


def effective_price(price: Price | None, usage: Usage) -> Price | None:
    """The rate table that applies to this request (long-context and fast variants)."""
    if price is None:
        return None
    if usage.speed == "fast" and price.fast is not None:
        price = price.fast
    threshold = price.long_context_threshold
    if threshold is not None and price.long is not None and usage.prompt_tokens > threshold:
        price = price.long
    return price


def segment_tokens(segments: list[Segment], prompt_tokens: int) -> list[int]:
    """Calibrated est_tokens when they match prompt_tokens, else a fresh estimate."""
    current = [s.est_tokens for s in segments]
    if prompt_tokens > 0 and sum(current) == prompt_tokens:
        return current
    if prompt_tokens <= 0 and any(current):
        return current
    return estimate(segments, prompt_tokens)


def segment_costs(tokens: list[int], usage: Usage, price: Price | None) -> list[float]:
    """Split the prompt cost over segments in prefix order: read, 1h write, 5m write, input."""
    p = effective_price(price, usage)
    if p is None:
        return [0.0] * len(tokens)
    spans = (
        (usage.cache_read, p.cache_read),
        (usage.cache_write_1h, p.cache_write_1h),
        (usage.cache_write_5m, p.cache_write_5m),
    )
    bounds: list[tuple[int, float]] = []
    pos = 0
    for n, rate in spans:
        if n > 0:
            pos += n
            bounds.append((pos, rate))
    out = []
    start = 0
    for n in tokens:
        end = start + n
        cost = 0.0
        lo = start
        for b, rate in bounds:
            if lo >= end:
                break
            if b > lo:
                hi = min(b, end)
                cost += (hi - lo) * rate
                lo = hi
        if lo < end:
            cost += (end - lo) * p.input
        out.append(cost / 1e6)
        start = end
    return out


def request_profile(segments: list[Segment], usage: Usage, price: Price | None) -> RequestProfile:
    tokens = segment_tokens(segments, usage.prompt_tokens)
    costs = segment_costs(tokens, usage, price)
    acc: dict[str, list] = {}
    for seg, n, c in zip(segments, tokens, costs, strict=True):
        row = acc.setdefault(seg.group, [0, 0, 0.0])
        row[0] += 1
        row[1] += n
        row[2] += c
    total = sum(tokens)
    return RequestProfile(total, sum(costs), _group_rows(acc, total))


def _group_rows(acc: dict[str, list], total: int) -> list[GroupRow]:
    rows = [
        GroupRow(g, cnt, n, n / total if total else 0.0, cost) for g, (cnt, n, cost) in acc.items()
    ]
    rows.sort(key=lambda r: (-r.tokens, r.group))
    return rows


def session_profile(
    items: list[tuple[Record, list[Segment] | None]],
    price_for: Callable[[Record], Price | None],
    top: int = 10,
) -> SessionProfile:
    """Totals across a session plus unused tools, big tool results and suggestions."""
    acc: dict[str, list] = {}
    tool_defs: dict[str, list] = {}  # name -> [group, requests, tokens, cost]
    called: set[str] = set()
    results: dict[str, ToolResultRow] = {}
    instructions: dict[str, InstructionRow] = {}
    requests = skipped = 0
    total_tokens = 0
    total_cost = 0.0
    cache_read = 0
    instr_per_request = 0
    for record, segments in items:
        if record.state == State.BLOCKED or not segments:
            skipped += 1
            continue
        requests += 1
        usage = record.usage
        tokens = segment_tokens(segments, usage.prompt_tokens)
        costs = segment_costs(tokens, usage, price_for(record))
        total_tokens += sum(tokens)
        total_cost += sum(costs)
        cache_read += usage.cache_read
        instr_per_request = max(
            instr_per_request,
            sum(n for s, n in zip(segments, tokens, strict=True) if s.kind == "instructions"),
        )
        for seg, n, c in zip(segments, tokens, costs, strict=True):
            row = acc.setdefault(seg.group, [0, 0, 0.0])
            row[0] += 1
            row[1] += n
            row[2] += c
            if seg.section == "tools":
                d = tool_defs.setdefault(seg.label, [seg.group, 0, 0, 0.0])
                d[1] += 1
                d[2] += n
                d[3] += c
            elif seg.kind == "tool_use":
                called.add(seg.label)
            elif seg.kind == "tool_result":
                r = results.get(seg.hash)
                if r is None:
                    results[seg.hash] = ToolResultRow(seg.label, n, record.id, seg.message, 1)
                else:
                    r.carried += 1
                    r.tokens = max(r.tokens, n)
            elif seg.kind == "instructions":
                ins = instructions.get(seg.hash)
                if ins is None:
                    instructions[seg.hash] = InstructionRow(seg.label, seg.chars, n, 1)
                else:
                    ins.requests += 1
                    ins.tokens = max(ins.tokens, n)

    unused = [
        UnusedTool(name, g, round(n / reqs) if reqs else 0, reqs, n, c)
        for name, (g, reqs, n, c) in tool_defs.items()
        if name not in called
    ]
    unused.sort(key=lambda u: (-u.tokens, u.name))

    servers: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0.0, False])
    for name, (_g, reqs, n, c) in tool_defs.items():
        server = mcp_server(name)
        if server is None:
            continue
        s = servers[server]
        s[0] += 1
        s[1] = max(s[1], reqs)
        s[2] += n
        s[3] += c
        s[4] = s[4] or name in called
    unused_servers = [
        UnusedServer(srv, cnt, round(n / reqs) if reqs else 0, reqs, n, c)
        for srv, (cnt, reqs, n, c, used) in servers.items()
        if not used
    ]
    unused_servers.sort(key=lambda u: (-u.tokens, u.server))

    largest = sorted(results.values(), key=lambda r: (-r.tokens, r.tool))[:top]
    instr = sorted(instructions.values(), key=lambda r: -r.tokens)

    profile = SessionProfile(
        requests=requests,
        skipped=skipped,
        total_tokens=total_tokens,
        cost_usd=total_cost,
        groups=_group_rows(acc, total_tokens),
        unused_tools=unused,
        unused_mcp_servers=unused_servers,
        largest_tool_results=largest,
        instruction_files=instr,
    )
    profile.suggestions = _suggestions(profile, cache_read, instr_per_request)
    return profile


def _suggestions(p: SessionProfile, cache_read: int, instr_tokens: int) -> list[str]:
    out: list[str] = []
    for s in p.unused_mcp_servers:
        if s.tokens_per_request < 200:
            continue
        out.append(
            f"MCP server '{s.server}' adds {fmt.tokens(s.tokens_per_request)} tokens to each of "
            f"{s.requests} requests ({fmt.tokens(s.tokens)} tokens, {fmt.money(s.cost_usd)}) "
            "and was never called."
        )
    builtin = [
        u for u in p.unused_tools if mcp_server(u.name) is None and u.group != "tools: server"
    ]
    per_req = sum(u.tokens_per_request for u in builtin)
    if builtin and per_req >= 2000:
        cost = sum(u.cost_usd for u in builtin)
        out.append(
            f"{len(builtin)} built-in tools were never called; together they add "
            f"{fmt.tokens(per_req)} tokens to each request ({fmt.money(cost)} this session)."
        )
    for r in p.largest_tool_results[:3]:
        if r.tokens >= 5000 and r.carried >= 2:
            out.append(
                f"A {r.tool} result of {fmt.tokens(r.tokens)} tokens was carried in "
                f"{r.carried} requests."
            )
    if instr_tokens >= 3000:
        names = ", ".join(dict.fromkeys(i.label for i in p.instruction_files))
        out.append(
            f"Instruction files ({names}) add {fmt.tokens(instr_tokens)} tokens to each request."
        )
    if p.requests >= 3 and p.total_tokens and cache_read / p.total_tokens < 0.5:
        out.append(
            f"Only {fmt.percent(cache_read / p.total_tokens)} of prompt tokens were cache reads; "
            "see the cache report for the misses."
        )
    return out


def diff(a: list[Segment], b: list[Segment]) -> ProfileDiff:
    """Segments added, removed and changed from a to b, matched by (section, label, n-th)."""
    ta, tb = _tokens_or_estimate(a), _tokens_or_estimate(b)
    ka = _keyed(a, ta)
    kb = _keyed(b, tb)
    added, removed, changed = [], [], []
    for key, (seg, n) in kb.items():
        old = ka.get(key)
        if old is None:
            added.append(SegmentChange(seg.section, seg.label, seg.group, 0, n, n))
        elif old[0].hash != seg.hash:
            changed.append(SegmentChange(seg.section, seg.label, seg.group, old[1], n, n - old[1]))
    for key, (seg, n) in ka.items():
        if key not in kb:
            removed.append(SegmentChange(seg.section, seg.label, seg.group, n, 0, -n))
    ga: dict[str, int] = defaultdict(int)
    gb: dict[str, int] = defaultdict(int)
    for seg, n in zip(a, ta, strict=True):
        ga[seg.group] += n
    for seg, n in zip(b, tb, strict=True):
        gb[seg.group] += n
    groups = [
        GroupDelta(g, ga.get(g, 0), gb.get(g, 0), gb.get(g, 0) - ga.get(g, 0))
        for g in dict.fromkeys([*ga, *gb])
        if ga.get(g, 0) != gb.get(g, 0)
    ]
    groups.sort(key=lambda d: -abs(d.delta))
    sa, sb = sum(ta), sum(tb)
    return ProfileDiff(sa, sb, sb - sa, added, removed, changed, groups)


def _tokens_or_estimate(segments: list[Segment]) -> list[int]:
    if any(s.est_tokens for s in segments):
        return [s.est_tokens for s in segments]
    return estimate(segments)


def _keyed(segments: list[Segment], tokens: list[int]) -> dict[tuple, tuple[Segment, int]]:
    seen: dict[tuple[str, str], int] = defaultdict(int)
    out = {}
    for seg, n in zip(segments, tokens, strict=True):
        k = (seg.section, seg.label)
        out[(*k, seen[k])] = (seg, n)
        seen[k] += 1
    return out
