"""Adapter and stream-tracker protocols plus helpers shared by the provider adapters."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from types import EllipsisType
from typing import Any, Protocol

from skinflint.model import Endpoint, Provider, RequestInfo, Usage

CHARS_PER_TOKEN = 4
MEDIA_CHARS = 1600 * CHARS_PER_TOKEN  # an image or other base64 payload counts as ~1.6k tokens


class StreamTracker(Protocol):
    usage: Usage  # best-known usage so far
    model: str | None
    upstream_id: str | None
    finished: bool  # saw the provider's terminal event
    error: str | None  # error event message seen in the stream, if any
    cache_miss_reason: str | None
    cache_missed_tokens: int | None

    def feed(self, chunk: bytes) -> bytes:
        """Observe upstream bytes; return the bytes to forward to the client."""
        ...

    def close(self) -> bytes:
        """Flush anything held back."""
        ...


class Adapter(Protocol):
    provider: Provider

    def endpoint(self, method: str, path: str) -> Endpoint | None: ...

    def parse_request(self, endpoint: Endpoint, body: dict) -> RequestInfo: ...

    def rewrite_request(
        self,
        endpoint: Endpoint,
        body: dict,
        *,
        inject_stream_usage: bool = False,
        previous_message_id: str | None | EllipsisType = ...,
    ) -> dict | None: ...

    def session_hint(self, headers: Mapping[str, str], body: dict) -> str | None: ...

    def usage_from_json(
        self, endpoint: Endpoint, data: dict
    ) -> tuple[Usage, str | None, str | None]: ...

    def cache_miss_from_json(self, data: dict) -> tuple[str | None, int | None]: ...

    def stream_tracker(self, endpoint: Endpoint, injected_usage: bool) -> StreamTracker: ...

    def error_body(self, message: str) -> tuple[int, dict[str, str], bytes]: ...

    def ratelimit_headers(self, headers: Mapping[str, str]) -> dict[str, str]: ...


def header(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Case-insensitive header lookup that works for dicts and aiohttp's CIMultiDict."""
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is not None:
            return str(value)
        lname = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lname:
                return str(value)
    except Exception:
        return None
    return None


def headers_with_prefix(headers: Mapping[str, str] | None, prefix: str) -> dict[str, str]:
    """Headers whose lower-cased name starts with ``prefix``, keyed by lower-cased name."""
    out: dict[str, str] = {}
    if not headers:
        return out
    try:
        for key, value in headers.items():
            lname = str(key).lower()
            if lname.startswith(prefix):
                out[lname] = str(value)
    except Exception:
        return out
    return out


def as_int(value: Any) -> int | None:
    """A non-negative int from untrusted JSON, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def text_chars(obj: Any) -> int:
    """Rough character count of a JSON value, with base64 media counted as a fixed size."""
    total = 0
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, str):
            if len(o) > 1000 and o.startswith("data:") and ";base64," in o[:100]:
                total += MEDIA_CHARS
            else:
                total += len(o) + 2
        elif isinstance(o, dict):
            if o.get("type") == "base64" and isinstance(o.get("data"), str):
                total += MEDIA_CHARS
                continue
            total += 2
            for k, v in o.items():
                total += len(k) + 4
                stack.append(v)
        elif isinstance(o, list):
            total += 2 + len(o)
            stack.extend(o)
        else:
            total += 4
    return total


def estimate_tokens(*parts: Any) -> int:
    return sum(text_chars(p) for p in parts if p is not None) // CHARS_PER_TOKEN


def error_message(err: Any) -> str:
    """'type: message' from an error object (or a bare string)."""
    if isinstance(err, str):
        return err
    err = as_dict(err)
    message = err.get("message")
    kind = err.get("code") or err.get("type")
    message = str(message) if message is not None else "unknown error"
    return f"{kind}: {message}" if isinstance(kind, str) and kind else message
