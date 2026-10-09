import json
import random
from pathlib import Path

import pytest
from multidict import CIMultiDict, CIMultiDictProxy

from skinflint.model import Endpoint, Provider, Usage
from skinflint.providers import ADAPTERS
from skinflint.providers.openai import OpenAIAdapter, openai_usage

FX = Path(__file__).parent / "fixtures" / "openai"

adapter = OpenAIAdapter()


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


def run(endpoint: Endpoint, raw: bytes, sizes=None, injected=False):
    tracker = adapter.stream_tracker(endpoint, injected)
    out, i = [], 0
    for size in sizes or [len(raw)]:
        out.append(tracker.feed(raw[i : i + size]))
        i += size
    out.append(tracker.close())
    return tracker, b"".join(out)


def without_usage_chunk(raw: bytes, sep: bytes = b"\n\n") -> bytes:
    blocks = raw.split(sep)
    return sep.join(b for b in blocks if b'"choices":[]' not in b)


# --- endpoints and requests -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/chat/completions", Endpoint.CHAT),
        ("/chat/completions", Endpoint.CHAT),
        ("/v1/responses", Endpoint.RESPONSES),
        ("/responses", Endpoint.RESPONSES),
        ("/backend-api/codex/responses", Endpoint.RESPONSES),
        ("/v1/responses?x=1", Endpoint.RESPONSES),
        ("/v1/responses/resp_1/cancel", None),
        ("/v1/responses/input_tokens", None),
        ("/v1/embeddings", None),
        ("/v1/models", None),
    ],
)
def test_endpoint(path, expected):
    assert adapter.endpoint("POST", path) is expected


def test_endpoint_requires_post():
    assert adapter.endpoint("GET", "/v1/responses") is None
    assert ADAPTERS[Provider.OPENAI].provider is Provider.OPENAI


def test_parse_request_chat():
    body = {
        "model": "gpt-5.5",
        "stream": True,
        "max_completion_tokens": 900,
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "x" * 4000}],
    }
    info = adapter.parse_request(Endpoint.CHAT, body)
    assert (info.model, info.stream, info.max_output_tokens) == ("gpt-5.5", True, 900)
    assert 900 < info.est_prompt_tokens < 1100
    assert info.session_hint is None


def test_parse_request_chat_legacy_max_tokens():
    info = adapter.parse_request(Endpoint.CHAT, {"model": "m", "max_tokens": 77, "messages": []})
    assert info.max_output_tokens == 77 and info.stream is False


def test_parse_request_responses():
    body = {
        "model": "gpt-5.6-codex",
        "instructions": "i" * 400,
        "input": "hi",
        "max_output_tokens": 64,
    }
    info = adapter.parse_request(Endpoint.RESPONSES, body)
    assert (info.model, info.max_output_tokens) == ("gpt-5.6-codex", 64)
    assert info.est_prompt_tokens > 90


def test_parse_request_data_url_image_is_fixed_size():
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 3_000_000}}
    body = {"model": "m", "messages": [{"role": "user", "content": [img]}]}
    assert adapter.parse_request(Endpoint.CHAT, body).est_prompt_tokens < 5000


def test_session_hint_headers_only():
    headers = CIMultiDictProxy(CIMultiDict({"Session_Id": "codex-1"}))
    assert adapter.session_hint(headers, {}) == "codex-1"
    assert adapter.session_hint({"X-Session-Id": "s2"}, {}) == "s2"
    assert adapter.session_hint({}, {"prompt_cache_key": "k", "user": "u"}) is None


# --- rewrite_request ------------------------------------------------------------------------


def test_rewrite_injects_include_usage_for_chat_streams():
    body = {"model": "m", "stream": True, "messages": []}
    new = adapter.rewrite_request(Endpoint.CHAT, body, inject_stream_usage=True)
    assert new == {**body, "stream_options": {"include_usage": True}}
    assert "stream_options" not in body


def test_rewrite_keeps_other_stream_options():
    body = {"stream": True, "stream_options": {"include_obfuscation": False}}
    new = adapter.rewrite_request(Endpoint.CHAT, body, inject_stream_usage=True)
    assert new["stream_options"] == {"include_obfuscation": False, "include_usage": True}
    assert body["stream_options"] == {"include_obfuscation": False}


@pytest.mark.parametrize(
    ("endpoint", "body", "inject"),
    [
        (Endpoint.CHAT, {"stream": True}, False),
        (Endpoint.CHAT, {"stream": False}, True),
        (Endpoint.CHAT, {}, True),
        (Endpoint.CHAT, {"stream": True, "stream_options": {"include_usage": True}}, True),
        (Endpoint.CHAT, {"stream": True, "stream_options": "bad"}, True),
        (Endpoint.RESPONSES, {"stream": True}, True),
    ],
)
def test_rewrite_no_change(endpoint, body, inject):
    assert adapter.rewrite_request(endpoint, body, inject_stream_usage=inject) is None


def test_rewrite_ignores_previous_message_id():
    body = {"stream": False}
    assert adapter.rewrite_request(Endpoint.CHAT, body, previous_message_id="msg_1") is None


def test_rewrite_explicit_false_is_turned_on():
    body = {"stream": True, "stream_options": {"include_usage": False}}
    new = adapter.rewrite_request(Endpoint.CHAT, body, inject_stream_usage=True)
    assert new["stream_options"] == {"include_usage": True}


# --- usage normalisation --------------------------------------------------------------------


def test_usage_from_json_chat():
    data = json.loads((FX / "chat_nonstream.json").read_text(encoding="utf-8"))
    usage, model, upstream_id = adapter.usage_from_json(Endpoint.CHAT, data)
    assert usage == Usage(
        input_tokens=904,
        cache_read=4096,
        output_tokens=120,
        reasoning_tokens=64,
        service_tier="flex",
    )
    assert (model, upstream_id) == ("gpt-5.5-2026-08-01", "chatcmpl-fx002")
    assert adapter.cache_miss_from_json(data) == (None, None)


def test_usage_from_json_responses():
    data = json.loads((FX / "responses_nonstream.json").read_text(encoding="utf-8"))
    usage, model, upstream_id = adapter.usage_from_json(Endpoint.RESPONSES, data)
    assert usage == Usage(
        input_tokens=172, cache_read=128, output_tokens=20, service_tier="default"
    )
    assert (model, upstream_id) == ("gpt-5.4-mini", "resp_fx006")


def test_usage_cached_and_written_subtracted_never_negative():
    u = openai_usage(
        {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 90, "cache_write_tokens": 50},
        }
    )
    assert (u.input_tokens, u.cache_read, u.cache_write_5m, u.cache_write_1h) == (0, 90, 50, 0)


@pytest.mark.parametrize(
    "usage",
    [None, "x", {}, {"prompt_tokens": "9"}, {"prompt_tokens_details": 3, "prompt_tokens": 4}],
)
def test_usage_malformed(usage):
    assert openai_usage(usage).cache_read == 0


# --- chat streams ---------------------------------------------------------------------------


@pytest.mark.parametrize("crlf", [False, True])
def test_chat_stream_injected_usage_chunk_is_stripped(crlf):
    raw = (FX / "chat_stream_include_usage.sse").read_bytes()
    sep = b"\n\n"
    if crlf:
        raw, sep = raw.replace(b"\n", b"\r\n"), b"\r\n\r\n"
    expected = without_usage_chunk(raw, sep)
    assert len(expected) < len(raw)
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(Endpoint.CHAT, raw, sizes, injected=True)
        assert forwarded == expected
        u = tracker.usage
        assert (u.input_tokens, u.cache_write_5m, u.cache_read) == (308, 256, 1536)
        assert (u.output_tokens, u.reasoning_tokens, u.service_tier) == (57, 32, "default")
        assert tracker.model == "gpt-5.5-2026-08-01"
        assert tracker.upstream_id == "chatcmpl-fx001"
        assert tracker.finished and tracker.error is None


def test_chat_stream_client_requested_usage_passes_through():
    raw = (FX / "chat_stream_include_usage.sse").read_bytes()
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(Endpoint.CHAT, raw, sizes, injected=False)
        assert forwarded == raw
        assert tracker.usage.cache_read == 1536


def test_chat_stream_injected_holds_partial_event_until_complete():
    raw = (FX / "chat_stream_include_usage.sse").read_bytes()
    tracker = adapter.stream_tracker(Endpoint.CHAT, True)
    first_end = raw.index(b"\n\n") + 2
    assert tracker.feed(raw[: first_end - 1]) == b""
    assert tracker.feed(raw[first_end - 1 : first_end]) == raw[:first_end]


def test_chat_stream_usage_on_last_choice_chunk_is_not_stripped():
    chunk = {
        "id": "c",
        "model": "m",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    chunk["usage"] = {"prompt_tokens": 10, "completion_tokens": 2}
    raw = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
    tracker, forwarded = run(Endpoint.CHAT, raw, injected=True)
    assert forwarded == raw
    assert tracker.usage.input_tokens == 10


def test_chat_stream_without_usage_or_done():
    raw = (FX / "chat_stream_include_usage.sse").read_bytes()
    cut = raw.rindex(b"data: ", 0, raw.index(b'"choices":[]'))
    tracker, forwarded = run(Endpoint.CHAT, raw[:cut], injected=True)
    assert forwarded == raw[:cut]
    assert tracker.usage.is_empty() and tracker.finished is False


def test_chat_stream_error_chunk():
    raw = (FX / "chat_stream_error.sse").read_bytes()
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(Endpoint.CHAT, raw, sizes, injected=True)
        assert forwarded == raw
        assert (
            tracker.error == "server_error: The server had an error while processing your request."
        )
        assert tracker.finished is False


def test_chat_stream_close_flushes_unterminated_event():
    raw = b'data: {"id":"c","model":"m","choices":[]}\n\ndata: {"id":"c","choices":[{"index":0}]}'
    tracker, forwarded = run(Endpoint.CHAT, raw, [5] * 30, injected=True)
    assert forwarded == raw


@pytest.mark.parametrize(
    "garbage",
    [
        b"\xff\xfe garbage\n\n",
        b"data: {nope\n\n",
        b'data: {"usage": 5}\n\n',
        b'data: {"choices": [], "usage": "x"}\n\n',
        b": keep-alive\n\n",
        b"event: weird\ndata: 1\n\n",
        pytest.param(b"data: " + b"[" * 100_000 + b"\n\n", id="deep-nesting"),
    ],
)
def test_chat_stream_never_raises_on_garbage(garbage):
    raw = garbage + (FX / "chat_stream_include_usage.sse").read_bytes()
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(Endpoint.CHAT, raw, sizes, injected=True)
        assert forwarded == without_usage_chunk(raw)
        assert tracker.usage.output_tokens == 57 and tracker.finished


# --- responses streams ----------------------------------------------------------------------


@pytest.mark.parametrize("crlf", [False, True])
def test_responses_stream_completed(crlf):
    raw = (FX / "responses_stream.sse").read_bytes()
    if crlf:
        raw = raw.replace(b"\n", b"\r\n")
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(Endpoint.RESPONSES, raw, sizes, injected=True)
        assert forwarded == raw
        u = tracker.usage
        assert (u.input_tokens, u.cache_write_5m, u.cache_read) == (1000, 3000, 8000)
        assert (u.output_tokens, u.reasoning_tokens, u.service_tier) == (400, 250, "priority")
        assert (tracker.model, tracker.upstream_id) == ("gpt-5.6-codex", "resp_fx003")
        assert tracker.finished and tracker.error is None


def test_responses_stream_truncated_not_finished():
    raw = (FX / "responses_stream.sse").read_bytes()
    cut = raw.index(b"event: response.completed")
    tracker, _ = run(Endpoint.RESPONSES, raw[:cut])
    assert tracker.finished is False
    assert tracker.model == "gpt-5.6-codex" and tracker.usage.is_empty()


def test_responses_stream_error_event():
    tracker, _ = run(Endpoint.RESPONSES, (FX / "responses_stream_error.sse").read_bytes())
    assert tracker.error == "server_error: Something went wrong"
    assert tracker.finished is False


def test_responses_stream_failed_has_usage_and_error():
    tracker, _ = run(Endpoint.RESPONSES, (FX / "responses_stream_failed.sse").read_bytes())
    assert tracker.finished is True
    assert tracker.error == "server_error: The model failed"
    assert (tracker.usage.input_tokens, tracker.usage.output_tokens) == (900, 3)


def test_responses_stream_incomplete_is_terminal():
    resp = {"id": "r", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 1}}
    data = json.dumps({"type": "response.incomplete", "response": resp})
    tracker, _ = run(Endpoint.RESPONSES, f"data: {data}\n\n".encode())
    assert tracker.finished and tracker.error is None and tracker.usage.input_tokens == 5


@pytest.mark.parametrize(
    "garbage",
    [
        b"\xff\n\n",
        b"event: response.completed\ndata: {bad\n\n",
        b'event: response.completed\ndata: {"response": 3}\n\n',
        b'event: error\ndata: "x"\n\n',
        b"event: response.unknown_future\ndata: {}\n\n",
    ],
)
def test_responses_stream_never_raises_on_garbage(garbage):
    raw = garbage + (FX / "responses_stream.sse").read_bytes()
    for sizes in chunkings(len(raw)):
        tracker, forwarded = run(Endpoint.RESPONSES, raw, sizes)
        assert forwarded == raw
        assert tracker.usage.output_tokens == 400


# --- refusals and headers -----------------------------------------------------------------


def test_error_body_is_insufficient_quota_429():
    status, headers, body = adapter.error_body("skinflint: budget 'daily' reached")
    assert status == 429
    assert headers == {"content-type": "application/json", "x-should-retry": "false"}
    assert json.loads(body) == {
        "error": {
            "message": "skinflint: budget 'daily' reached",
            "type": "insufficient_quota",
            "param": None,
            "code": "skinflint_budget_exceeded",
        }
    }


def test_ratelimit_headers():
    headers = CIMultiDictProxy(
        CIMultiDict({"X-RateLimit-Remaining-Tokens": "100", "x-request-id": "r", "Date": "d"})
    )
    assert adapter.ratelimit_headers(headers) == {"x-ratelimit-remaining-tokens": "100"}


def test_parse_request_worst_case_hints():
    adapter = OpenAIAdapter()
    body = {
        "model": "gpt-5.5",
        "input": "hi",
        "service_tier": "priority",
        "previous_response_id": "resp_abc",
        "tools": [{"type": "web_search"}],
    }
    info = adapter.parse_request(Endpoint.RESPONSES, body)
    assert info.service_tier == "priority" and info.previous_response_id == "resp_abc"
    assert info.web_searches is None and not info.server_context
    assert info.max_prompt_tokens >= info.est_prompt_tokens
    info = adapter.parse_request(Endpoint.RESPONSES, {"model": "m", "conversation": "conv_1"})
    assert info.server_context and info.previous_response_id is None
