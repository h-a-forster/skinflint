"""Explain prompt-cache hits and misses by comparing segment hash chains between requests."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from skinflint import fmt
from skinflint.model import Price, Provider, Record, Segment, State, Usage
from skinflint.profile import effective_price
from skinflint.segments import BILLING_LABEL, estimate, fingerprint

VERDICTS = (
    "hit",
    "partial",
    "cold",
    "expired",
    "prefix_changed",
    "no_cache_control",
    "below_minimum",
    "api_reported",
    "unexplained",
)
HIT_RATIO = 0.9
TTL_SECONDS = {"5m": 300.0, "1h": 3600.0}
OPENAI_TTL = 600.0  # in-memory prompt caches live roughly 5-10 minutes
OPENAI_MIN = 1024
CANDIDATES = 200
API_CHANGED = {"model_changed", "system_changed", "tools_changed", "messages_changed"}

_MIN_TOKENS = {
    "fable-5-1": 512,
    "mythos-5-1": 512,
    "opus-5-5": 512,
    "opus-5": 512,
    "sonnet-5-5": 512,
    "fable-5": 512,
    "mythos-5": 512,
    "haiku-5-5": 512,
    "opus-4-8": 1024,
    "sonnet-5": 1024,
    "sonnet-4-6": 1024,
    "sonnet-4-5": 1024,
    "opus-4-1": 1024,
    "opus-4-0": 1024,
    "opus-4": 1024,
    "sonnet-4-0": 1024,
    "sonnet-4": 1024,
    "opus-4-7": 2048,
    "mythos-preview": 2048,
    "3-5-haiku": 2048,
    "haiku-3-5": 2048,
    "opus-4-6": 4096,
    "opus-4-5": 4096,
    "haiku-4-5": 4096,
}


@dataclass(slots=True)
class CacheEvent:
    record_id: int | None
    ts: float
    model: str
    session: str | None
    agent: str | None
    verdict: str
    cause: str | None
    predecessor_id: int | None
    gap_s: float | None
    prompt_tokens: int
    cache_read: int
    cache_write: int
    input_tokens: int
    cacheable_tokens: int  # estimated tokens up to the last breakpoint (whole prompt on OpenAI)
    expected_read: int  # estimated tokens that could have been read from the predecessor
    missed_tokens: int
    extra_cost_usd: float
    api_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class CauseRow:
    cause: str
    count: int
    missed_tokens: int
    extra_cost_usd: float


@dataclass(slots=True)
class CacheSummary:
    requests: int
    hit_rate: float | None  # cache_read / prompt tokens
    counts: dict[str, int]
    extra_cost_usd: float
    missed_tokens: int
    top_causes: list[CauseRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def min_cacheable(provider: Provider, model: str) -> int:
    """Minimum cacheable prefix in tokens for a model (1024 when unknown)."""
    if provider != Provider.ANTHROPIC:
        return OPENAI_MIN
    m = model.lower()
    i = m.find("claude-")
    if i >= 0:
        m = m[i + 7 :]
    m = re.sub(r"\[.*?\]$", "", m)
    m = re.sub(r"(-v\d+(:\d+)?)$", "", m)
    m = re.sub(r"-\d{8}$", "", m)
    m = m.removesuffix("-latest")
    while m:
        if m in _MIN_TOKENS:
            return _MIN_TOKENS[m]
        if "-" not in m:
            break
        m = m.rsplit("-", 1)[0]
    return 1024


@dataclass(slots=True)
class _Item:
    record: Record
    segs: list[Segment]  # comparable segments (billing header removed)
    tokens: list[int]
    cum: list[int]  # cum[k] = tokens of segs[:k]
    chain: list[int]  # chain[k] identifies the hash prefix segs[:k]
    bp_ends: list[int]  # k such that segs[k - 1] is a breakpoint
    ttls: dict[int, str]
    cacheable: int  # segment count of the cacheable prefix
    key: tuple
    msg_start: int  # index of the first messages segment


def _prepare(record: Record, segments: list[Segment]) -> _Item:
    all_tokens = estimate(segments, record.usage.prompt_tokens)
    segs, tokens = [], []
    for seg, n in zip(segments, all_tokens, strict=True):
        if seg.section == "system" and seg.label == BILLING_LABEL:
            continue
        segs.append(seg)
        tokens.append(n)
    cum = [0]
    chain = [0]
    bp_ends: list[int] = []
    ttls: dict[int, str] = {}
    for k, (seg, n) in enumerate(zip(segs, tokens, strict=True), 1):
        cum.append(cum[-1] + n)
        chain.append(hash((chain[-1], seg.hash)))
        if seg.breakpoint:
            bp_ends.append(k)
            ttls[k] = seg.ttl or "5m"
    if record.provider == Provider.ANTHROPIC:
        cacheable = bp_ends[-1] if bp_ends else 0
    else:
        cacheable = len(segs)
    agent = record.agent or ("fp:" + fingerprint(segments))
    key = (record.provider, record.session, agent)
    msg_start = next((i for i, s in enumerate(segs) if s.section == "messages"), len(segs))
    return _Item(record, segs, tokens, cum, chain, bp_ends, ttls, cacheable, key, msg_start)


def _lcp(a: _Item, b: _Item) -> int:
    lo, hi = 0, min(len(a.segs), len(b.segs))
    if a.chain[hi] == b.chain[hi]:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a.chain[mid] == b.chain[mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def analyze(
    items: list[tuple[Record, list[Segment] | None]],
    price_for: Callable[[Record], Price | None],
) -> list[CacheEvent]:
    """One CacheEvent per settled request with usage, in time order."""
    history: dict[tuple, list[_Item]] = {}
    events: list[CacheEvent] = []
    ordered = sorted(enumerate(items), key=lambda p: (p[1][0].ts, p[0]))
    for _, (record, segments) in ordered:
        if record.state != State.OK or record.usage.prompt_tokens <= 0:
            continue
        price = price_for(record)
        if not segments:
            events.append(_usage_only(record, price))
            continue
        item = _prepare(record, segments)
        past = history.setdefault(item.key, [])
        pred, k = None, 0
        for cand in reversed(past[-CANDIDATES:]):
            n = _lcp(item, cand)
            if pred is None or n > k:
                pred, k = cand, n
        if pred is None and str(item.key[2]).startswith("fp:"):
            pred = _same_conversation(item, history)
            k = _lcp(item, pred) if pred is not None else 0
        events.append(_diagnose(item, pred, k, price))
        past.append(item)
    return events


def _same_conversation(item: _Item, history: dict[tuple, list[_Item]]) -> _Item | None:
    """Predecessor from another fingerprint in the same session that shares the conversation.

    A fingerprint changes when tools are added or the system prompt changes, which is exactly
    when a predecessor is needed; the messages section still starts the same way.
    """
    best, best_n = None, 0
    for key, past in history.items():
        if key[:2] != item.key[:2] or key == item.key or not str(key[2]).startswith("fp:"):
            continue
        for cand in reversed(past[-CANDIDATES:]):
            n = _messages_lcp(item, cand)
            if n == 0:
                continue
            cand_msgs = cand.cum[-1] - cand.cum[cand.msg_start]
            shared = cand.cum[cand.msg_start + n] - cand.cum[cand.msg_start]
            if 2 * shared < cand_msgs:
                continue
            if n > best_n or (n == best_n and best is not None and cand.record.ts > best.record.ts):
                best, best_n = cand, n
    return best


def _messages_lcp(a: _Item, b: _Item) -> int:
    n = 0
    for x, y in zip(a.segs[a.msg_start :], b.segs[b.msg_start :], strict=False):
        if x.hash != y.hash:
            break
        n += 1
    return n


def _event(record: Record, **kw) -> CacheEvent:
    u = record.usage
    base = dict(
        record_id=record.id,
        ts=record.ts,
        model=record.model,
        session=record.session,
        agent=record.agent,
        cause=None,
        predecessor_id=None,
        gap_s=None,
        prompt_tokens=u.prompt_tokens,
        cache_read=u.cache_read,
        cache_write=u.cache_write,
        input_tokens=u.input_tokens,
        cacheable_tokens=0,
        expected_read=0,
        missed_tokens=0,
        extra_cost_usd=0.0,
        api_reason=record.cache_miss_reason,
    )
    base.update(kw)
    return CacheEvent(**base)


def _usage_only(record: Record, price: Price | None) -> CacheEvent:
    u = record.usage
    reason = record.cache_miss_reason
    if reason in API_CHANGED:
        missed = min(record.cache_missed_tokens or 0, u.cache_write + u.input_tokens)
        return _event(
            record,
            verdict="api_reported",
            cause=reason,
            missed_tokens=missed,
            extra_cost_usd=extra_cost(missed, u, price),
        )
    ratio = u.cache_read / u.prompt_tokens
    verdict = "hit" if ratio >= HIT_RATIO else "partial" if u.cache_read else "unexplained"
    return _event(record, verdict=verdict, cause=None if verdict == "hit" else "no profile")


def extra_cost(missed: int, usage: Usage, price: Price | None) -> float:
    """Cost of missed tokens: written ones at (write - read), the rest at (input - read)."""
    p = effective_price(price, usage)
    if p is None or missed <= 0:
        return 0.0
    written = min(missed, usage.cache_write)
    uncached = min(missed - written, usage.input_tokens)
    if usage.cache_write:
        write_rate = (
            usage.cache_write_5m * p.cache_write_5m + usage.cache_write_1h * p.cache_write_1h
        ) / usage.cache_write
    else:
        write_rate = p.cache_write_5m
    return (written * (write_rate - p.cache_read) + uncached * (p.input - p.cache_read)) / 1e6


def _diagnose(item: _Item, pred: _Item | None, k: int, price: Price | None) -> CacheEvent:
    r = item.record
    u = r.usage
    read = u.cache_read
    anthropic = r.provider == Provider.ANTHROPIC
    cacheable_tokens = item.cum[item.cacheable]
    minimum = min_cacheable(r.provider, r.model)
    common: dict = dict(cacheable_tokens=cacheable_tokens, agent=item.key[2])
    if pred is not None:
        common.update(predecessor_id=pred.record.id, gap_s=r.ts - pred.record.ts)

    if anthropic and not item.bp_ends and read == 0:
        if u.prompt_tokens < minimum:
            return _event(
                r,
                verdict="below_minimum",
                cause=f"prompt of {fmt.tokens(u.prompt_tokens)} tokens is under the "
                f"{minimum}-token minimum for {r.model}",
                **common,
            )
        missed = item.cum[k] if pred is not None else 0
        return _event(
            r,
            verdict="no_cache_control",
            cause="no cache_control breakpoints in the request",
            missed_tokens=missed,
            extra_cost_usd=extra_cost(missed, u, price),
            **common,
        )

    if read == 0 and cacheable_tokens < minimum:
        return _event(
            r,
            verdict="below_minimum",
            cause=f"cacheable prefix of ~{fmt.tokens(cacheable_tokens)} tokens is under the "
            f"{minimum}-token minimum for {r.model}",
            **common,
        )

    if pred is None:
        if read >= HIT_RATIO * cacheable_tokens and read > 0:
            return _event(r, verdict="hit", **common)
        if r.cache_miss_reason in API_CHANGED:
            missed = min(r.cache_missed_tokens or 0, u.cache_write + u.input_tokens)
            return _event(
                r,
                verdict="api_reported",
                cause=r.cache_miss_reason,
                missed_tokens=missed,
                extra_cost_usd=extra_cost(missed, u, price),
                **common,
            )
        if read > 0:
            return _event(
                r,
                verdict="partial",
                cause="no earlier request from this agent; read a cache shared with another",
                **common,
            )
        return _event(r, verdict="cold", cause="first request of this agent", **common)

    # Tokens that would have been read had nothing changed: the predecessor's longest
    # cached prefix that is still inside this request's cacheable prefix.
    if anthropic:
        ends = [e for e in pred.bp_ends if e <= item.cacheable]
    else:
        ends = [min(len(pred.segs), item.cacheable)]
    full_end = max(ends, default=0)
    expected = item.cum[min(full_end, len(item.segs))]
    common["expected_read"] = expected
    gap = r.ts - pred.record.ts

    if expected == 0:
        if read > 0:
            return _event(r, verdict="partial", **common)
        return _event(
            r, verdict="cold", cause="nothing cached by the previous request applies", **common
        )
    if read >= HIT_RATIO * expected:
        return _event(r, verdict="hit", **common)

    missed = max(0, expected - read)
    cost = extra_cost(missed, u, price)
    where = _name_difference(item, pred, k)
    if r.cache_miss_reason in API_CHANGED:
        api_missed = r.cache_missed_tokens
        if api_missed is not None:
            missed = min(api_missed, u.cache_write + u.input_tokens)
            cost = extra_cost(missed, u, price)
        detail = where.removeprefix("tools changed: ") if where else None
        cause = r.cache_miss_reason + (f": {detail}" if detail else "")
        return _event(
            r,
            verdict="api_reported",
            cause=cause,
            missed_tokens=missed,
            extra_cost_usd=cost,
            **common,
        )
    if pred.record.model != r.model:
        return _event(
            r,
            verdict="prefix_changed",
            cause=f"model changed: {pred.record.model} -> {r.model}",
            missed_tokens=missed,
            extra_cost_usd=cost,
            **common,
        )
    if k < full_end:
        return _event(
            r,
            verdict="prefix_changed",
            cause=where,
            missed_tokens=missed,
            extra_cost_usd=cost,
            **common,
        )
    ttl = _ttl_seconds(pred, full_end) if anthropic else OPENAI_TTL
    if gap > ttl:
        return _event(
            r,
            verdict="expired",
            cause=f"{fmt.duration(gap)} since the previous request exceeds the "
            f"{fmt.duration(ttl)} cache lifetime",
            missed_tokens=missed,
            extra_cost_usd=cost,
            **common,
        )
    verdict = "partial" if read > 0 else "unexplained"
    return _event(r, verdict=verdict, missed_tokens=missed, extra_cost_usd=cost, **common)


def _ttl_seconds(pred: _Item, end: int) -> float:
    ttls = [TTL_SECONDS.get(t, 300.0) for e, t in pred.ttls.items() if e <= end]
    return max(ttls, default=300.0)


def _name_difference(item: _Item, pred: _Item, k: int) -> str | None:
    """Plain description of the first segment that differs from the predecessor."""
    cur = item.segs[k] if k < len(item.segs) else None
    old = pred.segs[k] if k < len(pred.segs) else None
    if cur is None and old is None:
        return None
    section = (cur or old).section  # type: ignore[union-attr]
    if section == "tools" or (old is not None and old.section == "tools"):
        return "tools changed: " + _tools_change(item, pred)
    if section == "system" or (old is not None and old.section == "system"):
        if cur is None or cur.section != "system":
            return f"{old.label} removed"  # type: ignore[union-attr]
        if old is None or old.section != "system":
            return f"{cur.label} added"
        return f"{cur.label} changed"
    if cur is None:
        return f"messages[{old.message}] removed"  # type: ignore[union-attr]
    if old is None:
        return f"messages[{cur.message}] added"
    return f"messages[{cur.message}] edited ({cur.label})"


def _tools_change(item: _Item, pred: _Item) -> str:
    new = {s.label: s.hash for s in item.segs if s.section == "tools"}
    old = {s.label: s.hash for s in pred.segs if s.section == "tools"}
    parts = []
    added = [n for n in new if n not in old]
    removed = [n for n in old if n not in new]
    edited = [n for n in new if n in old and new[n] != old[n]]
    for names, verb in ((added, "added"), (removed, "removed"), (edited, "schema changed")):
        if names:
            shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
            parts.append(f"{shown} {verb}")
    if not parts:
        parts.append("tools reordered")
    return "; ".join(parts)


def summarize(events: list[CacheEvent], top: int = 5) -> CacheSummary:
    prompt = sum(e.prompt_tokens for e in events)
    read = sum(e.cache_read for e in events)
    counts = Counter(e.verdict for e in events)
    causes: dict[str, CauseRow] = {}
    for e in events:
        if e.verdict == "hit":
            continue
        key = _cause_key(e)
        row = causes.setdefault(key, CauseRow(key, 0, 0, 0.0))
        row.count += 1
        row.missed_tokens += e.missed_tokens
        row.extra_cost_usd += e.extra_cost_usd
    ranked = sorted(causes.values(), key=lambda c: (-c.extra_cost_usd, -c.count, c.cause))
    return CacheSummary(
        requests=len(events),
        hit_rate=read / prompt if prompt else None,
        counts={v: counts[v] for v in VERDICTS if counts[v]},
        extra_cost_usd=sum(e.extra_cost_usd for e in events),
        missed_tokens=sum(e.missed_tokens for e in events),
        top_causes=ranked[:top],
    )


def _cause_key(e: CacheEvent) -> str:
    if e.verdict in ("prefix_changed", "api_reported") and e.cause:
        return f"{e.verdict}: {e.cause}"
    return e.verdict
