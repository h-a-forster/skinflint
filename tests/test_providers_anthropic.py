import json
import random
from pathlib import Path

import pytest
from multidict import CIMultiDict, CIMultiDictProxy

from skinflint.model import Endpoint, Provider, Usage
from skinflint.providers import ADAPTERS, client_name, detect
from skinflint.providers.anthropic import AnthropicAdapter

CC = Path(__file__).parent / "fixtures" / "claude_code"
SESSION = "11111111-2222-3333-4444-555555555555"

adapter = AnthropicAdapter()


def chunkings(n: int):
    yield [n]
    yield [1] * n
    for seed in (1, 2, 3):
        rng = random.Random(seed)
        sizes, total = [], 0
        while total < n:
            sizes.append(rng.randint(1, 211))
            total += sizes[-1]
        yield sizes


def run(raw: bytes, sizes=None):
    tracker = adapter.stream_tracker(Endpoint.MESSAGES, False)
    out, i = [], 0
    for size in sizes or [len(raw)]:
        out.append(tracker.feed(raw[i : i + size]))
        i += size
    out.append(tracker.close())
    return tracker, b"".join(out)


def sse(*events: tuple[str, dict]) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def start(usage: dict, diagnostics=None, model="claude-opus-5-5", mid="msg_1") -> tuple:
    msg = {"id": mid, "model": model, "usage": usage, "diagnostics": diagnostics}
    return ("message_start", {"type": "message_start", "message": msg})


def delta(usage: dict) -> tuple:
    return ("message_delta", {"type": "message_delta", "delta": {}, "usage": usage})


STOP = ("message_stop", {"type": "message_stop"})


# --- endpoints, detection, requests -------------------------------------------------------


def test_endpoint_only_post_messages():
    assert adapter.endpoint("POST", "/v1/messages") is Endpoint.MESSAGES
    assert adapter.endpoint("post", "/v1/messages?beta=true") is Endpoint.MESSAGES
    assert adapter.endpoint("POST", "/v1/messages/count_tokens") is None
    assert adapter.endpoint("POST", "/v1/messages/batches") is None
    assert adapter.endpoint("GET", "/v1/messages") is None
    assert adapter.endpoint("HEAD", "/api/hello") is None


def test_detect_and_registry():
    assert ADAPTERS[Provider.ANTHROPIC].provider is Provider.ANTHROPIC
    assert detect("/whatever", {"Anthropic-Version": "2023-06-01"}) is Provider.ANTHROPIC
    assert detect("/v1/messages", {}) is Provider.ANTHROPIC
    assert detect("/v1/complete", {}) is Provider.ANTHROPIC
    assert detect("/api/hello", {}) is Provider.ANTHROPIC
    assert detect("/v1/chat/completions", {"authorization": "Bearer x"}) is Provider.OPENAI


def test_client_name():
    ua = "claude-cli/2.1.287 (external, claude-desktop, agent-sdk/0.3.293)"
    assert client_name(ua) == "claude-cli/2.1.287"
    assert client_name(None) is None
    assert client_name("  ") is None


def test_parse_request_first_turn():
    body = json.loads((CC / "first_turn.request.json").read_text(encoding="utf-8"))
    info = adapter.parse_request(Endpoint.MESSAGES, body)
    assert info.provider is Provider.ANTHROPIC
    assert info.model == "claude-haiku-4-5-20251001"
    assert info.stream is True
    assert info.max_output_tokens == 32000
    assert info.session_hint == SESSION
    # Real prompt was 10 + 5339 + 52496 = 57,845 tokens; a rough estimate is enough.
    assert 0.7 * 57845 < info.est_prompt_tokens < 1.3 * 57845


def test_parse_request_side_request_not_streaming():
    body = json.loads((CC / "side_request.request.json").read_text(encoding="utf-8"))
    info = adapter.parse_request(Endpoint.MESSAGES, body)
    assert info.stream is False and info.max_output_tokens == 1024


def test_parse_request_counts_images_as_fixed_size():
    image = {"type": "image", "source": {"type": "base64", "data": "A" * 4_000_000}}
    body = {"model": "m", "messages": [{"role": "user", "content": [image]}]}
    assert adapter.parse_request(Endpoint.MESSAGES, body).est_prompt_tokens < 5000


@pytest.mark.parametrize(
    ("name", "real"),
    [("first_turn", 57845), ("tool_turn", 58217), ("side_request", 4604)],
)
def test_parse_request_worst_case_bound_covers_real_prompt(name, real):
    body = json.loads((CC / f"{name}.request.json").read_text(encoding="utf-8"))
    info = adapter.parse_request(Endpoint.MESSAGES, body)
    assert info.est_prompt_tokens < real  # the plain estimate runs ~7% low here
    # The bound leaves room for a tokenizer ~35% hungrier than Haiku 4.5's.
    assert info.max_prompt_tokens > 1.35 * real


def test_parse_request_worst_case_hints():
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 10,
        "speed": "fast",
        "inference_geo": "us",
        "service_tier": "auto",
        "tools": [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 4},
            {"type": "web_search_20250305", "name": "web_search_old", "max_uses": 2},
            {"name": "Bash", "input_schema": {}},
        ],
        "messages": [],
    }
    info = adapter.parse_request(Endpoint.MESSAGES, body)
    assert (info.speed, info.inference_geo, info.service_tier) == ("fast", "us", "auto")
    assert info.web_searches == 6
    body["tools"].append({"type": "web_search_20260209", "name": "uncapped"})
    assert adapter.parse_request(Endpoint.MESSAGES, body).web_searches is None
    assert adapter.parse_request(Endpoint.MESSAGES, {"model": "m"}).web_searches == 0


def test_parse_request_malformed_body():
    info = adapter.parse_request(Endpoint.MESSAGES, {"model": 5, "max_tokens": "x"})
    assert info.model == "" and info.max_output_tokens is None and info.stream is False


# --- session hints --------------------------------------------------------------------------


def test_session_hint_header_is_case_insensitive():
    headers = CIMultiDictProxy(CIMultiDict({"X-Claude-Code-Session-Id": "hdr-session"}))
    assert adapter.session_hint(headers, {}) == "hdr-session"
    assert adapter.session_hint({"x-claude-code-session-id": "lower"}, {}) == "lower"
    assert adapter.session_hint({"X-CLAUDE-CODE-SESSION-ID": "upper"}, {}) == "upper"


def test_session_hint_metadata_json_string():
    body = json.loads((CC / "tool_turn.request.json").read_text(encoding="utf-8"))
    assert adapter.session_hint({}, body) == SESSION


def test_session_hint_legacy_user_id():
    uid = (
        "user_" + "ab" * 32 + "_account_00000000-0000-0000-0000-000000000000"
        "_session_9f2c1e4a-1111-2222-3333-444455556666"
    )
    body = {"metadata": {"user_id": uid}}
    assert adapter.session_hint({}, body) == "9f2c1e4a-1111-2222-3333-444455556666"


@pytest.mark.parametrize(
    "metadata", [None, "x", {}, {"user_id": 7}, {"user_id": "{not json"}, {"user_id": "plain"}]
)
def test_session_hint_absent_or_malformed(metadata):
    assert adapter.session_hint({}, {"metadata": metadata}) is None


# --- rewrite_request ------------------------------------------------------------------------


def test_rewrite_injects_diagnostics():
    body = {"model": "m", "messages": []}
    new = adapter.rewrite_request(Endpoint.MESSAGES, body, previous_message_id="msg_prev")
    assert new == {"model": "m", "messages": [], "diagnostics": {"previous_message_id": "msg_prev"}}
    assert "diagnostics" not in body


def test_rewrite_first_turn_uses_null():
    new = adapter.rewrite_request(Endpoint.MESSAGES, {"model": "m"}, previous_message_id=None)
    assert new == {"model": "m", "diagnostics": {"previous_message_id": None}}


def test_rewrite_skips_when_ellipsis_or_present():
    assert adapter.rewrite_request(Endpoint.MESSAGES, {"model": "m"}) is None
    body = {"model": "m", "diagnostics": None}
    assert adapter.rewrite_request(Endpoint.MESSAGES, body, previous_message_id="x") is None
    assert (
        adapter.rewrite_request(
            Endpoint.MESSAGES, {"stream": True}, inject_stream_usage=True, previous_message_id=...
        )
        is None
    )


# --- non-streaming usage --------------------------------------------------------------------


def test_usage_from_json_side_request():
    data = json.loads((CC / "side_request.response.json").read_text(encoding="utf-8"))
    usage, model, upstream_id = adapter.usage_from_json(Endpoint.MESSAGES, data)
    assert usage == Usage(
        input_tokens=93,
        cache_read=4511,
        output_tokens=56,
        service_tier="standard",
        inference_geo="not_available",
    )
    assert model == "claude-haiku-4-5-20251001" and upstream_id == "msg_fixture"
    assert adapter.cache_miss_from_json(data) == (None, None)


def test_usage_from_json_extras_and_diagnostics():
    data = {
        "id": "msg_x",
        "model": "claude-opus-5-5",
        "usage": {
            "input_tokens": 42,
            "cache_creation_input_tokens": 41850,
            "cache_read_input_tokens": 0,
            "output_tokens": 210,
            "output_tokens_details": {"thinking_tokens": 50},
            "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 1},
            "speed": "fast",
            "service_tier": "priority",
            "inference_geo": "us",
        },
        "diagnostics": {
            "cache_miss_reason": {"type": "system_changed", "cache_missed_input_tokens": 41850}
        },
    }
    u, _, _ = adapter.usage_from_json(Endpoint.MESSAGES, data)
    assert (u.input_tokens, u.cache_write_5m, u.cache_write_1h) == (42, 41850, 0)
    assert (u.output_tokens, u.reasoning_tokens, u.web_search_requests) == (210, 50, 3)
    assert (u.speed, u.service_tier, u.inference_geo) == ("fast", "priority", "us")
    assert adapter.cache_miss_from_json(data) == ("system_changed", 41850)


@pytest.mark.parametrize(
    "diag",
    [None, {"cache_miss_reason": None}, {"cache_miss_reason": "weird"}, "x", []],
)
def test_usage_from_json_diagnostics_inconclusive(diag):
    assert adapter.cache_miss_from_json({"usage": {}, "diagnostics": diag}) == (None, None)


def test_usage_from_json_previous_message_not_found_has_no_tokens():
    diag = {"cache_miss_reason": {"type": "previous_message_not_found"}}
    assert adapter.cache_miss_from_json({"diagnostics": diag}) == (
        "previous_message_not_found",
        None,
    )


@pytest.mark.parametrize("data", [{}, {"usage": None}, {"usage": {"input_tokens": "9"}}, []])
def test_usage_from_json_malformed(data):
    assert adapter.usage_from_json(Endpoint.MESSAGES, data)[0] == Usage()
    assert adapter.cache_miss_from_json(data) == (None, None)


# --- streaming ----------------------------------------------------------------------------


EXPECTED = {
    # (input, 5m, 1h, read, output, reasoning), computed from each fixture's message_delta
    "first_turn": (10, 0, 5339, 52496, 290, 69),
    "tool_turn": (8, 0, 374, 57835, 113, 92),
    "fresh_session": (10, 0, 57808, 0, 42, 35),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
@pytest.mark.parametrize("crlf", [False, True])
def test_stream_fixtures_any_chunking(name, crlf):
    raw = (CC / f"{name}.response.sse").read_bytes()
    if crlf:
        raw = raw.replace(b"\n", b"\r\n")
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(raw, sizes)
        assert forwarded == raw
        u = tracker.usage
        got = (
            u.input_tokens,
            u.cache_write_5m,
            u.cache_write_1h,
            u.cache_read,
            u.output_tokens,
            u.reasoning_tokens,
        )
        assert got == EXPECTED[name]
        assert u.service_tier == "standard"
        assert tracker.model == "claude-haiku-4-5-20251001"
        assert tracker.upstream_id == "msg_fixture"
        assert tracker.finished is True
        assert tracker.error is None
        assert tracker.cache_miss_reason is None


def test_fresh_session_matches_observed_prompt_total():
    tracker, _ = run((CC / "fresh_session.response.sse").read_bytes())
    assert tracker.usage.prompt_tokens == 57818


def test_stream_truncated_keeps_message_start_usage():
    raw = (CC / "first_turn.response.sse").read_bytes()
    cut = raw.index(b"event: message_delta")
    tracker, forwarded = run(raw[:cut])
    assert forwarded == raw[:cut]
    assert tracker.finished is False
    u = tracker.usage
    assert (u.input_tokens, u.cache_write_1h, u.cache_read, u.output_tokens) == (10, 5339, 52496, 3)


def test_stream_delta_cumulative_growth_goes_to_5m():
    raw = sse(
        start(
            {
                "input_tokens": 2679,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 100,
                },
                "output_tokens": 1,
            }
        ),
        delta({"output_tokens": 40}),
        delta(
            {
                "input_tokens": 10682,
                "cache_creation_input_tokens": 150,
                "cache_read_input_tokens": None,
                "output_tokens": 510,
                "server_tool_use": {"web_search_requests": 1},
            }
        ),
        STOP,
    )
    tracker, _ = run(raw)
    u = tracker.usage
    assert (u.input_tokens, u.cache_write_5m, u.cache_write_1h, u.cache_read) == (10682, 50, 100, 0)
    assert (u.output_tokens, u.web_search_requests) == (510, 1)


def test_stream_cache_creation_without_breakdown_is_5m():
    tracker, _ = run(sse(start({"input_tokens": 1, "cache_creation_input_tokens": 77}), STOP))
    assert (tracker.usage.cache_write_5m, tracker.usage.cache_write_1h) == (77, 0)


def test_stream_diagnostics_on_message_start():
    diag = {"cache_miss_reason": {"type": "tools_changed", "cache_missed_input_tokens": 1234}}
    tracker, _ = run(sse(start({"input_tokens": 5}, diagnostics=diag), STOP))
    assert tracker.cache_miss_reason == "tools_changed"
    assert tracker.cache_missed_tokens == 1234


def test_stream_error_event_after_200():
    raw = (CC / "tool_turn.response.sse").read_bytes()
    cut = raw.index(b"event: message_delta")
    err = (
        b'event: error\ndata: {"type": "error", "error": '
        b'{"type": "overloaded_error", "message": "Overloaded"}}\n\n'
    )
    tracker, forwarded = run(raw[:cut] + err)
    assert forwarded == raw[:cut] + err
    assert tracker.error == "overloaded_error: Overloaded"
    assert tracker.finished is False
    assert tracker.usage.cache_read == 57835


def test_stream_without_event_names_uses_data_type():
    raw = b"".join(
        b"data: " + json.dumps(d).encode() + b"\n\n"
        for _, d in (
            start({"input_tokens": 3, "output_tokens": 1}),
            delta({"output_tokens": 9}),
            STOP,
        )
    )
    tracker, _ = run(raw)
    assert (tracker.usage.input_tokens, tracker.usage.output_tokens) == (3, 9)
    assert tracker.finished


def test_stream_final_event_without_trailing_blank_line():
    raw = sse(start({"input_tokens": 3}))
    raw += b'event: message_stop\ndata: {"type":"message_stop"}'
    tracker, forwarded = run(raw, [7] * (len(raw) // 7 + 1))
    assert forwarded == raw and tracker.finished


@pytest.mark.parametrize(
    "garbage",
    [
        b"\xff\xfe\x00garbage\n\n",
        b"event: message_start\ndata: {not json\n\n",
        b"event: message_start\ndata: []\n\n",
        b'event: message_start\ndata: {"message": "str"}\n\n',
        b'event: message_start\ndata: {"message": {"usage": {"input_tokens": -4}}}\n\n',
        b'event: message_delta\ndata: {"usage": {"output_tokens": true}}\n\n',
        b'event: message_delta\ndata: {"usage": {"output_tokens_details": 5}}\n\n',
        b'event: error\ndata: {"error": null}\n\n',
        b"event: unknown_future_event\ndata: {}\n\n",
        b": comment only\n\n",
        pytest.param(b"data: " + b"[" * 100_000 + b"\n\n", id="deep-nesting"),
    ],
)
def test_stream_never_raises_on_garbage(garbage):
    raw = garbage + (CC / "tool_turn.response.sse").read_bytes()
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(raw, sizes)
        assert forwarded == raw
        assert tracker.usage.output_tokens == 113
        assert tracker.finished


# --- refusals and headers -----------------------------------------------------------------


def test_error_body_is_billing_error_402():
    status, headers, body = adapter.error_body("skinflint: budget 'daily' reached")
    assert status == 402
    assert headers["content-type"] == "application/json"
    assert headers["x-should-retry"] == "false"
    assert "retry-after" not in {k.lower() for k in headers}
    data = json.loads(body)
    assert data["type"] == "error"
    assert data["error"] == {
        "type": "billing_error",
        "message": "skinflint: budget 'daily' reached",
    }
    assert data["request_id"].startswith("skinflint_")
    assert headers["request-id"] == data["request_id"]


def test_ratelimit_headers_subset_lowercased():
    headers = CIMultiDictProxy(
        CIMultiDict(
            {
                "Anthropic-Ratelimit-Unified-5h-Utilization": "0.42",
                "anthropic-ratelimit-unified-status": "allowed",
                "request-id": "req_1",
                "content-type": "text/event-stream",
            }
        )
    )
    assert adapter.ratelimit_headers(headers) == {
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-status": "allowed",
    }
