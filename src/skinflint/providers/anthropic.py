"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
import re
import secrets
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

_LEGACY_SESSION = re.compile(r"_session_([0-9A-Za-z-]{8,64})$")
_STREAM_EVENTS = frozenset(
    {"message_start", "content_block_delta", "message_delta", "message_stop", "error"}
)
_DELTA_TEXT = ("text", "thinking", "partial_json")  # content_block_delta fields that are output
_COUNTS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
_LABELS = ("speed", "service_tier", "inference_geo")


class UsageAccumulator:
    """Merges Anthropic usage objects in arrival order; the last non-null value per field wins.

    The 5m/1h split comes from the latest ``cache_creation`` breakdown; if the write total
    later grows beyond it (server tools), the growth counts as 5m writes.
    """

    __slots__ = ("counts", "labels", "split", "thinking", "web_search")

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.labels: dict[str, str] = {}
        self.split: tuple[int, int] | None = None
        self.thinking: int | None = None
        self.web_search: int | None = None

    def merge(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for key in _COUNTS:
            v = as_int(usage.get(key))
            if v is not None:
                self.counts[key] = v
        for key in _LABELS:
            v = as_str(usage.get(key))
            if v is not None:
                self.labels[key] = v
        cc = usage.get("cache_creation")
        if isinstance(cc, dict):
            self.split = (
                as_int(cc.get("ephemeral_5m_input_tokens")) or 0,
                as_int(cc.get("ephemeral_1h_input_tokens")) or 0,
            )
        v = as_int(as_dict(usage.get("output_tokens_details")).get("thinking_tokens"))
        if v is not None:
            self.thinking = v
        v = as_int(as_dict(usage.get("server_tool_use")).get("web_search_requests"))
        if v is not None:
            self.web_search = v

    def usage(self) -> Usage:
        c = self.counts
        written = c.get("cache_creation_input_tokens")
        if written is None:
            written = sum(self.split) if self.split else 0
        one_hour = min(self.split[1], written) if self.split else 0
        return Usage(
            input_tokens=c.get("input_tokens", 0),
            cache_write_5m=written - one_hour,
            cache_write_1h=one_hour,
            cache_read=c.get("cache_read_input_tokens", 0),
            output_tokens=c.get("output_tokens", 0),
            reasoning_tokens=self.thinking or 0,
            web_search_requests=self.web_search or 0,
            speed=self.labels.get("speed"),
            service_tier=self.labels.get("service_tier"),
            inference_geo=self.labels.get("inference_geo"),
        )


def diagnostics(value: Any) -> tuple[str | None, int | None]:
    """(cache_miss_reason type, cache_missed_input_tokens) from a ``diagnostics`` value."""
    reason = as_dict(value).get("cache_miss_reason")
    if not isinstance(reason, dict):
        return None, None
    return as_str(reason.get("type")), as_int(reason.get("cache_missed_input_tokens"))


class AnthropicStreamTracker:
    """Reads usage from a Messages SSE stream; forwards every byte unchanged."""

    def __init__(self) -> None:
        self._parser = SSEParser()
        self._acc = UsageAccumulator()
        self.usage = Usage()
        self.streamed_chars = 0
        self.model: str | None = None
        self.upstream_id: str | None = None
        self.finished = False
        self.error: str | None = None
        self.cache_miss_reason: str | None = None
        self.cache_missed_tokens: int | None = None

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
        if (ev.event is not None and ev.event not in _STREAM_EVENTS) or not ev.data:
            return
        obj = load_json(ev.data)
        if not isinstance(obj, dict):
            return
        kind = obj.get("type") if ev.event is None else ev.event
        if kind == "content_block_delta":
            delta = as_dict(obj.get("delta"))
            for key in _DELTA_TEXT:
                text = delta.get(key)
                if isinstance(text, str):
                    self.streamed_chars += output_chars(text)
        elif kind == "message_start":
            msg = as_dict(obj.get("message"))
            self.model = as_str(msg.get("model")) or self.model
            self.upstream_id = as_str(msg.get("id")) or self.upstream_id
            self._merge(msg.get("usage"))
            if "diagnostics" in msg:
                self._diagnostics(msg["diagnostics"])
        elif kind == "message_delta":
            self._merge(obj.get("usage"))
            if obj.get("diagnostics") is not None:
                self._diagnostics(obj["diagnostics"])
        elif kind == "message_stop":
            self.finished = True
        elif kind == "error":
            self.error = error_message(obj.get("error"))

    def _merge(self, usage: Any) -> None:
        self._acc.merge(usage)
        self.usage = self._acc.usage()

    def _diagnostics(self, value: Any) -> None:
        reason, tokens = diagnostics(value)
        if reason is not None:
            self.cache_miss_reason, self.cache_missed_tokens = reason, tokens


class AnthropicAdapter:
    provider = Provider.ANTHROPIC

    def endpoint(self, method: str, path: str) -> Endpoint | None:
        path = path.split("?", 1)[0].rstrip("/")
        if method.upper() == "POST" and path == "/v1/messages":
            return Endpoint.MESSAGES
        return None

    def parse_request(self, endpoint: Endpoint, body: dict) -> RequestInfo:
        body = as_dict(body)
        prompt = (body.get("tools"), body.get("system"), body.get("messages"))
        return RequestInfo(
            provider=self.provider,
            endpoint=endpoint,
            model=as_str(body.get("model")) or "",
            stream=body.get("stream") is True,
            max_output_tokens=as_int(body.get("max_tokens")),
            est_prompt_tokens=estimate_tokens(*prompt),
            session_hint=_metadata_session(body),
            max_prompt_tokens=bound_tokens(*prompt),
            speed=as_str(body.get("speed")),
            service_tier=as_str(body.get("service_tier")),
            inference_geo=as_str(body.get("inference_geo")),
            web_searches=web_search_cap(body.get("tools"), ("web_search",)),
            server_context=server_inputs(*prompt)[1],
        )

    def rewrite_request(
        self,
        endpoint: Endpoint,
        body: dict,
        *,
        inject_stream_usage: bool = False,
        previous_message_id: str | None | EllipsisType = ...,
    ) -> dict | None:
        """Add ``diagnostics.previous_message_id`` unless told not to (Ellipsis) or present."""
        if (
            previous_message_id is ...
            or endpoint is not Endpoint.MESSAGES
            or not isinstance(body, dict)
            or "diagnostics" in body
        ):
            return None
        return {**body, "diagnostics": {"previous_message_id": previous_message_id}}

    def session_hint(self, headers: Mapping[str, str], body: dict) -> str | None:
        return header(headers, "x-claude-code-session-id") or _metadata_session(body)

    def usage_from_json(
        self, endpoint: Endpoint, data: dict
    ) -> tuple[Usage, str | None, str | None]:
        data = as_dict(data)
        acc = UsageAccumulator()
        acc.merge(data.get("usage"))
        return acc.usage(), as_str(data.get("model")), as_str(data.get("id"))

    def cache_miss_from_json(self, data: dict) -> tuple[str | None, int | None]:
        """(cache_miss_reason type, cache_missed_input_tokens) from a non-streaming body."""
        return diagnostics(as_dict(data).get("diagnostics"))

    def stream_tracker(
        self, endpoint: Endpoint, injected_usage: bool = False
    ) -> AnthropicStreamTracker:
        return AnthropicStreamTracker()

    def error_body(self, message: str) -> tuple[int, dict[str, str], bytes]:
        request_id = "skinflint_" + secrets.token_hex(12)
        body = {
            "type": "error",
            "error": {"type": "billing_error", "message": message},
            "request_id": request_id,
        }
        headers = {
            "content-type": "application/json",
            "x-should-retry": "false",
            "request-id": request_id,
        }
        return 402, headers, json.dumps(body).encode()

    def ratelimit_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        return headers_with_prefix(headers, "anthropic-ratelimit-")


def _metadata_session(body: Any) -> str | None:
    user_id = as_dict(as_dict(body).get("metadata")).get("user_id")
    if isinstance(user_id, str):
        text = user_id.strip()
        if text.startswith("{"):
            user_id = load_json(text)
        else:
            m = _LEGACY_SESSION.search(text)
            return m.group(1) if m else None
    if isinstance(user_id, dict):
        return as_str(user_id.get("session_id"))
    return None
