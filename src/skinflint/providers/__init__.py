"""Provider adapters: metered endpoints, usage extraction, stream tracking, refusals."""

from __future__ import annotations

from collections.abc import Mapping

from skinflint.model import Provider
from skinflint.providers.anthropic import AnthropicAdapter
from skinflint.providers.base import Adapter, StreamTracker, header
from skinflint.providers.openai import OpenAIAdapter

__all__ = [
    "ADAPTERS",
    "Adapter",
    "StreamTracker",
    "client_name",
    "detect",
]

ADAPTERS: dict[Provider, Adapter] = {
    Provider.ANTHROPIC: AnthropicAdapter(),
    Provider.OPENAI: OpenAIAdapter(),
}


def detect(path: str, headers: Mapping[str, str]) -> Provider:
    """Anthropic if an anthropic-version header is present or the path is an Anthropic one."""
    if header(headers, "anthropic-version") is not None:
        return Provider.ANTHROPIC
    if path.startswith(("/v1/messages", "/v1/complete", "/api/hello")):
        return Provider.ANTHROPIC
    return Provider.OPENAI


def client_name(user_agent: str | None) -> str | None:
    """First product token of a User-Agent: 'claude-cli/2.1.287 (external, ...)' ->
    'claude-cli/2.1.287'."""
    if not user_agent:
        return None
    parts = user_agent.strip().split(None, 1)
    return parts[0][:64] if parts else None
