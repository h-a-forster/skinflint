"""End-to-end proxy tests against the fake upstream in tests/fakes.py, with a real Store."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from fakes import (
    CHAT_USAGE,
    FakeUpstream,
    Reply,
    anthropic_events,
    anthropic_json,
    chat_events,
    error_reply,
    responses_events,
    split,
    stream,
    wait_for,
)

from skinflint import proxy as proxy_mod
from skinflint.budget import Budget
from skinflint.config import Config
from skinflint.model import Action, BudgetRule, Endpoint, Per, Provider, Record, State, Window
from skinflint.pricing import Pricing
from skinflint.proxy import PROXY, Proxy, create_app, embedded
from skinflint.store import Store

try:
    from compression import zstd  # type: ignore[import-not-found]
except ImportError:
    from backports import zstd  # type: ignore[no-redef]

FIXTURES = Path(__file__).parent / "fixtures"
CC = FIXTURES / "claude_code"
OA = FIXTURES / "openai"
MODEL = "claude-haiku-4-5"
SESSION = "11111111-2222-3333-4444-555555555555"
API_KEY = "sk-ant-api03-SECRETSECRETSECRET"
BEARER = "Bearer sk-ant-oat01-OAUTHSECRETOAUTHSECRET"
ANTHROPIC_HEADERS = {"anthropic-version": "2023-06-01", "content-type": "application/json"}


def msg_body(stream_: bool = True, **extra) -> dict:
    return {
        "model": MODEL,
        "max_tokens": 1000,
        "system": [{"type": "text", "text": "You are a helpful agent."}],
        "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream_,
        **extra,
    }


def chat_body(stream_: bool = True, **extra) -> dict:
    return {
        "model": "gpt-5.4",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream_,
        **extra,
    }


@dataclass
class Harness:
    client: aiohttp.ClientSession
    store: Store
    proxy: Proxy
    fake: FakeUpstream
    cfg: Config
    finished: Counter
    runner: web.AppRunner | None = None

    async def post(self, path: str, body: dict | bytes, headers: dict | None = None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        return await self.client.post(path, data=data, headers=headers or {})

    async def messages(self, body: dict | None = None, headers: dict | None = None, path=""):
        return await self.post(
            path + "/v1/messages", body or msg_body(), {**ANTHROPIC_HEADERS, **(headers or {})}
        )

    async def close(self) -> None:
        """Stop like production: server, then app cleanup (drains the ledger), then store."""
        if self.runner is not None:
            await self.client.close()
            await self.runner.cleanup()
            self.store.close()
            self.runner = None

    async def records(self) -> list[Record]:
        """All ledger rows once no request is pending and the write queue has drained."""
        await wait_for(lambda: all(r.state is not State.PENDING for r in self.store.records()))
        await self.proxy.run_db(lambda: None)
        return self.store.records()


@pytest.fixture
async def fake(aiohttp_server):
    up = FakeUpstream()
    server = await aiohttp_server(up.app())
    up.url = str(server.make_url("")).rstrip("/")
    return up


@pytest.fixture
async def make(tmp_path, fake):
    """Run the proxy like production (AppRunner, handlers not cancelled on disconnect)."""
    made: list[tuple[Harness, web.AppRunner]] = []

    async def build(
        handler_cancellation: bool = False, shutdown_timeout: float = 5.0, **kw
    ) -> Harness:
        cfg = Config(
            db_path=tmp_path / "skinflint.db",
            upstreams={Provider.ANTHROPIC: fake.url, Provider.OPENAI: fake.url},
            **kw,
        )
        store = Store(cfg.db_path)
        finished: Counter = Counter()
        original = store.finish

        def counting_finish(record_id, *args, **kwargs):
            finished[record_id] += 1
            return original(record_id, *args, **kwargs)

        store.finish = counting_finish
        pricing = Pricing.load(cfg.prices, unknown_model=cfg.unknown_model)
        budget = Budget(cfg.budgets, store, pricing, reserve=cfg.reserve)
        app = create_app(cfg, store, pricing, budget)
        runner = web.AppRunner(
            app, handler_cancellation=handler_cancellation, shutdown_timeout=shutdown_timeout
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        client = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}")
        h = Harness(client, store, app[PROXY], fake, cfg, finished)
        h.runner = runner
        made.append(h)
        return h

    yield build
    for h in made:
        await h.close()
        assert all(n == 1 for n in h.finished.values()), f"settled twice: {h.finished}"


@pytest.fixture
async def h(make) -> Harness:
    return await make()


# -- 1. transparency -----------------------------------------------------------------------


async def test_anthropic_stream_bytes_identical_with_split_chunks(h):
    data = (CC / "first_turn.response.sse").read_bytes()
    h.fake.reply(Reply(chunks=split(data, 7), content_type="text/event-stream"))
    body = json.loads((CC / "first_turn.request.json").read_text(encoding="utf-8"))
    resp = await h.messages(body)
    assert resp.status == 200
    assert await resp.read() == data
    assert resp.headers["content-type"] == "text/event-stream"
    (rec,) = await h.records()
    assert rec.state is State.OK


async def test_responses_stream_bytes_identical(h):
    data = (OA / "responses_stream.sse").read_bytes()
    h.fake.reply(Reply(chunks=split(data, 13), content_type="text/event-stream"))
    resp = await h.post("/v1/responses", {"model": "gpt-5.6-codex", "input": "hi", "stream": True})
    assert await resp.read() == data
    (rec,) = await h.records()
    assert rec.state is State.OK
    assert rec.usage.cache_read == 8000
    assert rec.usage.cache_write_5m == 3000
    assert rec.usage.input_tokens == 1000
    assert rec.upstream_id == "resp_fx003"


async def test_json_body_status_and_headers_preserved(h):
    data = (CC / "side_request.response.json").read_bytes()
    h.fake.reply(
        Reply(
            status=201,
            body=data,
            headers={
                "request-id": "req_123",
                "anthropic-ratelimit-unified-5h-utilization": "0.42",
                "anthropic-ratelimit-unified-status": "allowed",
                "keep-alive": "timeout=5",
                "x-custom": "yes",
            },
        )
    )
    resp = await h.messages(msg_body(stream_=False))
    assert resp.status == 201
    assert await resp.read() == data
    assert resp.headers["request-id"] == "req_123"
    assert resp.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.42"
    assert resp.headers["x-custom"] == "yes"
    assert "keep-alive" not in resp.headers
    (rec,) = await h.records()
    assert rec.state is State.OK and rec.status == 201
    ts, saved = h.store.ratelimit(Provider.ANTHROPIC)
    assert saved["anthropic-ratelimit-unified-5h-utilization"] == "0.42"
    assert saved["anthropic-ratelimit-unified-status"] == "allowed"


async def test_request_headers_query_and_auth_forwarding(h, caplog):
    caplog.set_level(logging.DEBUG)
    beta = "oauth-2025-04-20,interleaved-thinking-2025-05-14"
    resp = await h.post(
        "/v1/messages?beta=true",
        msg_body(),
        {
            **ANTHROPIC_HEADERS,
            "x-api-key": API_KEY,
            "authorization": BEARER,
            "anthropic-beta": beta,
            "x-skinflint-scope": "proj",
            "x-skinflint-session": "s-override",
            "proxy-authorization": "Basic Zm9vOmJhcg==",
            "te": "trailers",
            "keep-alive": "timeout=5",
            "x-stainless-lang": "js",
        },
    )
    assert resp.status == 200
    await resp.read()
    (got,) = h.fake.requests
    assert got.query == "beta=true"
    assert got.headers["x-api-key"] == API_KEY
    assert got.headers["authorization"] == BEARER
    assert got.headers["anthropic-beta"] == beta
    assert got.headers["x-stainless-lang"] == "js"
    for name in ("x-skinflint-scope", "x-skinflint-session", "proxy-authorization", "te"):
        assert name not in got.headers
    assert "keep-alive" not in got.headers
    (rec,) = await h.records()
    assert rec.scope == "proj" and rec.session == "s-override" and rec.plan
    await h.close()
    blobs = b"".join(p.read_bytes() for p in h.cfg.db_path.parent.glob("skinflint.db*"))
    for secret in ("SECRETSECRET", "OAUTHSECRET", "Zm9vOmJhcg"):
        assert secret.encode() not in blobs
        assert secret not in caplog.text


async def test_raw_path_and_query_are_not_re_encoded(h):
    resp = await h.client.get("/v1/files/a%2Fb%20c?after=x%26y&beta=true")
    await resp.read()
    (got,) = h.fake.requests
    assert got.path == "/v1/files/a%2Fb%20c"
    assert got.query == "after=x%26y&beta=true"


async def test_large_body_forwarded_intact(h):
    body = msg_body(stream_=False)
    body["messages"][0]["content"] = "x" * (20 * 1024 * 1024)
    raw = json.dumps(body).encode()
    resp = await h.post("/v1/messages", raw, ANTHROPIC_HEADERS)
    assert resp.status == 200
    assert h.fake.requests[0].body == raw
    (rec,) = await h.records()
    assert rec.state is State.OK


@pytest.mark.parametrize("encoding", ["gzip", "zstd"])
async def test_compressed_request_bodies_forwarded_decoded(h, encoding):
    raw = json.dumps(chat_body(stream_=False)).encode()
    packed = gzip.compress(raw) if encoding == "gzip" else zstd.compress(raw)
    resp = await h.post(
        "/v1/chat/completions",
        packed,
        {"content-type": "application/json", "content-encoding": encoding},
    )
    assert resp.status == 200
    (got,) = h.fake.requests
    assert got.body == raw
    assert "content-encoding" not in got.headers
    (rec,) = await h.records()
    assert rec.state is State.OK and rec.usage.cache_read == 1000


async def test_undecodable_request_body_is_a_clean_400(h, caplog):
    caplog.set_level(logging.DEBUG)
    resp = await h.post(
        "/v1/responses",
        b"definitely not zstd",
        {"content-type": "application/json", "content-encoding": "zstd"},
    )
    assert resp.status == 400
    err = (await resp.json())["error"]
    assert err["type"] == "invalid_request_error" and "decode" in err["message"]
    assert h.fake.requests == []
    assert "Unhandled exception" not in caplog.text
    assert "Traceback" not in caplog.text
    # the connection stays usable
    resp = await h.client.get("/v1/models")
    assert resp.status == 200


async def test_unknown_content_encoding_is_forwarded_as_is(h):
    resp = await h.post("/v1/files", b"opaque", {"content-encoding": "x-custom"})
    await resp.read()
    (got,) = h.fake.requests
    assert got.body == b"opaque" and got.headers["content-encoding"] == "x-custom"


# -- 2. metering ---------------------------------------------------------------------------


async def test_claude_code_turn_is_metered(h):
    h.fake.reply(
        stream(
            [(CC / "first_turn.response.sse").read_bytes()],
            headers={
                "anthropic-ratelimit-unified-7d-utilization": "0.1",
            },
        )
    )
    body = json.loads((CC / "first_turn.request.json").read_text(encoding="utf-8"))
    resp = await h.messages(
        body,
        {
            "x-claude-code-session-id": "sess-header",
            "x-claude-code-agent-id": "agent-main",
            "x-claude-code-request-class": "main",
            "anthropic-beta": "oauth-2025-04-20",
            "user-agent": "claude-cli/2.1.287 (external, claude-desktop, agent-sdk/0.3.293)",
        },
        path="/s/team-a",
    )
    await resp.read()
    (rec,) = await h.records()
    assert rec.state is State.OK and rec.status == 200
    assert rec.scope == "team-a"
    assert rec.session == "sess-header"
    assert rec.agent == "agent-main"
    assert rec.request_class == "main"
    assert rec.plan is True
    assert rec.client == "claude-cli/2.1.287"
    assert rec.model == "claude-haiku-4-5-20251001"
    assert rec.upstream_id == "msg_fixture"
    u = rec.usage
    assert (u.input_tokens, u.cache_write_1h, u.cache_write_5m, u.cache_read) == (
        10,
        5339,
        0,
        52496,
    )
    assert u.output_tokens == 290 and u.reasoning_tokens == 69
    expected = (10 * 1.0 + 5339 * 2.0 + 52496 * 0.1 + 290 * 5.0) / 1e6
    assert rec.cost_usd == pytest.approx(expected)
    assert rec.reserved_usd == 0
    assert rec.ttft_ms is not None and rec.duration_ms is not None
    segs = h.store.segments(rec.id)
    assert segs and sum(s.est_tokens for s in segs) == u.prompt_tokens
    assert h.store.ratelimit(Provider.ANTHROPIC)[1] == {
        "anthropic-ratelimit-unified-7d-utilization": "0.1"
    }


async def test_session_from_metadata_and_fingerprint_agent(h):
    body = json.loads((CC / "side_request.request.json").read_text(encoding="utf-8"))
    h.fake.reply(Reply(body=(CC / "side_request.response.json").read_bytes()))
    resp = await h.messages(body, {"x-skinflint-scope": "other"})
    await resp.read()
    (rec,) = await h.records()
    assert rec.session == SESSION
    assert rec.agent.startswith("fp:") and len(rec.agent) == 11
    assert rec.scope == "other" and rec.plan is False and rec.request_class is None
    assert rec.usage.cache_read == 4511 and rec.stream is False


async def test_openai_chat_json_metered(h):
    resp = await h.post("/v1/chat/completions", chat_body(stream_=False), {"session_id": "cx-1"})
    await resp.read()
    (rec,) = await h.records()
    assert rec.provider is Provider.OPENAI and rec.endpoint is Endpoint.CHAT
    assert rec.session == "cx-1"
    assert rec.usage.input_tokens == 200 and rec.usage.cache_read == 1000
    assert rec.cost_usd > 0


# -- 3. budgets ----------------------------------------------------------------------------


async def test_budget_blocks_anthropic_with_billing_error(make):
    h = await make(budgets=[BudgetRule(name="daily", usd=1e-9)])
    resp = await h.messages()
    assert resp.status == 402
    assert resp.headers["x-should-retry"] == "false"
    assert "retry-after" not in resp.headers
    err = await resp.json()
    assert err["type"] == "error" and err["error"]["type"] == "billing_error"
    assert "daily" in err["error"]["message"]
    assert h.fake.requests == []
    (rec,) = await h.records()
    assert rec.state is State.BLOCKED and rec.blocked_by == "daily"


async def test_budget_blocks_openai_with_insufficient_quota(make):
    h = await make(budgets=[BudgetRule(name="daily", usd=1e-9)])
    resp = await h.post("/v1/responses", {"model": "gpt-5.4", "input": "hi"})
    assert resp.status == 429
    assert resp.headers["x-should-retry"] == "false"
    err = (await resp.json())["error"]
    assert err["type"] == "insufficient_quota" and err["code"] == "skinflint_budget_exceeded"
    assert h.fake.requests == []


async def test_concurrent_admission_never_exceeds_cap(make):
    h = await make()
    info = h.proxy.budget  # reservation for one request, computed the same way as admit()
    from skinflint.providers import ADAPTERS

    req = ADAPTERS[Provider.ANTHROPIC].parse_request(Endpoint.MESSAGES, msg_body())
    per_request, _ = info.reservation(req)
    assert per_request > 0
    h.proxy.budget.rules = [BudgetRule(name="tight", usd=per_request * 5.5)]
    h.fake.reply(*[stream(anthropic_events(), head_delay=0.3) for _ in range(20)])
    responses = await asyncio.gather(*(h.messages() for _ in range(20)))
    statuses = Counter(r.status for r in responses)
    for r in responses:
        await r.read()
    assert statuses == {200: 5, 402: 15}
    assert len(h.fake.requests) == 5
    states = Counter(r.state for r in await h.records())
    assert states == {State.OK: 5, State.BLOCKED: 15}


async def test_warn_rule_does_not_block(make, caplog):
    caplog.set_level(logging.WARNING, logger="skinflint")
    h = await make(budgets=[BudgetRule(name="heads-up", usd=1e-9, action=Action.WARN)])
    resp = await h.messages()
    assert resp.status == 200
    await resp.read()
    assert "heads-up" in caplog.text
    (rec,) = await h.records()
    assert rec.state is State.OK


async def test_per_session_cap(make):
    rule = BudgetRule(name="per-session", requests=1, per=Per.SESSION, window=Window.TOTAL)
    h = await make(budgets=[rule])
    a1 = await h.messages(headers={"x-claude-code-session-id": "A"})
    await a1.read()
    a2 = await h.messages(headers={"x-claude-code-session-id": "A"})
    b1 = await h.messages(headers={"x-claude-code-session-id": "B"})
    await b1.read()
    assert (a1.status, a2.status, b1.status) == (200, 402, 200)
    assert "per-session" in (await a2.json())["error"]["message"]


# -- 4. failure modes ----------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_upstream_unreachable(make):
    h = await make()
    h.cfg.upstreams[Provider.ANTHROPIC] = f"http://127.0.0.1:{free_port()}"
    h.cfg.upstreams[Provider.OPENAI] = h.cfg.upstreams[Provider.ANTHROPIC]
    resp = await h.messages()
    assert resp.status == 502
    assert (await resp.json())["error"]["type"] == "api_error"
    resp = await h.post("/v1/chat/completions", chat_body())
    assert resp.status == 502
    assert (await resp.json())["error"]["type"] == "server_error"
    resp = await h.client.get("/v1/models")
    assert resp.status == 502
    recs = await h.records()
    assert [(r.state, r.status) for r in recs] == [(State.ERROR, 502)] * 2
    assert "unreachable" in recs[0].error


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_upstream_errors_pass_through(h, status):
    reply = error_reply(status, "slow down", "rate_limit_error", headers={"retry-after": "7"})
    h.fake.reply(reply)
    resp = await h.messages()
    assert resp.status == status
    assert await resp.read() == reply.body
    assert resp.headers["retry-after"] == "7"
    (rec,) = await h.records()
    assert rec.state is State.ERROR and rec.status == status and rec.error == "slow down"


@pytest.mark.parametrize("cancel", [False, True])
async def test_client_disconnect_mid_stream(make, cancel):
    h = await make(handler_cancellation=cancel)
    h.fake.reply(stream(anthropic_events(deltas=200), delay=0.02))
    resp = await h.messages()
    first = await resp.content.readany()
    assert first.startswith(b"event: message_start")
    resp.close()
    await wait_for(lambda: h.fake.disconnects == 1 and h.fake.active == 0)
    (rec,) = await h.records()
    assert rec.state is State.ABORTED
    assert rec.error == ("cancelled" if cancel else "client disconnected")
    assert rec.usage.cache_read == 50000 and rec.usage.output_tokens == 1
    assert rec.cost_usd > 0


async def test_shutdown_with_stream_in_flight_settles_row(make):
    h = await make(shutdown_timeout=0.3)
    h.fake.reply(stream(anthropic_events(deltas=500), delay=0.02))
    resp = await h.messages()
    await resp.content.readany()
    db = h.cfg.db_path
    await h.close()
    resp.close()
    with Store(db) as store:
        (rec,) = store.records()
    assert rec.state is State.ABORTED and rec.usage.cache_read == 50000
    await wait_for(lambda: h.fake.active == 0)


async def test_stream_without_terminal_event(h):
    h.fake.reply(stream(anthropic_events(terminal=False)))
    resp = await h.messages()
    await resp.read()
    (rec,) = await h.records()
    assert rec.state is State.ERROR and rec.error == "stream ended early"
    assert rec.usage.output_tokens == 300


async def test_upstream_drop_mid_stream_reaches_client_as_truncation(h):
    h.fake.reply(stream(anthropic_events(), disconnect_after=3))
    resp = await h.messages()
    with pytest.raises(aiohttp.ClientPayloadError):
        await resp.read()
    (rec,) = await h.records()
    assert rec.state is State.ERROR and "upstream stream failed" in rec.error
    assert rec.usage.cache_read == 50000


async def test_stream_error_event_marks_row(h):
    events = anthropic_events(terminal=False)[:3] + [
        b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error",'
        b'"message":"Overloaded"}}\n\n'
    ]
    h.fake.reply(stream(events))
    resp = await h.messages()
    await resp.read()
    (rec,) = await h.records()
    assert rec.state is State.ERROR and "Overloaded" in rec.error


async def test_malformed_json_forwarded_unmetered(h):
    resp = await h.post("/v1/messages", b"{not json", ANTHROPIC_HEADERS)
    assert resp.status == 400
    assert h.fake.requests[0].body == b"{not json"
    assert await h.records() == []


async def test_non_metered_endpoints_pass_through(h):
    resp = await h.post("/v1/messages/count_tokens", msg_body(), ANTHROPIC_HEADERS)
    assert (await resp.json()) == {"input_tokens": 1234}
    resp = await h.client.get("/v1/models", headers={"anthropic-version": "2023-06-01"})
    assert resp.status == 200 and (await resp.json())["data"]
    resp = await h.client.head("/api/hello")
    assert resp.status == 200
    assert [r.method for r in h.fake.requests] == ["POST", "GET", "HEAD"]
    assert await h.records() == []


async def test_unmetered_block(make):
    h = await make(unmetered="block", budgets=[BudgetRule(name="daily", usd=10)])
    resp = await h.post("/v1/files", b"data", {"anthropic-version": "2023-06-01"})
    assert resp.status == 402 and resp.headers["x-should-retry"] == "false"
    resp = await h.post("/v1/messages/count_tokens", msg_body(), ANTHROPIC_HEADERS)
    assert resp.status == 200
    resp = await h.client.get("/v1/models")
    assert resp.status == 200
    assert [r.path for r in h.fake.requests] == ["/v1/messages/count_tokens", "/v1/models"]


async def test_websocket_upgrade_refused(h):
    resp = await h.client.get(
        "/v1/responses", headers={"connection": "Upgrade", "upgrade": "websocket"}
    )
    assert resp.status == 426
    assert h.fake.requests == []


async def test_invalid_scope(h):
    resp = await h.messages(path="/s/bad%20scope")
    assert resp.status == 400
    resp = await h.messages(headers={"x-skinflint-scope": "x" * 65})
    assert resp.status == 400
    resp = await h.client.post("/s//v1/messages")
    assert resp.status == 400
    assert h.fake.requests == []


async def test_gzip_upstream_decoded(h):
    data = (CC / "side_request.response.json").read_bytes()
    h.fake.reply(Reply(body=data, gzip=True))
    resp = await h.messages(msg_body(stream_=False))
    assert "content-encoding" not in resp.headers
    assert await resp.read() == data
    events = anthropic_events()
    h.fake.reply(stream(events, gzip=True))
    resp = await h.messages()
    assert "content-encoding" not in resp.headers
    assert await resp.read() == b"".join(events)
    assert [r.state for r in await h.records()] == [State.OK, State.OK]


async def test_health(h):
    resp = await h.client.get("/_skinflint/health")
    assert (await resp.json())["ok"] is True


async def test_cpu_work_does_not_stall_streams(h):
    """Parsing and profiling ~250 KB bodies must not freeze other streams."""
    big = (CC / "first_turn.request.json").read_bytes()
    h.fake.reply(stream(anthropic_events(deltas=60), delay=0.01))
    resp = await h.messages()
    gaps: list[float] = []

    async def reader():
        last = time.perf_counter()
        async for _ in resp.content.iter_any():
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    async def load():
        await asyncio.sleep(0.05)
        rs = await asyncio.gather(
            *(h.post("/v1/messages", big, ANTHROPIC_HEADERS) for _ in range(10))
        )
        for r in rs:
            await r.read()

    await asyncio.gather(reader(), load())
    assert max(gaps) < 0.5, f"stream stalled for {max(gaps):.3f}s"
    assert len(await h.records()) == 11


# -- 5. rewrites ---------------------------------------------------------------------------


async def test_chat_stream_usage_injected_and_hidden(h):
    resp = await h.post("/v1/chat/completions", chat_body())
    text = await resp.read()
    sent = h.fake.requests[0].json()
    assert sent["stream_options"] == {"include_usage": True}
    expected = chat_events("gpt-5.4", "chatcmpl-0001", CHAT_USAGE, include_usage=True)
    assert text == b"".join(e for e in expected if b'"choices":[]' not in e)
    (rec,) = await h.records()
    assert rec.usage.cache_read == 1000 and rec.usage.output_tokens == 80


async def test_chat_stream_usage_injected_into_existing_options(h):
    resp = await h.post("/v1/chat/completions", chat_body(stream_options={"x": 1}))
    text = await resp.read()
    assert h.fake.requests[0].json()["stream_options"] == {"x": 1, "include_usage": True}
    assert b'"choices":[]' not in text
    (rec,) = await h.records()
    assert rec.usage.output_tokens == 80


async def test_chat_stream_client_usage_left_alone(h):
    raw = json.dumps(chat_body(stream_options={"include_usage": True})).encode()
    resp = await h.post("/v1/chat/completions", raw)
    text = await resp.read()
    assert h.fake.requests[0].body == raw
    assert b'"choices":[]' in text
    (rec,) = await h.records()
    assert rec.usage.output_tokens == 80


@pytest.fixture
def first_party(monkeypatch, fake):
    monkeypatch.setitem(proxy_mod.FIRST_PARTY, Provider.ANTHROPIC, fake.url)


async def turn(h: Harness, agent: str | None, session: str | None = "S") -> dict:
    headers = {}
    if session:
        headers["x-claude-code-session-id"] = session
    if agent:
        headers["x-claude-code-agent-id"] = agent
    resp = await h.messages(headers=headers)
    await resp.read()
    await h.records()
    return h.fake.requests[-1].json()


async def test_diagnostics_injection_per_conversation(h, first_party):
    assert (await turn(h, "A"))["diagnostics"] == {"previous_message_id": None}
    assert (await turn(h, "A"))["diagnostics"] == {"previous_message_id": "msg_0001"}
    assert (await turn(h, "B"))["diagnostics"] == {"previous_message_id": None}
    assert (await turn(h, "A"))["diagnostics"] == {"previous_message_id": "msg_0002"}
    assert (await turn(h, "B"))["diagnostics"] == {"previous_message_id": "msg_0003"}
    assert "diagnostics" not in await turn(h, "A", session=None)


async def test_diagnostics_not_injected_for_third_party_upstream(h):
    assert "diagnostics" not in await turn(h, "A")
    assert "diagnostics" not in await turn(h, "A")
    assert h.fake.requests[0].body == json.dumps(msg_body()).encode()


async def test_client_diagnostics_left_alone(h, first_party):
    body = msg_body(diagnostics={"previous_message_id": "msg_client"})
    raw = json.dumps(body).encode()
    resp = await h.post("/v1/messages", raw, {**ANTHROPIC_HEADERS, "x-claude-code-session-id": "S"})
    await resp.read()
    assert h.fake.requests[0].body == raw


async def test_diagnostics_rejected_retries_once_and_disables(h, first_party):
    h.fake.reply(
        error_reply(400, "diagnostics: Extra inputs are not permitted", "invalid_request_error")
    )
    resp = await h.messages(headers={"x-claude-code-session-id": "S"})
    assert resp.status == 200
    assert await resp.read() == b"".join(anthropic_events(MODEL, "msg_0001"))
    first, retry = h.fake.requests
    assert "diagnostics" in first.json() and "diagnostics" not in retry.json()
    assert retry.body == json.dumps(msg_body()).encode()
    (rec,) = await h.records()
    assert rec.state is State.OK and rec.upstream_id == "msg_0001"
    assert h.proxy.diagnostics is False
    assert "diagnostics" not in await turn(h, None)
    assert len(h.fake.requests) == 3


async def test_other_400_with_diagnostics_injected_passes_through(h, first_party):
    reply = error_reply(400, "max_tokens: too large", "invalid_request_error")
    h.fake.reply(reply)
    resp = await h.messages(headers={"x-claude-code-session-id": "S"})
    assert resp.status == 400 and await resp.read() == reply.body
    assert len(h.fake.requests) == 1
    (rec,) = await h.records()
    assert rec.state is State.ERROR and rec.error == "max_tokens: too large"
    assert h.proxy.diagnostics is True


async def test_diagnostics_retry_unreachable(h, first_party):
    h.fake.reply(error_reply(400, "unknown field diagnostics", "invalid_request_error"))
    h.fake.reply(lambda rec: Reply(chunks=[b"x"], disconnect_after=0))
    resp = await h.messages(headers={"x-claude-code-session-id": "S"})
    assert resp.status == 502
    (rec,) = await h.records()
    assert rec.state is State.ERROR and rec.status == 502


DIAG = {"cache_miss_reason": {"type": "system_changed", "cache_missed_input_tokens": 5000}}


async def test_cache_miss_reason_from_stream_and_json(h):
    h.fake.diagnostics = DIAG
    resp = await h.messages()
    await resp.read()
    h.fake.reply(Reply(body=anthropic_json(diagnostics=DIAG)))
    resp = await h.messages(msg_body(stream_=False))
    await resp.read()
    recs = await h.records()
    assert [(r.cache_miss_reason, r.cache_missed_tokens) for r in recs] == [
        ("system_changed", 5000)
    ] * 2


# -- 6. embedded ---------------------------------------------------------------------------


def test_embedded_runs_and_shuts_down(tmp_path):
    dead = f"http://127.0.0.1:{free_port()}"
    cfg = Config(
        port=0,
        db_path=tmp_path / "e.db",
        upstreams={Provider.ANTHROPIC: dead, Provider.OPENAI: dead},
    )
    with embedded(cfg) as base:
        with urllib.request.urlopen(base + "/_skinflint/health", timeout=5) as r:
            assert json.loads(r.read())["ok"] is True
        req = urllib.request.Request(
            base + "/v1/messages",
            data=json.dumps(msg_body()).encode(),
            headers=ANTHROPIC_HEADERS,
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 502
        port = int(base.rsplit(":", 1)[1])
    import threading

    assert not any(t.name == "skinflint-proxy" for t in threading.enumerate())
    assert not any(t.name.startswith("skinflint-db") for t in threading.enumerate())
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
    with Store(cfg.db_path) as store:
        (rec,) = store.records()
        assert rec.state is State.ERROR and rec.status == 502


async def test_responses_failed_event_marks_row(h):
    h.fake.reply(stream(responses_events(terminal="response.failed"), chunk_size=5))
    resp = await h.post("/responses", {"model": "gpt-5.4", "input": "hi", "stream": True})
    assert await resp.read() == b"".join(responses_events(terminal="response.failed"))
    (rec,) = await h.records()
    assert rec.state is State.ERROR and rec.endpoint is Endpoint.RESPONSES
    assert rec.usage.cache_read == 2048


async def test_stream_without_content_type_is_metered_as_stream(h):
    # Codex's ChatGPT backend streams server-sent events with no Content-Type header.
    h.fake.reply(Reply(chunks=responses_events(), content_type=""))
    resp = await h.post(
        "/responses",
        {"model": "gpt-5.5", "input": "hi", "stream": True},
        headers={"session-id": "codex-session-1"},
    )
    assert await resp.read() == b"".join(responses_events())
    (rec,) = await h.records()
    assert rec.state is State.OK
    assert rec.usage.cache_read == 2048
    assert rec.session == "codex-session-1"


@pytest.mark.parametrize("cancel", [False, True])
async def test_client_disconnect_after_terminal_event_is_ok(make, cancel):
    # Codex closes the connection as soon as it has response.completed.
    h = await make(handler_cancellation=cancel)
    h.fake.reply(stream(responses_events() + [b": keep-alive\n\n"] * 50, delay=0.02))
    resp = await h.post("/responses", {"model": "gpt-5.5", "input": "hi", "stream": True})
    seen = b""
    while b"response.completed" not in seen or not seen.endswith(b"\n\n"):
        seen += await resp.content.readany()
    resp.close()
    await wait_for(lambda: h.fake.disconnects == 1 and h.fake.active == 0)
    (rec,) = await h.records()
    assert rec.state is State.OK and rec.error is None
    assert rec.usage.cache_read == 2048


async def test_chatgpt_account_marks_plan_traffic(h):
    h.fake.reply(stream(responses_events()))
    resp = await h.post(
        "/responses",
        {"model": "gpt-5.5", "input": "hi", "stream": True},
        headers={"chatgpt-account-id": "acct"},
    )
    await resp.read()
    (rec,) = await h.records()
    assert rec.plan is True
