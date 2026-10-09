"""Split a request body into ordered segments and attribute prompt tokens to them."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from skinflint.model import Endpoint, Provider, Segment

KINDS = {
    "tool",
    "server_tool",
    "system",
    "instructions",
    "reminder",
    "user_text",
    "assistant_text",
    "thinking",
    "tool_use",
    "tool_result",
    "image",
    "document",
    "other",
}

BILLING_LABEL = "billing header"
IMAGE_TOKENS = 1600

CHARS_PER_TOKEN = {
    "tool": 3.2,
    "server_tool": 3.2,
    "tool_use": 3.2,
    "tool_result": 3.5,
    "other": 3.5,
}
PROSE_CHARS_PER_TOKEN = 4.0
# Images cost a bounded number of tokens whatever their encoded size. Documents scale with
# their content, so they are estimated from size like everything else.
FIXED_TOKENS = {"image": IMAGE_TOKENS}
_MEDIA = {
    "image": "image",
    "image_url": "image",
    "input_image": "image",
    "document": "document",
    "file": "document",
    "input_file": "document",
}

_REMINDER_PREFIXES = ("<system-reminder>", "<environment_context>", "<user_instructions>")
_INSTRUCTION_FILE = re.compile(r"Contents of ([^\n]*?\.(?:md|mdc))(?=[\s:(]|$)")
_AGENTS_HEADER = re.compile(r"^#\s*([\w.-]+\.md) instructions for", re.M)
_REMINDER_RULES: tuple[tuple[str, str], ...] = (
    ("skills are available", "skills"),
    ("mcp server instructions", "mcp instructions"),
    ("agent types", "agent types"),
    ("deferred tools", "deferred tools"),
    ("# environment", "environment"),
    ("<environment_context>", "environment"),
    ("<env>", "environment"),
    ("date is", "date"),
    ("powered by the model", "model"),
    ("attribution", "attribution"),
    ("answer the user's questions", "user context"),
    ("todo", "todos"),
    ("plan mode", "plan mode"),
    ("hook", "hooks"),
    ("memory", "memory"),
)


def canonical(obj: Any) -> str:
    """Canonical JSON of a block: sorted keys, compact, cache_control removed."""
    return json.dumps(
        _strip_cache_control(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _strip_cache_control(obj: Any) -> Any:
    if isinstance(obj, dict):
        base = obj
        if "cache_control" in obj:
            base = {k: v for k, v in obj.items() if k != "cache_control"}
        out = None
        for k, v in base.items():
            if isinstance(v, dict | list):
                nv = _strip_cache_control(v)
                if nv is not v:
                    if out is None:
                        out = dict(base)
                    out[k] = nv
        return base if out is None else out
    if isinstance(obj, list):
        new = None
        for i, v in enumerate(obj):
            if isinstance(v, dict | list):
                nv = _strip_cache_control(v)
                if nv is not v:
                    if new is None:
                        new = list(obj)
                    new[i] = nv
        return obj if new is None else new
    return obj


def _make(
    section: str,
    kind: str,
    label: str,
    group: str,
    block: Any,
    message: int = -1,
) -> Segment:
    text = canonical(block)
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    seg = Segment(section, kind, label, group, len(text), digest, message)
    cc = block.get("cache_control") if isinstance(block, dict) else None
    if isinstance(cc, dict):
        seg.breakpoint = True
        seg.ttl = cc.get("ttl") or "5m"
    return seg


def mcp_server(name: str) -> str | None:
    """'mcp__github__create_issue' -> 'github'."""
    if not name.startswith("mcp__"):
        return None
    rest = name[5:]
    server, sep, _ = rest.partition("__")
    return server if sep and server else None


def _tool_group(name: str) -> str:
    server = mcp_server(name)
    return f"tools: mcp {server}" if server else "tools: built-in"


def classify_text(text: str) -> tuple[str, str, str] | None:
    """(kind, label, group) for injected context blocks, None for plain user text."""
    head = text.lstrip()[:400]
    if not head.startswith(_REMINDER_PREFIXES) and not _AGENTS_HEADER.match(head):
        return None
    low = head.lower()
    if (
        "contents of " in text
        or "instructions are shown below" in low
        or "<user_instructions>" in low
    ):
        return "instructions", _instruction_label(text), "instruction files"
    m = _AGENTS_HEADER.match(head)
    if m:
        return "instructions", m.group(1), "instruction files"
    for needle, label in _REMINDER_RULES:
        if needle in low:
            return "reminder", label, f"context: {label}"
    return "reminder", "other", "context: other"


def _instruction_label(text: str) -> str:
    names: list[str] = []
    for m in _INSTRUCTION_FILE.finditer(text):
        name = re.split(r"[\\/]", m.group(1).strip())[-1]
        if name and name not in names:
            names.append(name)
    for m in _AGENTS_HEADER.finditer(text):
        if m.group(1) not in names:
            names.append(m.group(1))
    return ", ".join(names) if names else "instructions"


def _user_text(text: str) -> tuple[str, str, str]:
    return classify_text(text) or ("user_text", "user prompt", "user prompts")


def segment(provider: Provider, endpoint: Endpoint, body: dict) -> list[Segment]:
    """Segments of a request body in the order the provider reads and caches them."""
    if not isinstance(body, dict):
        return []
    if provider == Provider.ANTHROPIC:
        segs = _anthropic(body)
    elif endpoint == Endpoint.RESPONSES:
        segs = _responses(body)
    else:
        segs = _chat(body)
    return segs


# Anthropic


def _anthropic(body: dict) -> list[Segment]:
    segs: list[Segment] = []
    for tool in _as_list(body.get("tools")):
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type")
        name = str(tool.get("name") or ttype or "tool")
        if ttype and ttype != "custom":
            segs.append(_make("tools", "server_tool", name, "tools: server", tool))
        else:
            segs.append(_make("tools", "tool", name, _tool_group(name), tool))

    system = body.get("system")
    if isinstance(system, str):
        system = [{"type": "text", "text": system}] if system else []
    for i, block in enumerate(_as_list(system)):
        if not isinstance(block, dict):
            block = {"type": "text", "text": str(block)}
        text = block.get("text")
        if isinstance(text, str) and text.startswith("x-anthropic-billing-header:"):
            label = BILLING_LABEL
        else:
            label = f"system[{i}]"
        segs.append(_make("system", "system", label, "system prompt", block))

    tool_names: dict[str, str] = {}
    for mi, msg in enumerate(_as_list(body.get("messages"))):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in _as_list(content):
            if not isinstance(block, dict):
                block = {"type": "text", "text": str(block)}
            segs.extend(_anthropic_block(role, block, mi, tool_names))

    auto = body.get("cache_control")
    if isinstance(auto, dict) and segs and not segs[-1].breakpoint:
        segs[-1].breakpoint = True
        segs[-1].ttl = auto.get("ttl") or "5m"
    return segs


def _split_media(
    block: dict, key: str, section: str, kind: str, name: str, group: str, mi: int
) -> list[Segment]:
    """A tool result as its text segment followed by one fixed-size segment per image/file.

    The breakpoint of the enclosing block moves to the last segment emitted for it.
    """
    content = block.get(key)
    if not isinstance(content, list):
        return [_make(section, kind, name, group, block, mi)]
    rest, media = [], []
    for part in content:
        mkind = _MEDIA.get(part.get("type")) if isinstance(part, dict) else None
        (media if mkind else rest).append(part)
    if not media:
        return [_make(section, kind, name, group, block, mi)]
    segs = [_make(section, kind, name, group, {**block, key: rest}, mi)]
    for part in media:
        mkind = _MEDIA[part["type"]]
        segs.append(_make(section, mkind, f"tool_result:{name} {mkind}", f"{mkind}s", part, mi))
    head = segs[0]
    if head.breakpoint:
        segs[-1].breakpoint, segs[-1].ttl = True, head.ttl
        head.breakpoint, head.ttl = False, None
    return segs


def _anthropic_block(role: Any, block: dict, mi: int, tool_names: dict[str, str]) -> list[Segment]:
    btype = block.get("type")
    if btype == "text":
        text = block.get("text") or ""
        if role == "assistant":
            kind, label, group = "assistant_text", "assistant", "assistant text"
        elif role == "system":
            kind, label, group = "system", "system message", "system prompt"
        else:
            kind, label, group = _user_text(text)
    elif btype in ("thinking", "redacted_thinking"):
        kind, label, group = "thinking", btype, "thinking"
    elif btype in ("tool_use", "server_tool_use", "mcp_tool_use"):
        name = str(block.get("name") or "tool")
        if block.get("id"):
            tool_names[str(block["id"])] = name
        kind, label, group = "tool_use", name, "tool calls"
    elif btype == "tool_result" or (isinstance(btype, str) and btype.endswith("_tool_result")):
        name = tool_names.get(str(block.get("tool_use_id")))
        if name is None:
            name = btype.removesuffix("_tool_result") if btype != "tool_result" else "unknown"
        return _split_media(
            block, "content", "messages", "tool_result", name, f"tool results: {name}", mi
        )
    elif btype == "image":
        kind, label, group = "image", "image", "images"
    elif btype == "document":
        kind, label, group = "document", str(block.get("title") or btype), "documents"
    elif btype == "search_result":
        kind, label, group = "other", str(block.get("title") or btype), "documents"
    else:
        kind, label, group = "other", str(btype or "block"), "other"
    return [_make("messages", kind, label, group, block, mi)]


# OpenAI


def _openai_tool(tool: Any) -> Segment | None:
    if not isinstance(tool, dict):
        return None
    ttype = tool.get("type") or "function"
    fn = tool.get("function")
    name = tool.get("name") or (fn.get("name") if isinstance(fn, dict) else None)
    if ttype in ("function", "custom"):
        name = str(name or ttype)
        return _make("tools", "tool", name, _tool_group(name), tool)
    if ttype == "mcp":
        server = str(tool.get("server_label") or "mcp")
        return _make("tools", "server_tool", server, f"tools: mcp {server}", tool)
    return _make("tools", "server_tool", str(name or ttype), "tools: server", tool)


def _chat(body: dict) -> list[Segment]:
    segs = [s for s in map(_openai_tool, _as_list(body.get("tools"))) if s is not None]
    tool_names: dict[str, str] = {}
    sys_i = 0
    for mi, msg in enumerate(_as_list(body.get("messages"))):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if role in ("system", "developer"):
            for part in _as_list(parts):
                segs.append(
                    _make(
                        "system",
                        "system",
                        f"{role}[{sys_i}]",
                        "system prompt",
                        _with_role(role, part),
                    )
                )
                sys_i += 1
            continue
        if role in ("tool", "function"):
            call_id = str(msg.get("tool_call_id") or "")
            name = tool_names.get(call_id) or str(msg.get("name") or "unknown")
            segs.extend(
                _split_media(
                    msg, "content", "messages", "tool_result", name, f"tool results: {name}", mi
                )
            )
            continue
        for part in _as_list(parts):
            segs.append(_openai_part(role, part, mi))
        for call in _as_list(msg.get("tool_calls")):
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or call.get("type") or "tool")
            if call.get("id"):
                tool_names[str(call["id"])] = name
            segs.append(_make("messages", "tool_use", name, "tool calls", call, mi))
    return segs


def _with_role(role: str, part: Any) -> dict:
    if isinstance(part, dict):
        return {"role": role, **part}
    return {"role": role, "text": str(part)}


def _openai_part(role: str, part: Any, mi: int) -> Segment:
    if not isinstance(part, dict):
        part = {"type": "text", "text": str(part)}
    ptype = part.get("type") or "text"
    block = _with_role(role, part)
    if ptype in ("text", "input_text", "output_text"):
        text = part.get("text") or ""
        if role == "assistant":
            return _make("messages", "assistant_text", "assistant", "assistant text", block, mi)
        kind, label, group = _user_text(text if isinstance(text, str) else "")
        return _make("messages", kind, label, group, block, mi)
    if ptype == "refusal":
        return _make("messages", "assistant_text", "refusal", "assistant text", block, mi)
    if ptype in ("image_url", "input_image", "image"):
        return _make("messages", "image", "image", "images", part, mi)
    if ptype in ("file", "input_file"):
        return _make("messages", "document", "file", "documents", part, mi)
    return _make("messages", "other", str(ptype), "other", block, mi)


def _responses(body: dict) -> list[Segment]:
    segs = [s for s in map(_openai_tool, _as_list(body.get("tools"))) if s is not None]
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        block = {"type": "instructions", "text": instructions}
        segs.append(_make("system", "system", "instructions", "system prompt", block))
    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    tool_names: dict[str, str] = {}
    sys_i = 0
    for mi, item in enumerate(_as_list(items)):
        if not isinstance(item, dict):
            continue
        itype = item.get("type") or ("message" if "role" in item else "other")
        if itype == "message":
            role = str(item.get("role") or "user")
            content = item.get("content")
            parts = (
                [{"type": "input_text", "text": content}] if isinstance(content, str) else content
            )
            for part in _as_list(parts):
                if role in ("system", "developer"):
                    segs.append(
                        _make(
                            "system",
                            "system",
                            f"{role}[{sys_i}]",
                            "system prompt",
                            _with_role(role, part),
                        )
                    )
                    sys_i += 1
                else:
                    segs.append(_openai_part(role, part, mi))
        elif itype in ("function_call", "custom_tool_call", "local_shell_call", "mcp_call"):
            name = str(item.get("name") or itype.removesuffix("_call"))
            if item.get("call_id"):
                tool_names[str(item["call_id"])] = name
            segs.append(_make("messages", "tool_use", name, "tool calls", item, mi))
        elif itype.endswith("_call_output"):
            name = tool_names.get(str(item.get("call_id"))) or "unknown"
            segs.extend(
                _split_media(
                    item, "output", "messages", "tool_result", name, f"tool results: {name}", mi
                )
            )
        elif itype == "reasoning":
            segs.append(_make("messages", "thinking", "reasoning", "thinking", item, mi))
        elif itype.endswith("_call"):
            segs.append(_make("messages", "tool_use", itype, "tool calls", item, mi))
        else:
            segs.append(_make("messages", "other", itype, "other", item, mi))
    return segs


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return []


# Token attribution


def _raw_estimate(seg: Segment) -> float:
    ratio = CHARS_PER_TOKEN.get(seg.kind, PROSE_CHARS_PER_TOKEN)
    return seg.chars / ratio


def estimate(segments: list[Segment], prompt_tokens: int = 0) -> list[int]:
    """Estimated tokens per segment, scaled to sum to prompt_tokens when it is > 0."""
    fixed = [FIXED_TOKENS.get(s.kind) for s in segments]
    raw = [0.0 if f is not None else _raw_estimate(s) for s, f in zip(segments, fixed, strict=True)]
    if prompt_tokens <= 0 or not segments:
        return [f if f is not None else round(r) for f, r in zip(fixed, raw, strict=True)]
    fixed_total = sum(f for f in fixed if f is not None)
    raw_total = sum(raw)
    if fixed_total > prompt_tokens or (raw_total == 0 and fixed_total > 0):
        weights = [float(f) if f is not None else r for f, r in zip(fixed, raw, strict=True)]
        return _largest_remainder(weights, prompt_tokens)
    target = prompt_tokens - fixed_total
    if raw_total == 0:
        raw = [0.0] * (len(segments) - 1) + [1.0]
    scaled = _largest_remainder(raw, target)
    return [f if f is not None else v for f, v in zip(fixed, scaled, strict=True)]


def _largest_remainder(weights: list[float], total: int) -> list[int]:
    wsum = sum(weights)
    if wsum <= 0:
        out = [0] * len(weights)
        if out:
            out[-1] = total
        return out
    exact = [w * total / wsum for w in weights]
    out = [int(x) for x in exact]
    short = total - sum(out)
    order = sorted(range(len(exact)), key=lambda i: exact[i] - out[i], reverse=True)
    for i in order[:short]:
        out[i] += 1
    return out


def calibrate(segments: list[Segment], prompt_tokens: int) -> None:
    """Fill est_tokens; the total equals prompt_tokens exactly when prompt_tokens > 0."""
    for seg, n in zip(segments, estimate(segments, prompt_tokens), strict=True):
        seg.est_tokens = n


def fingerprint(segments: list[Segment]) -> str:
    """8 hex chars identifying an agent: sorted tool names + system blocks (no billing header)."""
    tools = sorted(s.label for s in segments if s.section == "tools")
    system = [s.hash for s in segments if s.section == "system" and s.label != BILLING_LABEL]
    text = json.dumps([tools, system], separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:8]
