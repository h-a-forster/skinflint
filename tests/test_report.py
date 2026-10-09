import json
import time
from pathlib import Path

import pytest

from skinflint import cachedoctor, profile, report
from skinflint.budget import Budget
from skinflint.model import BudgetRule, Endpoint, Per, Price, Provider, Record, State, Usage, Window
from skinflint.pricing import Pricing
from skinflint.segments import calibrate, segment
from skinflint.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
NOW = time.time()
S1 = "aaaa1111-0000-4000-8000-000000000001"
S2 = "bbbb2222-0000-4000-8000-000000000002"


def fixture_segments(name: str, prompt_tokens: int):
    body = json.loads((FIXTURES / f"{name}.request.json").read_text(encoding="utf-8"))
    segs = segment(Provider.ANTHROPIC, Endpoint.MESSAGES, body)
    calibrate(segs, prompt_tokens)
    return segs


def rec(**kw) -> Record:
    base = dict(
        ts=NOW - 600,
        provider=Provider.ANTHROPIC,
        endpoint=Endpoint.MESSAGES,
        scope="proj",
        model="claude-sonnet-5-5",
        state=State.OK,
        session=S1,
        agent="fp:1234abcd",
        client="claude-cli/2.1.287",
        status=200,
    )
    base.update(kw)
    return Record(**base)


def seed(store: Store) -> list[Record]:
    first = Usage(input_tokens=10, cache_write_1h=57_808, output_tokens=200)
    second = Usage(input_tokens=8, cache_write_1h=374, cache_read=57_835, output_tokens=120)
    rows = [
        (rec(usage=first, cost_usd=0.2334, plan=True), fixture_segments("first_turn", 57_818)),
        (
            rec(ts=NOW - 500, usage=second, cost_usd=0.0098, plan=True),
            fixture_segments("tool_turn", 58_217),
        ),
        (rec(ts=NOW - 400, state=State.BLOCKED, blocked_by="daily", status=402), None),
        (
            rec(
                ts=NOW - 300,
                provider=Provider.OPENAI,
                endpoint=Endpoint.CHAT,
                model="gpt-unknown",
                scope="ci-1",
                session=S2,
                agent=None,
                client="codex/1.0",
                usage=Usage(input_tokens=1000, output_tokens=50),
                cost_usd=0.01,
                cost_estimated=True,
            ),
            None,
        ),
        (
            rec(ts=NOW - 3 * 86400, session=None, model="claude-haiku-4-5", cost_usd=1.5),
            None,
        ),
    ]
    out = []
    for r, segs in rows:
        rid = store.insert(r)
        if segs is not None:
            store.finish(rid, r, segs)
        out.append(r)
    return out


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "ledger.db")
    seed(s)
    yield s
    s.close()


def build_report(store: Store, by: str = "model", since: float | None = None) -> report.Report:
    aggs = store.aggregate(by, since=since)
    flags: dict = {}
    for r in store.records(since=since):
        f = flags.setdefault(getattr(r, by), [False, False])
        f[0] |= r.cost_estimated
        f[1] |= r.plan
    rows = [
        report.ReportRow(a.key, a.requests, a.blocked, a.errors, a.usage, a.cost_usd, *flags[a.key])
        for a in aggs
    ]
    return report.Report(by, since, None, rows, report.total_row(rows))


def test_report_table_has_total_row_and_footnotes(store):
    rep = build_report(store, since=NOW - 86400)
    text = report.render_report(rep)
    lines = text.split("\n")
    assert lines[0].startswith("by model, since ")
    assert lines[1].split() == [
        "model",
        "requests",
        "blocked",
        "prompt",
        "cached",
        "output",
        "cost",
    ]
    sonnet = next(ln for ln in lines if ln.startswith("claude-sonnet-5-5"))
    assert sonnet.split() == ["claude-sonnet-5-5", "2", "1", "116k", "50%", "320", "$0.2432"]
    gpt = next(ln for ln in lines if ln.startswith("gpt-unknown"))
    assert gpt.endswith("$0.0100~")
    total = next(ln for ln in lines if ln.startswith("total"))
    assert total.split()[1:3] == ["3", "1"]
    rule = lines[2]
    assert lines[lines.index(total) - 1] == rule
    assert report.ESTIMATED in text
    assert report.NOTIONAL in text
    widths = {len(ln) for ln in lines[1:6] if ln and not ln.startswith(("~", "sub"))}
    assert len(widths) == 1  # aligned columns, right-aligned cost


def test_report_dict_is_stable(store):
    d = build_report(store).to_dict()
    assert set(d) == {"by", "since", "until", "filters", "rows", "total"}
    assert set(d["total"]) == {
        "key",
        "requests",
        "blocked",
        "errors",
        "prompt_tokens",
        "input_tokens",
        "cache_read",
        "cache_write",
        "output_tokens",
        "cache_hit_rate",
        "cost_usd",
        "cost_estimated",
        "plan",
    }
    assert d["total"]["requests"] == 4
    json.dumps(d)


def test_report_empty_period():
    rep = report.Report("model", NOW, None, [], report.total_row([]))
    assert report.render_report(rep).endswith("no requests in this period")


def test_report_by_session_shortens_ids(store):
    text = report.render_report(build_report(store, by="session"))
    assert "aaaa1111 " in text and S1 not in text
    assert "(none)" in text


def test_sessions_table(store):
    text = report.render_sessions(store.sessions(), now=NOW)
    lines = text.split("\n")
    assert lines[0].split()[:3] == ["session", "scope", "client"]
    assert lines[2].startswith("bbbb2222  ci-1")
    assert "5m00s ago" in lines[2]
    assert lines[3].startswith("aaaa1111  proj   claude-cli/2.1.287")
    assert report.NOTIONAL in text
    assert report.render_sessions([]) == "no sessions in this period"


def test_request_profile(store):
    r = store.records(states=[State.OK], session=S1)[0]
    segs = store.segments(r.id)
    price = Pricing.load().lookup(r.provider, r.model)[0]
    text = report.render_request_profile(r, profile.request_profile(segs, r.usage, price))
    lines = text.split("\n")
    assert lines[0].startswith(f"request #{r.id}  ")
    assert "claude-sonnet-5-5  prompt 57.8k (0% cached)  output 200  $0.2334" in lines[0]
    assert any(ln.startswith("group") and "share" in ln for ln in lines)
    assert any(ln.startswith("tools: built-in") for ln in lines)
    assert report.NOTIONAL in text


def test_session_profile(store):
    items = [(r, store.segments(r.id)) for r in store.records(session=S1)]
    prof = profile.session_profile(items, lambda r: Pricing.load().lookup(r.provider, r.model)[0])
    text = report.render_session_profile(S1, prof)
    assert text.startswith(f"session {S1}  2 requests")
    assert "(1 without a profile)" in text
    assert "unused tool" in text
    if prof.unused_mcp_servers:
        assert "unused MCP server" in text
    if prof.suggestions:
        assert "suggestions:\n  - " in text


def test_diff(store):
    a, b = store.records(states=[State.OK], session=S1)
    d = profile.diff(store.segments(a.id), store.segments(b.id))
    text = report.render_diff(a, b, d)
    assert text.startswith(f"#{a.id} -> #{b.id}: ")
    assert "\n+  tool calls" in text
    assert "group" in text and "delta" in text


def test_cache_timeline(store):
    items = [(r, store.segments(r.id)) for r in store.records(session=S1)]
    events = cachedoctor.analyze(items, lambda r: Pricing.load().lookup(r.provider, r.model)[0])
    text = report.render_cache(S1, events, cachedoctor.summarize(events))
    lines = text.split("\n")
    assert lines[0] == f"session {S1}: 2 requests, hit rate 50%"
    assert lines[2].split()[:7] == ["#", "time", "agent", "prompt", "read", "write", "verdict"]
    assert lines[4].split()[2:6] == ["fp:1234abcd", "57.8k", "0", "57.8k"]
    assert "verdicts: " in text and "extra cost from misses: " in text
    assert report.render_cache(None, [], cachedoctor.summarize([])).endswith("usage")


def test_budget_table(store):
    rules = [
        BudgetRule(name="daily", usd=20.0),
        BudgetRule(name="session", usd=5.0, per=Per.SESSION, window=Window.TOTAL),
        BudgetRule(name="volume", requests=1000, tokens=1_000_000, window=Window.MONTH),
    ]
    statuses = Budget(rules, store, None).status(now=NOW)
    text = report.render_budget(statuses, now=NOW)
    lines = text.split("\n")
    assert lines[0].split() == [
        "budget",
        "window",
        "per",
        "spent",
        "/",
        "limit",
        "remaining",
        "resets",
    ]
    daily = next(ln for ln in lines if ln.startswith("daily"))
    assert "$0.2532 / $20.00" in daily and "$19.75" in daily and " in " in daily
    sess = next(ln for ln in lines if ln.startswith("session"))
    assert "n/a: no session" in sess and "never" in sess
    vol = [ln for ln in lines if "/ 1,000" in ln or "/ 1M" in ln]
    assert len(vol) == 2
    assert report.render_budget([]) == "no budgets configured"


def test_prices_table_and_detail():
    pricing = Pricing.load()
    text = report.render_prices(pricing.models()[:3], "2026-10-09")
    assert text.startswith("USD per million tokens, as of 2026-10-09")
    assert "write 5m" in text
    price = Price(
        1.0, 5.0, 1.25, 2.0, 0.1, long_context_threshold=100_000, long=Price(2, 10, 2.5, 4, 0.2)
    )
    detail = report.render_price("x-1", Provider.ANTHROPIC, "x", "prefix", price)
    assert detail.startswith("x-1 -> anthropic x (prefix)")
    assert "prompts over 100,000 tokens:" in detail
    assert any(ln.split() == ["cache", "read", "0.1"] for ln in detail.splitlines())


def test_run_summary_and_banner():
    usage = Usage(input_tokens=100, cache_read=900, output_tokens=50)
    line = report.run_summary("run-1", 3, usage, 0.5, False, 2, cap=1.0)
    assert line == (
        "skinflint: run-1  3 requests  prompt 1k (90% cached)  output 50  $0.5000 of $1.00"
        "  2 blocked"
    )
    one = report.run_summary("x", 1, Usage(), 0, True, 0)
    assert one == "skinflint: x  1 request  prompt 0  output 0  $0~"
    b = report.banner("0.1.0", "http://h:1", None, 0, "/l.db", ["export A=1"], "careful")
    assert "config  none: no budgets" in b and "warning: careful" in b and "  export A=1" in b
