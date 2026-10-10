"""Adapter and stream-tracker protocols plus helpers shared by the provider adapters."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import replace
from types import EllipsisType
from typing import Any, Protocol

from skinflint.model import Endpoint, Provider, RequestInfo, Usage

CHARS_PER_TOKEN = 4
MEDIA_CHARS = 1600 * CHARS_PER_TOKEN  # an image or other base64 payload counts as ~1.6k tokens
# The worst-case bound. 4 chars per token runs ~7% low on Haiku 4.5's tokenizer (the newer
# Sonnet 5.5 / Opus 5.5 tokenizer produced fewer tokens for the same request in our
# measurements); the bound still assumes 2.5 chars per token, leaving room for a tokenizer
# ~35% hungrier than Haiku 4.5's, and high-resolution images (~4.8k tokens each).
BOUND_CHARS_PER_TOKEN = 2.5
BOUND_MEDIA_TOKENS = 5000
# Output of a stream that ended before its final usage: generated text, code and JSON run
# 3-4 chars per token, so 3 errs high.
OUTPUT_CHARS_PER_TOKEN = 3


def output_chars(text: str) -> int:
    """Weighted length of generated text, in OUTPUT_CHARS_PER_TOKEN units per token. Byte-level
    BPE tokens are at least one byte, so non-ASCII text (CJK, emoji) cannot run above one
    token per byte, while ASCII runs 3-4 chars per token: each ASCII char counts 1 and each
    UTF-8 byte of anything else counts OUTPUT_CHARS_PER_TOKEN."""
    if text.isascii():
        return len(text)
    n_ascii = len(text.encode("ascii", "ignore"))
    n_bytes = len(text.encode("utf-8"))
    return n_ascii + OUTPUT_CHARS_PER_TOKEN * (n_bytes - n_ascii)


class StreamTracker(Protocol):
    usage: Usage  # best-known usage so far
    streamed_chars: int  # generated content (text, thinking, tool input) seen, as output_chars
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


def text_chars(obj: Any, media_chars: int = MEDIA_CHARS) -> int:
    """Rough character count of a JSON value, with base64 media counted as a fixed size."""
    total = 0
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, str):
            if len(o) > 1000 and o.startswith("data:") and ";base64," in o[:100]:
                total += media_chars
            else:
                total += len(o) + 2
        elif isinstance(o, dict):
            if o.get("type") == "base64" and isinstance(o.get("data"), str):
                total += media_chars
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


def bound_tokens(*parts: Any) -> int:
    """A conservative prompt size for the worst-case reservation (see BOUND_CHARS_PER_TOKEN),
    with images sent by reference counted as BOUND_MEDIA_TOKENS each. Base64 documents (PDFs)
    are counted like one image, so a many-page PDF can exceed it; documents sent by reference
    are not counted here (see server_inputs)."""
    media = int(BOUND_MEDIA_TOKENS * BOUND_CHARS_PER_TOKEN)
    chars = sum(text_chars(p, media) for p in parts if p is not None)
    images, _document = server_inputs(*parts)
    return math.ceil(chars / BOUND_CHARS_PER_TOKEN) + images * BOUND_MEDIA_TOKENS


def _remote(url: Any) -> bool:
    return isinstance(url, str) and bool(url) and not url.startswith("data:")


def server_inputs(*parts: Any) -> tuple[int, bool]:
    """Inputs the body names by reference instead of carrying: (images, any document). An
    image by URL or file id is counted as one high-resolution image (BOUND_MEDIA_TOKENS);
    a document or file by URL or file id has no size we can see, so it is unbounded.

    Anthropic: image / document blocks whose ``source.type`` is ``url`` or ``file``.
    OpenAI Chat: ``image_url`` parts with a non-data URL, ``file`` parts with a ``file_id``.
    OpenAI Responses: ``input_image`` with ``image_url`` or ``file_id``, ``input_file`` with
    ``file_id`` or ``file_url``."""
    images, document = 0, False
    stack = list(parts)
    while stack:
        o = stack.pop()
        if isinstance(o, list):
            stack.extend(o)
            continue
        if not isinstance(o, dict):
            continue
        kind = o.get("type")
        if kind in ("image", "document"):
            source = as_dict(o.get("source"))
            if source.get("type") in ("url", "file"):
                if kind == "image":
                    images += 1
                else:
                    document = True
                continue
        elif kind == "image_url":
            if _remote(as_dict(o.get("image_url")).get("url")):
                images += 1
                continue
        elif kind == "file":
            if as_str(as_dict(o.get("file")).get("file_id")):
                document = True
                continue
        elif kind == "input_image":
            if as_str(o.get("file_id")) or _remote(o.get("image_url")):
                images += 1
                continue
        elif kind == "input_file":
            if as_str(o.get("file_id")) or as_str(o.get("file_url")):
                document = True
                continue
        stack.extend(o.values())
    return images, document


def web_search_cap(tools: Any, prefixes: tuple[str, ...]) -> int | None:
    """Most web searches the request's server tools allow: the sum of their ``max_uses``,
    0 without a web search tool, None when one sets no cap."""
    total = 0
    for tool in tools if isinstance(tools, list) else []:
        kind = as_str(as_dict(tool).get("type")) or ""
        if not kind.startswith(prefixes):
            continue
        cap = as_int(tool.get("max_uses"))
        if cap is None:
            return None
        total += cap
    return total


def error_message(err: Any) -> str:
    """'type: message' from an error object (or a bare string)."""
    if isinstance(err, str):
        return err
    err = as_dict(err)
    message = err.get("message")
    kind = err.get("code") or err.get("type")
    message = str(message) if message is not None else "unknown error"
    return f"{kind}: {message}" if isinstance(kind, str) and kind else message


def estimate_unfinished(usage: Usage, streamed_chars: int, prompt_tokens: int) -> Usage | None:
    """Usage for a stream that ended before the provider's final usage: output from the
    content streamed so far, input from the prompt bound when the stream reported none.
    None when the reported usage already covers both."""
    output = max(usage.output_tokens, math.ceil(streamed_chars / OUTPUT_CHARS_PER_TOKEN))
    no_input = usage.prompt_tokens == 0 and prompt_tokens > 0
    if output == usage.output_tokens and not no_input:
        return None
    return replace(
        usage,
        output_tokens=output,
        input_tokens=prompt_tokens if no_input else usage.input_tokens,
    )
