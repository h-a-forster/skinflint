"""A fake upstream for proxy tests and benchmarks.

``FakeUpstream`` is an aiohttp server that answers like the Anthropic Messages API and the
OpenAI Chat Completions / Responses APIs (JSON and SSE), with configurable usage, delays
between chunks, mid-stream errors, abrupt disconnects, gzip bodies and 4xx/5xx errors. It
records every request it received. Queue ``Reply`` objects (or callables) to script answers;
otherwise it emulates the provider from the request path and body.
"""

from __future__ import annotations

import asyncio
import gzip
import itertools
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from multidict import CIMultiDict

ANTHROPIC_USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 50000,
    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2000},
    "output_tokens": 300,
    "service_tier": "standard",
}
CHAT_USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 80,
    "total_tokens": 1280,
    "prompt_tokens_details": {"cached_tokens": 1000},
    "completion_tokens_details": {"reasoning_tokens": 20},
}
RESPONSES_USAGE = {
    "input_tokens": 3000,
    "input_tokens_details": {"cached_tokens": 2048},
    "output_tokens": 150,
    "output_tokens_details": {"reasoning_tokens": 64},
    "total_tokens": 3150,
}


@dataclass
class Received:
    method: str
    path: str  # raw path, without the query string
    query: str  # raw query string
    headers: CIMultiDict[str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    chunks: list[bytes] | None = None  # streamed one write per chunk when set
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0  # before each chunk
    head_delay: float = 0.0  # before the response headers
    gzip: bool = False
    disconnect_after: int | None = None  # close the connection after this many chunks


def sse(event: str | None, data: Any) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {text}\n\n".encode()


def split(data: bytes, size: int) -> list[bytes]:
    """Fixed-size chunks: boundaries fall mid-event and mid-line."""
    return [data[i : i + size] for i in range(0, len(data), size)]


def anthropic_events(
    model: str = "claude-haiku-4-5",
    msg_id: str = "msg_fake",
    usage: dict | None = None,
    text: str = "Hello there",
    diagnostics: Any = None,
    deltas: int = 3,
    terminal: bool = True,
) -> list[bytes]:
    usage = dict(ANTHROPIC_USAGE if usage is None else usage)
    start_usage = {**usage, "output_tokens": 1}
    events = [
        sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "model": model,
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "stop_reason": None,
                    "usage": start_usage,
                    "diagnostics": diagnostics,
                },
            },
        ),
        sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        ),
        sse("ping", {"type": "ping"}),
    ]
    for i in range(deltas):
        events.append(
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": f"{text} {i}"},
                },
            )
        )
    events.append(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
    events.append(
        sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {k: v for k, v in usage.items() if k != "cache_creation"},
            },
        )
    )
    if terminal:
        events.append(sse("message_stop", {"type": "message_stop"}))
    return events


def anthropic_json(
    model: str = "claude-haiku-4-5",
    msg_id: str = "msg_fake",
    usage: dict | None = None,
    diagnostics: Any = None,
) -> bytes:
    return json.dumps(
        {
            "model": model,
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": ANTHROPIC_USAGE if usage is None else usage,
            "diagnostics": diagnostics,
        }
    ).encode()


def chat_events(
    model: str = "gpt-5.4",
    cid: str = "chatcmpl-fake",
    usage: dict | None = None,
    include_usage: bool = True,
    deltas: int = 3,
    terminal: bool = True,
) -> list[bytes]:
    base = {"id": cid, "object": "chat.completion.chunk", "created": 1760000000, "model": model}
    null_usage = {"usage": None} if include_usage else {}
    events = []
    for i in range(deltas):
        choice = {"index": 0, "delta": {"content": f"tok{i}"}, "finish_reason": None}
        events.append(sse(None, {**base, "choices": [choice], **null_usage}))
    done = {"index": 0, "delta": {}, "finish_reason": "stop"}
    events.append(sse(None, {**base, "choices": [done], **null_usage}))
    if include_usage:
        events.append(sse(None, {**base, "choices": [], "usage": usage or CHAT_USAGE}))
    if terminal:
        events.append(b"data: [DONE]\n\n")
    return events


def chat_json(model: str = "gpt-5.4", cid: str = "chatcmpl-fake", usage: dict | None = None):
    return json.dumps(
        {
            "id": cid,
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": usage or CHAT_USAGE,
        }
    ).encode()


def responses_events(
    model: str = "gpt-5.4",
    rid: str = "resp_fake",
    usage: dict | None = None,
    deltas: int = 3,
    terminal: str | None = "response.completed",
) -> list[bytes]:
    def resp(status: str, u: Any) -> dict:
        return {"id": rid, "object": "response", "model": model, "status": status, "usage": u}

    events = [
        sse("response.created", {"type": "response.created", "response": resp("in_progress", None)})
    ]
    for i in range(deltas):
        events.append(
            sse(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": f"tok{i}"},
            )
        )
    if terminal:
        status = terminal.split(".", 1)[1]
        events.append(
            sse(terminal, {"type": terminal, "response": resp(status, usage or RESPONSES_USAGE)})
        )
    return events


def responses_json(model: str = "gpt-5.4", rid: str = "resp_fake", usage: dict | None = None):
    return json.dumps(
        {
            "id": rid,
            "object": "response",
            "model": model,
            "status": "completed",
            "output": [],
            "usage": usage or RESPONSES_USAGE,
        }
    ).encode()


def stream(events: list[bytes], chunk_size: int | None = None, **kw: Any) -> Reply:
    data = b"".join(events)
    chunks = split(data, chunk_size) if chunk_size else list(events)
    return Reply(chunks=chunks, content_type="text/event-stream", **kw)


def error_reply(status: int, message: str, kind: str = "api_error", **kw: Any) -> Reply:
    body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
    return Reply(status=status, body=body, **kw)


Responder = Callable[[Received], Reply]


class FakeUpstream:
    def __init__(self) -> None:
        self.requests: list[Received] = []
        self.queue: deque[Reply | Responder] = deque()
        self.anthropic_usage: dict = dict(ANTHROPIC_USAGE)
        self.chat_usage: dict = dict(CHAT_USAGE)
        self.responses_usage: dict = dict(RESPONSES_USAGE)
        self.diagnostics: Any = None
        self.delay = 0.0  # default delay between streamed events
        self.headers: dict[str, str] = {"request-id": "req_fake"}
        self.disconnects = 0  # writes that failed because the reader went away
        self.active = 0  # responses currently being written
        self.record = True
        self._ids = itertools.count(1)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=512 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    def reply(self, *replies: Reply | Responder) -> None:
        self.queue.extend(replies)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        raw_path, _, query = request.raw_path.partition("?")
        rec = Received(request.method, raw_path, query, CIMultiDict(request.headers), b"")
        rec.body = await request.read()
        if self.record:
            self.requests.append(rec)
        if self.queue:
            item = self.queue.popleft()
            reply = item(rec) if callable(item) else item
        else:
            reply = self.default(rec)
        return await self.send(request, reply)

    def default(self, rec: Received) -> Reply:
        path = rec.path.rstrip("/")
        if rec.method == "HEAD":
            return Reply(status=200)
        if rec.method == "GET" and path.endswith("/models"):
            return Reply(body=json.dumps({"data": [{"id": "claude-haiku-4-5"}]}).encode())
        if path.endswith("/count_tokens"):
            return Reply(body=b'{"input_tokens":1234}')
        try:
            body = rec.json()
        except ValueError:
            return error_reply(400, "invalid JSON body", "invalid_request_error")
        n = next(self._ids)
        streaming = body.get("stream") is True
        model = body.get("model", "unknown")
        if path.endswith("/v1/messages"):
            mid = f"msg_{n:04d}"
            if streaming:
                return stream(
                    anthropic_events(
                        model, mid, self.anthropic_usage, diagnostics=self.diagnostics
                    ),
                    delay=self.delay,
                )
            return Reply(body=anthropic_json(model, mid, self.anthropic_usage, self.diagnostics))
        if path.endswith("/chat/completions"):
            cid = f"chatcmpl-{n:04d}"
            if streaming:
                opts = body.get("stream_options") or {}
                include = opts.get("include_usage") is True
                return stream(
                    chat_events(model, cid, self.chat_usage, include_usage=include),
                    delay=self.delay,
                )
            return Reply(body=chat_json(model, cid, self.chat_usage))
        if path.endswith("/responses"):
            rid = f"resp_{n:04d}"
            if streaming:
                return stream(responses_events(model, rid, self.responses_usage), delay=self.delay)
            return Reply(body=responses_json(model, rid, self.responses_usage))
        return error_reply(404, f"no route {rec.path}", "not_found_error")

    async def send(self, request: web.Request, reply: Reply) -> web.StreamResponse:
        if reply.head_delay:
            await asyncio.sleep(reply.head_delay)
        headers = {**self.headers, "content-type": reply.content_type, **reply.headers}
        if reply.chunks is None:
            body = reply.body
            if reply.gzip:
                body = gzip.compress(body)
                headers["content-encoding"] = "gzip"
            return web.Response(status=reply.status, headers=headers, body=body)
        chunks = reply.chunks
        if reply.gzip:
            chunks = split(gzip.compress(b"".join(chunks)), 64)
            headers["content-encoding"] = "gzip"
        resp = web.StreamResponse(status=reply.status, headers=headers)
        self.active += 1
        try:
            await resp.prepare(request)
            for i, chunk in enumerate(chunks):
                if reply.disconnect_after is not None and i >= reply.disconnect_after:
                    assert request.transport is not None
                    request.transport.close()
                    return resp
                if reply.delay:
                    await asyncio.sleep(reply.delay)
                await resp.write(chunk)
            await resp.write_eof()
        except ConnectionError:
            self.disconnects += 1
        except asyncio.CancelledError:
            self.disconnects += 1
            raise
        finally:
            self.active -= 1
        return resp


async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll until predicate() is true."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
