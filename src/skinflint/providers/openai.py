"""OpenAI Chat Completions and Responses API adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import EllipsisType
from typing import Any

from skinflint.model import Endpoint, Provider, RequestInfo, Usage
from skinflint.providers.base import (
    as_dict,
    as_int,
    as_str,
    bound_tokens,
    error_message,
    estimate_tokens,
    header,
    headers_with_prefix,
    load_json,
    output_chars,
    server_inputs,
    web_search_cap,
)
from skinflint.sse import SSEEvent, SSEParser

_USAGE_NULL = re.compile(r'"usage"\s*:\s*null')
_RESPONSE_EVENTS = frozenset(
    {
        "response.created",
        "response.queued",
        "response.in_progress",
        "response.completed",
        "response.failed",
        "response.incomplete",
        "error",
    }
)
_CHAT_DELTA_TEXT = ("content", "reasoning_content", "refusal")
_TERMINAL = frozenset({"response.completed", "response.failed", "response.incomplete"})
_SESSION_HEADERS = ("session-id", "session_id", "x-session-id")  # Codex sends session-id


def openai_usage(usage: Any, service_tier: str | None = None) -> Usage:
    """Normalise a Chat (prompt_/completion_tokens) or Responses (input_/output_tokens) usage.

    OpenAI's prompt count includes cached and written tokens; they are subtracted here.
    """
    u = as_dict(usage)
    if "prompt_tokens" in u or "completion_tokens" in u:
        total = as_int(u.get("prompt_tokens")) or 0
        details = as_dict(u.get("prompt_tokens_details"))
        output = as_int(u.get("completion_tokens")) or 0
        out_details = as_dict(u.get("completion_tokens_details"))
    else:
        total = as_int(u.get("input_tokens")) or 0
        details = as_dict(u.get("input_tokens_details"))
        output = as_int(u.get("output_tokens")) or 0
        out_details = as_dict(u.get("output_tokens_details"))
    cached = as_int(details.get("cached_tokens")) or 0
    written = as_int(details.get("cache_write_tokens")) or 0
    return Usage(
        input_tokens=max(0, total - cached - written),
        cache_write_5m=written,
        cache_read=cached,
        output_tokens=output,
        reasoning_tokens=as_int(out_details.get("reasoning_tokens")) or 0,
        service_tier=service_tier,
    )


class _Tracker:
    def __init__(self) -> None:
        self._parser = SSEParser()
        self.usage = Usage()
        self.streamed_chars = 0
        self.model: str | None = None
        self.upstream_id: str | None = None
        self.finished = False
        self.error: str | None = None
        self.cache_miss_reason: str | None = None
        self.cache_missed_tokens: int | None = None
        self._service_tier: str | None = None

    def _note(self, obj: dict) -> None:
        self.model = as_str(obj.get("model")) or self.model
        self.upstream_id = as_str(obj.get("id")) or self.upstream_id
        tier = as_str(obj.get("service_tier"))
        if tier is not None:
            self._service_tier = tier
            self.usage.service_tier = tier


class ChatStreamTracker(_Tracker):
    """Chat Completions stream. With ``strip_usage_chunk`` the usage-only chunk (empty
    ``choices``) that skinflint asked for is withheld from the client."""

    def __init__(self, strip_usage_chunk: bool = False) -> None:
        super().__init__()
        self._strip = strip_usage_chunk
        self._stripped = False

    def feed(self, chunk: bytes) -> bytes:
        chunk = bytes(chunk) if not isinstance(chunk, bytes) else chunk
        try:
            events = self._parser.feed(chunk)
        except Exception:
            return chunk if not self._strip else b""
        if not self._strip:
            for ev in events:
                self._handle(ev)
            return chunk
        return b"".join(ev.raw for ev in events if not self._handle(ev))

    def close(self) -> bytes:
        try:
            events = self._parser.close()
        except Exception:
            return b""
        kept = [ev.raw for ev in events if not self._handle(ev)]
        return b"".join(kept) if self._strip else b""

    def _handle(self, ev: SSEEvent) -> bool:
        """Observe one event; True if it must not be forwarded."""
        try:
            data = ev.data
            if not data:
                return False
            if data.strip() == "[DONE]":
                self.finished = True
                return False
            obj = load_json(data)
            if not isinstance(obj, dict):
                return False
            self._count(obj.get("choices"))
            wanted = (
                self.model is None
                or ('"usage"' in data and not _USAGE_NULL.search(data))
                or '"error"' in data
            )
            if not wanted:
                return False
            if obj.get("error"):
                self.error = error_message(obj["error"])
            self._note(obj)
            usage = obj.get("usage")
            if isinstance(usage, dict):
                self.usage = openai_usage(usage, self._service_tier)
                if self._strip and not self._stripped and obj.get("choices") == []:
                    self._stripped = True
                    return True
        except Exception:
            pass
        return False

    def _count(self, choices: Any) -> None:
        """Add the generated content in one chunk's choice deltas to streamed_chars."""
        for choice in choices if isinstance(choices, list) else []:
            delta = as_dict(as_dict(choice).get("delta"))
            for key in _CHAT_DELTA_TEXT:
                text = delta.get(key)
                if isinstance(text, str):
                    self.streamed_chars += output_chars(text)
            calls = delta.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                args = as_dict(as_dict(call).get("function")).get("arguments")
                if isinstance(args, str):
                    self.streamed_chars += output_chars(args)


class ResponsesStreamTracker(_Tracker):
    """Responses API stream; usage arrives on the terminal response.* event."""

    def feed(self, chunk: bytes) -> bytes:
        chunk = bytes(chunk) if not isinstance(chunk, bytes) else chunk
        try:
            for ev in self._parser.feed(chunk):
                self._handle(ev)
        except Exception:
            pass
        return chunk

    def close(self) -> bytes:
        try:
            for ev in self._parser.close():
                self._handle(ev)
        except Exception:
            pass
        return b""

    def _handle(self, ev: SSEEvent) -> None:
        if not ev.data:
            return
        is_delta = ev.event is not None and ev.event.endswith(".delta")
        if ev.event is not None and ev.event not in _RESPONSE_EVENTS and not is_delta:
            return
        obj = load_json(ev.data)
        if not isinstance(obj, dict):
            return
        kind = as_str(obj.get("type")) or ev.event
        if kind is not None and kind.endswith(".delta"):
            # output_text, reasoning_text, reasoning_summary_text, function_call_arguments...
            delta = obj.get("delta")
            if isinstance(delta, str):
                self.streamed_chars += output_chars(delta)
            return
        if kind == "error":
            self.error = error_message(obj.get("error") if "error" in obj else obj)
            return
        resp = obj.get("response")
        if not isinstance(resp, dict):
            return
        self._note(resp)
        if isinstance(resp.get("usage"), dict):
            self.usage = openai_usage(resp["usage"], self._service_tier)
        if kind in _TERMINAL:
            self.finished = True
            if kind == "response.failed":
                self.error = error_message(resp.get("error") or "response.failed")


class OpenAIAdapter:
    provider = Provider.OPENAI

    def endpoint(self, method: str, path: str) -> Endpoint | None:
        """POST .../chat/completions or .../responses (covers /v1, bare and Codex's
        /backend-api/codex prefixes)."""
        if method.upper() != "POST":
            return None
        path = path.split("?", 1)[0].rstrip("/")
        if path.endswith("/chat/completions"):
            return Endpoint.CHAT
        if path.endswith("/responses"):
            return Endpoint.RESPONSES
        return None

    def parse_request(self, endpoint: Endpoint, body: dict) -> RequestInfo:
        body = as_dict(body)
        if endpoint is Endpoint.CHAT:
            max_out = as_int(body.get("max_completion_tokens"))
            if max_out is None:
                max_out = as_int(body.get("max_tokens"))
            prompt = (body.get("tools"), body.get("functions"), body.get("messages"))
            choices = as_int(body.get("n")) or 1
        else:
            max_out = as_int(body.get("max_output_tokens"))
            # A stored prompt ({"id": ...}) adds server-held content; its variables are sent.
            prompt = (
                body.get("tools"),
                body.get("instructions"),
                body.get("input"),
                body.get("prompt"),
            )
            choices = 1
        _images, document = server_inputs(*prompt)
        stored_prompt = as_str(as_dict(body.get("prompt")).get("id")) is not None
        return RequestInfo(
            provider=self.provider,
            endpoint=endpoint,
            model=as_str(body.get("model")) or "",
            stream=body.get("stream") is True,
            max_output_tokens=max_out,
            est_prompt_tokens=estimate_tokens(*prompt),
            max_prompt_tokens=bound_tokens(*prompt),
            service_tier=as_str(body.get("service_tier")),
            web_searches=web_search_cap(body.get("tools"), ("web_search",)),
            previous_response_id=as_str(body.get("previous_response_id")),
            server_context=body.get("conversation") is not None or stored_prompt or document,
            choices=choices,
        )

    def rewrite_request(
        self,
        endpoint: Endpoint,
        body: dict,
        *,
        inject_stream_usage: bool = False,
        previous_message_id: str | None | EllipsisType = ...,
    ) -> dict | None:
        """Chat streams without ``stream_options.include_usage``: turn it on.

        The only rewrite this adapter makes, so a non-None result means the stream tracker
        must be created with ``injected_usage=True``.
        """
        if not inject_stream_usage or endpoint is not Endpoint.CHAT or not isinstance(body, dict):
            return None
        if body.get("stream") is not True:
            return None
        options = body.get("stream_options")
        if options is None:
            options = {}
        if not isinstance(options, dict) or options.get("include_usage") is True:
            return None
        return {**body, "stream_options": {**options, "include_usage": True}}

    def session_hint(self, headers: Mapping[str, str], body: dict) -> str | None:
        for name in _SESSION_HEADERS:
            value = header(headers, name)
            if value:
                return value
        return None

    def usage_from_json(
        self, endpoint: Endpoint, data: dict
    ) -> tuple[Usage, str | None, str | None]:
        data = as_dict(data)
        usage = openai_usage(data.get("usage"), as_str(data.get("service_tier")))
        return usage, as_str(data.get("model")), as_str(data.get("id"))

    def cache_miss_from_json(self, data: dict) -> tuple[str | None, int | None]:
        return None, None

    def stream_tracker(
        self, endpoint: Endpoint, injected_usage: bool = False
    ) -> ChatStreamTracker | ResponsesStreamTracker:
        if endpoint is Endpoint.CHAT:
            return ChatStreamTracker(strip_usage_chunk=injected_usage)
        return ResponsesStreamTracker()

    def error_body(self, message: str) -> tuple[int, dict[str, str], bytes]:
        body = {
            "error": {
                "message": message,
                "type": "insufficient_quota",
                "param": None,
                "code": "skinflint_budget_exceeded",
            }
        }
        headers = {"content-type": "application/json", "x-should-retry": "false"}
        return 429, headers, json.dumps(body).encode()

    def ratelimit_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        # x-codex-*: ChatGPT plan usage reported to Codex (primary/secondary windows).
        return {
            **headers_with_prefix(headers, "x-ratelimit-"),
            **headers_with_prefix(headers, "x-codex-"),
        }
