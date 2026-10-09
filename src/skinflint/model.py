"""Data types shared by every module. This module imports nothing from the package."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Provider(enum.StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Endpoint(enum.StrEnum):
    """Endpoints skinflint meters. Everything else is passed through unmetered."""

    MESSAGES = "messages"  # Anthropic POST /v1/messages
    CHAT = "chat.completions"  # OpenAI POST /v1/chat/completions
    RESPONSES = "responses"  # OpenAI POST /v1/responses


class State(enum.StrEnum):
    PENDING = "pending"  # admitted; upstream call in flight
    OK = "ok"  # upstream answered 2xx; usage recorded
    ERROR = "error"  # upstream answered non-2xx, or the connection failed
    ABORTED = "aborted"  # client went away before the response finished
    BLOCKED = "blocked"  # refused by a budget rule; never sent upstream
    LOST = "lost"  # left pending by a process that exited without settling it


class Window(enum.StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"  # starts Monday 00:00 local time
    MONTH = "month"
    TOTAL = "total"  # never resets


class Per(enum.StrEnum):
    """How a budget rule groups requests before comparing against its limit."""

    ALL = "all"  # one shared total across every matching request
    SCOPE = "scope"  # a separate total for each scope label
    SESSION = "session"  # a separate total for each session id
    REQUEST = "request"  # each request on its own, checked before it is sent


class Action(enum.StrEnum):
    BLOCK = "block"
    WARN = "warn"


@dataclass(slots=True)
class Usage:
    """Token counts for one request, normalised across providers.

    The four input fields are disjoint and sum to everything the model read.
    OpenAI reports cached tokens as a subset of prompt tokens; adapters subtract them so
    that ``input_tokens`` is always the uncached part.
    """

    input_tokens: int = 0  # uncached input, billed at the base input rate
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output_tokens: int = 0  # includes reasoning / thinking tokens
    reasoning_tokens: int = 0  # subset of output_tokens; informational only
    web_search_requests: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def prompt_tokens(self) -> int:
        """Every input token the model processed, cached or not."""
        return self.input_tokens + self.cache_write + self.cache_read

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        prompt = self.prompt_tokens
        return self.cache_read / prompt if prompt else None

    def is_empty(self) -> bool:
        return self.total_tokens == 0 and self.web_search_requests == 0


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens. Cache fields fall back to provider multipliers when unset."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    # Prompts longer than this many tokens are billed at the long_* rates (whole request).
    long_context_threshold: int | None = None
    long_input: float | None = None
    long_output: float | None = None
    web_search_per_1k: float = 0.0


@dataclass(slots=True)
class Segment:
    """One block of a request body, in the order the provider reads (and caches) it.

    Anthropic order: tools, then system, then messages. OpenAI: tools, instructions /
    system messages, then the rest of the conversation.
    """

    section: str  # "tools" | "system" | "messages"
    kind: str  # see segments.KINDS
    label: str  # specific name, e.g. "Bash", "mcp__github__create_issue", "CLAUDE.md"
    group: str  # roll-up bucket, e.g. "tools: built-in", "tools: mcp github", "tool results: Read"
    chars: int  # length of the block's canonical JSON
    hash: str  # 16 hex chars of sha256 over the block's canonical JSON, cache_control removed
    message: int = -1  # index into messages / input items; -1 outside the messages section
    breakpoint: bool = False  # block carries cache_control (Anthropic)
    ttl: str | None = None  # "5m" | "1h" for breakpoints
    est_tokens: int = 0  # estimated tokens, filled in by segments.calibrate()


@dataclass(slots=True)
class RequestInfo:
    """What the proxy needs to know about a metered request before sending it."""

    provider: Provider
    endpoint: Endpoint
    model: str
    stream: bool
    max_output_tokens: int | None  # max_tokens / max_completion_tokens / max_output_tokens
    est_prompt_tokens: int  # rough pre-flight estimate from the body size
    session_hint: str | None = None  # session id found in the body, if any


@dataclass(slots=True)
class Record:
    """One row of the request ledger."""

    ts: float  # unix seconds when the request arrived
    provider: Provider
    endpoint: Endpoint
    scope: str
    model: str
    state: State
    id: int | None = None
    session: str | None = None
    agent: str | None = None  # fingerprint of the agent's tool set and system prompt
    client: str | None = None  # short client name from the User-Agent, e.g. "claude-cli/2.1.287"
    stream: bool = False
    status: int | None = None  # HTTP status sent to the client
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    cost_estimated: bool = False  # the model had no exact price entry
    reserved_usd: float = 0.0  # held against budgets while pending
    reserved_tokens: int = 0
    duration_ms: int | None = None
    ttft_ms: int | None = None  # time to first upstream byte
    upstream_id: str | None = None  # provider request / message id
    blocked_by: str | None = None  # budget rule name
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BudgetRule:
    name: str
    usd: float | None = None
    tokens: int | None = None  # all tokens: uncached input + cache writes + cache reads + output
    requests: int | None = None
    window: Window = Window.DAY
    per: Per = Per.ALL
    scope: str = "*"  # glob over scope labels
    model: str = "*"  # glob over model ids
    action: Action = Action.BLOCK


@dataclass(slots=True)
class Spend:
    usd: float = 0.0
    tokens: int = 0
    requests: int = 0


@dataclass(slots=True)
class Decision:
    allowed: bool
    message: str = ""  # set when blocked; becomes the API error message
    rule: BudgetRule | None = None  # the rule that blocked
    warnings: list[str] = field(default_factory=list)  # messages from WARN rules
    record_id: int | None = None  # ledger row created on admission
