import json
from pathlib import Path

import pytest

from skinflint.cachedoctor import analyze, extra_cost, min_cacheable, summarize
from skinflint.model import Endpoint, Price, Provider, Record, State, Usage
from skinflint.segments import segment

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
PRICE = Price(input=1.0, output=5.0, cache_write_5m=1.25, cache_write_1h=2.0, cache_read=0.1)
MODEL = "claude-haiku-4-5-20251001"

FIRST = Usage(input_tokens=10, cache_write_1h=57_808)
HIT = Usage(input_tokens=8, cache_write_1h=374, cache_read=57_835)
MISS = Usage(input_tokens=8, cache_write_1h=58_209)


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.request.json").read_text(encoding="utf-8"))


def rec(rid, ts, usage, model=MODEL, provider=Provider.ANTHROPIC, **kw) -> Record:
    endpoint = Endpoint.MESSAGES if provider == Provider.ANTHROPIC else Endpoint.CHAT
    return Record(
        ts=ts,
        provider=provider,
        endpoint=endpoint,
        scope="default",
        model=model,
        state=State.OK,
        id=rid,
        session="s1",
        usage=usage,
        **kw,
    )


def segs(body, provider=Provider.ANTHROPIC):
    endpoint = Endpoint.MESSAGES if provider == Provider.ANTHROPIC else Endpoint.CHAT
    return segment(provider, endpoint, body)


def run(*items):
    return analyze(list(items), lambda r: PRICE)


def two_turns(second_body, usage=MISS, gap=60.0, **kw):
    return run(
        (rec(1, 0.0, FIRST), segs(load("first_turn"))),
        (rec(2, gap, usage, **kw), segs(second_body)),
    )


def test_cold_then_hit():
    events = two_turns(load("tool_turn"), HIT)
    assert [e.verdict for e in events] == ["cold", "hit"]
    assert events[1].predecessor_id == 1
    assert events[1].expected_read == pytest.approx(57_835, rel=0.02)
    assert events[1].extra_cost_usd == 0
    assert events[0].agent.startswith("fp:")


def test_tool_schema_changed():
    body = load("tool_turn")
    body["tools"][13]["description"] += " (changed)"
    e = two_turns(body)[1]
    assert e.verdict == "prefix_changed"
    assert e.cause == "tools changed: Grep schema changed"
    assert e.missed_tokens > 50_000
    assert e.extra_cost_usd == pytest.approx(e.missed_tokens * (2.0 - 0.1) / 1e6)


def test_tool_added():
    body = load("tool_turn")
    body["tools"].append({"name": "mcp__github__create_issue", "input_schema": {}})
    e = two_turns(body)[1]
    assert e.verdict == "prefix_changed"
    assert e.cause == "tools changed: mcp__github__create_issue added"


def test_system_changed_and_timestamp():
    body = load("tool_turn")
    body["system"][2]["text"] = "Current time: 2026-10-09T12:00:00Z\n" + body["system"][2]["text"]
    e = two_turns(body, Usage(input_tokens=8, cache_read=40_000, cache_write_1h=18_209))[1]
    assert e.verdict == "prefix_changed"
    assert e.cause == "system[2] changed"
    assert 10_000 < e.missed_tokens < 25_000


def test_billing_header_change_is_ignored():
    body = load("tool_turn")
    body["system"][0]["text"] = "x-anthropic-billing-header: cc_version=2.1.288; cch=abc;"
    assert two_turns(body, HIT)[1].verdict == "hit"


def test_earlier_message_edited():
    body = load("tool_turn")
    body["messages"][0]["content"][-1]["text"] = "a different prompt"
    e = two_turns(body)[1]
    assert e.verdict == "prefix_changed"
    assert e.cause == "messages[0] edited (user prompt)"


def test_ttl_expired():
    e = two_turns(load("tool_turn"), gap=2 * 3600)[1]
    assert e.verdict == "expired"
    assert "1h" in e.cause and "2h00m" in e.cause
    assert e.extra_cost_usd > 0


def test_five_minute_ttl_expired():
    a, b = load("first_turn"), load("tool_turn")
    for body in (a, b):
        for blk in body["system"][1:]:
            blk["cache_control"] = {"type": "ephemeral"}
        body["messages"][0]["content"][-1].pop("cache_control", None)
    for blk in b["messages"][1]["content"] + b["messages"][2]["content"]:
        if "cache_control" in blk:
            blk["cache_control"] = {"type": "ephemeral"}
    events = run((rec(1, 0, FIRST), segs(a)), (rec(2, 600, MISS), segs(b)))
    assert events[1].verdict == "expired"
    events = run((rec(1, 0, FIRST), segs(a)), (rec(2, 200, MISS), segs(b)))
    assert events[1].verdict == "unexplained"


def test_model_switch():
    e = two_turns(load("tool_turn"), model="claude-sonnet-4-6")[1]
    assert e.verdict == "prefix_changed"
    assert e.cause == f"model changed: {MODEL} -> claude-sonnet-4-6"


def test_api_reported_names_block():
    body = load("tool_turn")
    body["tools"].append({"name": "mcp__github__create_issue", "input_schema": {}})
    e = two_turns(body, cache_miss_reason="tools_changed", cache_missed_tokens=58_000)[1]
    assert e.verdict == "api_reported"
    assert e.cause == "tools_changed: mcp__github__create_issue added"
    assert e.missed_tokens == 58_000
    assert e.api_reason == "tools_changed"


def test_api_reason_without_segments():
    events = run((rec(1, 0, MISS, cache_miss_reason="system_changed", cache_missed_tokens=9), None))
    assert events[0].verdict == "api_reported" and events[0].missed_tokens == 9


def test_side_request_and_subagent_do_not_confuse_predecessor():
    side_body = load("side_request")
    sub_body = load("first_turn")
    sub_body["tools"] = sub_body["tools"][:5]
    events = run(
        (rec(1, 0, FIRST), segs(load("first_turn"))),
        (rec(2, 5, Usage(input_tokens=4604)), segs(side_body)),
        (rec(3, 10, Usage(input_tokens=10, cache_write_5m=20_000)), segs(sub_body)),
        (rec(4, 30, HIT), segs(load("tool_turn"))),
        (rec(5, 40, Usage(input_tokens=93, cache_read=4511)), segs(side_body)),
    )
    by_id = {e.record_id: e for e in events}
    assert by_id[4].verdict == "hit" and by_id[4].predecessor_id == 1
    assert by_id[2].verdict == "cold"
    assert by_id[3].verdict == "cold"
    assert by_id[5].verdict == "hit" and by_id[5].predecessor_id == 2


def test_no_cache_control():
    def strip(body):
        for blk in body["system"]:
            blk.pop("cache_control", None)
        for m in body["messages"]:
            for blk in m["content"]:
                blk.pop("cache_control", None)
        return body

    events = run(
        (rec(1, 0, Usage(input_tokens=57_818)), segs(strip(load("first_turn")))),
        (rec(2, 30, Usage(input_tokens=58_217)), segs(strip(load("tool_turn")))),
    )
    assert [e.verdict for e in events] == ["no_cache_control", "no_cache_control"]
    assert events[1].missed_tokens > 50_000
    assert events[1].extra_cost_usd == pytest.approx(events[1].missed_tokens * 0.9 / 1e6)


def test_below_minimum():
    body = {
        "system": [
            {"type": "text", "text": "short " * 300, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    e = run((rec(1, 0, Usage(input_tokens=400)), segs(body)))[0]
    assert e.verdict == "below_minimum"
    assert "4096-token minimum" in e.cause
    assert (
        run((rec(1, 0, Usage(input_tokens=600), model="claude-opus-5-5"), segs(body)))[0].verdict
        == "cold"
    )


@pytest.mark.parametrize(
    ("model", "minimum"),
    [
        ("claude-opus-5-5", 512),
        ("claude-sonnet-5", 1024),
        ("claude-sonnet-4-20250514", 1024),
        ("claude-opus-4-7", 2048),
        ("claude-3-5-haiku-20241022", 2048),
        ("claude-haiku-4-5-20251001", 4096),
        ("us.anthropic.claude-opus-4-5-20251101-v1:0", 4096),
        ("claude-opus-4-6[1m]", 4096),
        ("something-else", 1024),
    ],
)
def test_min_cacheable(model, minimum):
    assert min_cacheable(Provider.ANTHROPIC, model) == minimum
    assert min_cacheable(Provider.OPENAI, "gpt-5.5") == 1024


def chat(n_extra=0, system="You are a careful coding agent. " * 200):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "task " * 50}]
    for i in range(n_extra):
        msgs.append({"role": "assistant", "content": f"step {i}"})
        msgs.append({"role": "user", "content": "go on"})
    return {"model": "gpt-5.5", "messages": msgs}


def test_openai_hit_and_prefix_change():
    o = Provider.OPENAI
    events = run(
        (rec(1, 0, Usage(input_tokens=1700), model="gpt-5.5", provider=o), segs(chat(), o)),
        (
            rec(2, 20, Usage(input_tokens=30, cache_read=1664), model="gpt-5.5", provider=o),
            segs(chat(1), o),
        ),
        (
            rec(3, 40, Usage(input_tokens=1750), model="gpt-5.5", provider=o),
            segs(chat(2, system="Different system prompt. " * 250), o),
        ),
        (
            rec(4, 60 + 3600, Usage(input_tokens=1760), model="gpt-5.5", provider=o),
            segs(chat(3, system="Different system prompt. " * 250), o),
        ),
    )
    assert [e.verdict for e in events] == ["cold", "hit", "prefix_changed", "expired"]
    # The new system prompt changes the agent fingerprint; the shared conversation still
    # links request 3 to request 2.
    assert events[2].predecessor_id == 2
    assert events[2].cause == "system[0] changed"
    assert "cache lifetime" in events[3].cause


def test_openai_unrelated_conversation_is_cold():
    o = Provider.OPENAI
    other = chat(system="Other agent. " * 400)
    other["messages"][1]["content"] = "unrelated " * 50
    events = run(
        (rec(1, 0, Usage(input_tokens=1700), model="gpt-5.5", provider=o), segs(chat(), o)),
        (rec(2, 20, Usage(input_tokens=1700), model="gpt-5.5", provider=o), segs(other, o)),
    )
    assert events[1].verdict == "cold" and events[1].predecessor_id is None


def test_skips_unsettled_and_summarize():
    blocked = rec(9, 1, Usage())
    blocked.state = State.BLOCKED
    body = load("tool_turn")
    body["tools"][0]["description"] = "x"
    events = run(
        (rec(1, 0, FIRST), segs(load("first_turn"))),
        (blocked, None),
        (rec(2, 30, HIT), segs(load("tool_turn"))),
        (rec(3, 60, MISS), segs(body)),
        (rec(4, 70, MISS), segs(body)),
    )
    assert [e.record_id for e in events] == [1, 2, 3, 4]
    assert events[3].verdict == "partial" or events[3].verdict == "unexplained"
    s = summarize(events)
    assert s.requests == 4
    assert s.counts["hit"] == 1 and s.counts["cold"] == 1 and s.counts["prefix_changed"] == 1
    assert s.extra_cost_usd == pytest.approx(sum(e.extra_cost_usd for e in events))
    assert s.top_causes[0].cause == "prefix_changed: tools changed: Agent schema changed"
    assert s.hit_rate == pytest.approx(57_835 / sum(e.prompt_tokens for e in events))
    assert s.to_dict()["counts"]["hit"] == 1


def test_extra_cost():
    usage = Usage(input_tokens=100, cache_write_5m=1000)
    assert extra_cost(1100, usage, PRICE) == pytest.approx((1000 * 1.15 + 100 * 0.9) / 1e6)
    assert extra_cost(0, usage, PRICE) == 0
    assert extra_cost(10, usage, None) == 0
