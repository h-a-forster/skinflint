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
    error_message,
    estimate_tokens,
    header,
    headers_with_prefix,
    load_json,
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
_TERMINAL = frozenset({"response.completed", "response.failed", "response.incomplete"})
_SESSION_HEADERS = ("session_id", "x-session-id")


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
            wanted = (
                self.model is None
                or ('"usage"' in data and not _USAGE_NULL.search(data))
                or '"error"' in data
            )
            if not wanted:
                return False
            obj = load_json(data)
            if not isinstance(obj, dict):
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
        if (ev.event is not None and ev.event not in _RESPONSE_EVENTS) or not ev.data:
            return
        obj = load_json(ev.data)
        if not isinstance(obj, dict):
            return
        kind = as_str(obj.get("type")) or ev.event
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
            est = estimate_tokens(body.get("tools"), body.get("functions"), body.get("messages"))
        else:
            max_out = as_int(body.get("max_output_tokens"))
            est = estimate_tokens(body.get("tools"), body.get("instructions"), body.get("input"))
        return RequestInfo(
            provider=self.provider,
            endpoint=endpoint,
            model=as_str(body.get("model")) or "",
            stream=body.get("stream") is True,
            max_output_tokens=max_out,
            est_prompt_tokens=est,
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
        return headers_with_prefix(headers, "x-ratelimit-")
