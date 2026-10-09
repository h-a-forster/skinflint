import json
from pathlib import Path

import pytest

from skinflint.model import Endpoint, Price, Provider, Record, State, Usage
from skinflint.profile import diff, request_profile, segment_costs, session_profile
from skinflint.segments import calibrate, segment

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
PRICE = Price(input=1.0, output=5.0, cache_write_5m=1.25, cache_write_1h=2.0, cache_read=0.1)
MCP = "tools: mcp claude_ai_Claude_Docs"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.request.json").read_text(encoding="utf-8"))


def segs(name: str):
    return segment(Provider.ANTHROPIC, Endpoint.MESSAGES, load(name))


def record(ts: float, usage: Usage, rid: int) -> Record:
    return Record(
        ts=ts,
        provider=Provider.ANTHROPIC,
        endpoint=Endpoint.MESSAGES,
        scope="default",
        model="claude-haiku-4-5",
        state=State.OK,
        id=rid,
        session="s1",
        usage=usage,
    )


FIRST = Usage(input_tokens=10, cache_write_1h=57_808)
TOOL = Usage(input_tokens=8, cache_write_1h=374, cache_read=57_835)


def test_request_profile_groups_and_cost():
    s = segs("first_turn")
    p = request_profile(s, FIRST, PRICE)
    assert p.total_tokens == 57_818
    assert p.cost_usd == pytest.approx((57_808 * 2.0 + 10 * 1.0) / 1e6)
    groups = {g.group: g for g in p.groups}
    assert groups["tools: built-in"].segments == 34
    assert groups[MCP].segments == 8
    assert groups["system prompt"].segments == 3
    assert "context: skills" in groups and "instruction files" in groups
    assert sum(g.share for g in p.groups) == pytest.approx(1.0)
    assert sum(g.tokens for g in p.groups) == 57_818
    assert p.groups[0].group == "tools: built-in"  # biggest bucket
    assert p.to_dict()["groups"][0]["group"] == "tools: built-in"


def test_cost_split_follows_prefix_order():
    tokens = [100, 100, 100]
    usage = Usage(cache_read=150, cache_write_5m=100, input_tokens=50)
    costs = segment_costs(tokens, usage, PRICE)
    assert costs[0] == pytest.approx(100 * 0.1 / 1e6)
    assert costs[1] == pytest.approx((50 * 0.1 + 50 * 1.25) / 1e6)
    assert costs[2] == pytest.approx((50 * 1.25 + 50 * 1.0) / 1e6)
    assert segment_costs(tokens, usage, None) == [0.0, 0.0, 0.0]


def test_long_context_rates():
    long = Price(input=2.0, output=10, cache_write_5m=2.5, cache_write_1h=4, cache_read=0.2)
    price = Price(1.0, 5.0, 1.25, 2.0, 0.1, long_context_threshold=100, long=long)
    assert segment_costs([200], Usage(input_tokens=200), price) == [pytest.approx(400 / 1e6)]
    assert segment_costs([50], Usage(input_tokens=50), price) == [pytest.approx(50 / 1e6)]


def session_items():
    first = segs("first_turn")
    tool = segs("tool_turn")
    side = segs("side_request")
    return [
        (record(0, FIRST, 1), first),
        (record(5, Usage(input_tokens=93, cache_read=4511), 2), side),
        (record(30, TOOL, 3), tool),
        (record(40, TOOL, 4), None),
    ]


def test_session_profile_unused_mcp_server():
    p = session_profile(session_items(), lambda r: PRICE)
    assert p.requests == 3 and p.skipped == 1
    assert p.total_tokens == 57_818 + 4_604 + 58_217
    names = {u.name for u in p.unused_tools}
    assert "Read" not in names and "Glob" not in names and "Bash" in names
    assert len(p.unused_mcp_servers) == 1
    srv = p.unused_mcp_servers[0]
    assert srv.server == "claude_ai_Claude_Docs" and srv.tools == 8 and srv.requests == 2
    assert srv.tokens_per_request > 1000
    assert srv.cost_usd > 0
    msg = next(s for s in p.suggestions if "claude_ai_Claude_Docs" in s)
    assert msg.startswith("MCP server 'claude_ai_Claude_Docs' adds ")
    assert "to each of 2 requests" in msg and msg.endswith("and was never called.")
    assert any("built-in tools were never called" in s for s in p.suggestions)
    results = {r.tool for r in p.largest_tool_results}
    assert results == {"Read", "Glob"}
    assert p.instruction_files[0].label == "instructions"
    assert p.instruction_files[0].requests == 2
    d = p.to_dict()
    assert d["unused_mcp_servers"][0]["server"] == "claude_ai_Claude_Docs"


def test_session_profile_called_mcp_tool_keeps_server():
    body = load("tool_turn")
    body["messages"][1]["content"][1]["name"] = "mcp__claude_ai_Claude_Docs__read"
    s = segment(Provider.ANTHROPIC, Endpoint.MESSAGES, body)
    p = session_profile([(record(0, TOOL, 1), s)], lambda r: PRICE)
    assert p.unused_mcp_servers == []
    assert "mcp__claude_ai_Claude_Docs__read" not in {u.name for u in p.unused_tools}


def test_diff_first_to_tool_turn():
    a, b = segs("first_turn"), segs("tool_turn")
    calibrate(a, 57_818)
    calibrate(b, 58_217)
    d = diff(a, b)
    assert [(c.section, c.label) for c in d.added] == [
        ("messages", "thinking"),
        ("messages", "Read"),
        ("messages", "Glob"),
        ("messages", "Read"),
        ("messages", "Glob"),
    ]
    assert d.removed == [] and d.changed == []
    assert d.delta == 58_217 - 57_818
    assert {g.group for g in d.groups} >= {"tool calls", "tool results: Read", "thinking"}


def test_diff_changed_and_removed():
    a = segs("first_turn")
    body = load("first_turn")
    body["system"][2]["text"] += " Today is 2026-10-09 12:00:01."
    del body["tools"][3]
    b = segment(Provider.ANTHROPIC, Endpoint.MESSAGES, body)
    d = diff(a, b)
    assert [c.label for c in d.changed] == ["system[2]"]
    assert d.changed[0].delta > 0
    assert [c.label for c in d.removed] == ["ArtifactData"]
    assert d.to_dict()["removed"][0]["delta"] < 0
