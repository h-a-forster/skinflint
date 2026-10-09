# skinflint

[![CI](../../actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A local proxy for the Anthropic and OpenAI APIs. It puts hard spend caps on coding agents,
shows where every prompt token goes, and explains each prompt-cache miss.

Point Claude Code, Codex or any SDK at `http://127.0.0.1:4100`. Requests pass through
unchanged. Each one is checked against your budgets first and recorded after.

## Why

- **Agents spend unattended.** Provider spend limits are per organisation and per month.
  Nothing stops one runaway session or one CI job at $5.
- **Most of the prompt is not your prompt.** Through skinflint, Claude Code 2.1.287 sent
  57,818 tokens to answer `Reply with exactly: hi`. 79% were tool definitions; 41 of the 42
  tools were never called. See [results](docs/results.md).
- **Cache misses are silent.** One changed tool schema or a timestamp in the system prompt
  re-bills the whole prefix at the cache-write rate. The response only shows a bigger number.

## What it does

- **Hard caps.** Budgets per day, hour, week, month or forever; per scope, per session or per
  request; in dollars, tokens or requests. Over the cap, the request is refused with the
  provider's own error format and `x-should-retry: false`, so clients stop instead of
  retrying. Checks and reservations are atomic, so parallel agents cannot overshoot together.
- **Context profiler.** Splits each request into tool definitions, MCP servers, system
  prompt, instruction files, skills lists, tool results and messages, and attributes the real
  token count and cost to each. Flags tools and MCP servers that are sent on every request and
  never called.
- **Cache doctor.** Compares each request with its predecessor and names the block that broke
  the cached prefix, or reports that the TTL expired, and what the miss cost. Uses Anthropic's
  cache diagnostics when available.
- **Ledger.** Every request in SQLite: tokens by cache class, cost, latency, session, agent.
  `report`, `sessions`, `budget`, and a Claude Code status line.

Depends only on aiohttp (plus `backports.zstd` before Python 3.14). No telemetry. API keys are
never stored or logged.

## Quick start

```sh
uv tool install skinflint      # or: pipx install skinflint
skinflint init                 # writes ~/.skinflint/config.toml with a $20/day cap
skinflint serve
```

In another shell:

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:4100
claude
```

Or wrap one command with its own proxy and cap:

```sh
skinflint run --cap 2 -- claude -p "fix the failing test"
```

Then:

```sh
skinflint report                  # spend by model, last 24h
skinflint profile --session last  # where the tokens went
skinflint cache --session last    # why the cache missed
```

Setup for Codex, the SDKs and the status line: [docs/clients.md](docs/clients.md).

## Example

A Claude Code session, as seen by `skinflint profile --session last`:

```text
session 2d6d2aaa-7346-479e-9b9e-200680e3ed16  3 requests  prompt 120k  prompt cost $0.0227

group                             tokens  share      cost
--------------------------------  ------  -----  --------
tools: built-in                    91.6k    76%   $0.0092
system prompt                      17.1k    14%   $0.0037
context: skills                     3.7k     3%   $0.0039
tools: mcp claude_ai_Claude_Docs    2.8k     2%   $0.0003
context: agent types                1.6k     1%   $0.0017
instruction files                     1k     1%   $0.0011
...

suggestions:
  - MCP server 'claude_ai_Claude_Docs' adds 1.4k tokens to each of 2 requests (2.8k tokens, $0.0003) and was never called.
  - 33 built-in tools were never called; together they add 45.1k tokens to each request ($0.0090 this session).
```

When a cap is reached, Claude Code shows the refusal and stops:

```text
API Error: 402 skinflint: budget 'session' reached: $5.01 of $5.00 used this session (3af4ca3c-...).
```

## Budgets

```toml
# ~/.skinflint/config.toml
[[budget]]
name = "daily"
usd = 20
window = "day"

[[budget]]
name = "session"
usd = 5
per = "session"
window = "total"

[[budget]]
name = "opus-hourly"
usd = 10
window = "hour"
model = "claude-opus-*"
```

Every option: [docs/configuration.md](docs/configuration.md).

## Commands

| Command | Does |
|---|---|
| `serve` | Run the proxy. |
| `run [--cap USD] -- CMD` | Run one command behind a private proxy, optionally capped. |
| `env` | Print the environment variables that point clients at the proxy. |
| `report` | Spend and tokens by model, scope, session, day, agent or client. |
| `sessions` | Recent sessions with cost and cache hit rate. |
| `profile [ID]`, `profile --session ID` | Token and cost breakdown of a request or a session. |
| `diff A B` | What changed in the context between two requests. |
| `cache --session ID` | Per-request cache verdicts, causes and extra cost. |
| `budget` | Each rule's spend, limit and reset time. |
| `prices [MODEL]` | Built-in price table. |
| `statusline` | One-line summary for Claude Code's status line. |
| `init`, `prune` | Write a starter config; drop old profiles. |

All data commands take `--json`.

## Supported

| | |
|---|---|
| Anthropic | Messages API, streaming and not, prompt caching (5m and 1h), cache diagnostics, extended thinking, server tools, fast mode, data residency |
| OpenAI | Chat Completions and Responses, streaming and not, cached input, cache writes, reasoning tokens, service tiers |
| Clients | Claude Code (API key or subscription), Codex CLI, official SDKs, anything with a configurable base URL |
| Platforms | Linux, macOS, Windows; Python 3.11+ |

## Limits

- Only traffic through the proxy is capped. Keep a provider-side spend limit as a backstop.
- With the default `reserve = "estimate"`, requests already in flight when a cap is reached
  can finish, so spend can overshoot by their output. `reserve = "worst_case"` never
  overshoots and refuses earlier. See [reservations](docs/configuration.md#reservations).
- Costs come from a price table dated in [`prices.toml`](src/skinflint/data/prices.toml).
  Unknown models are priced at the provider's flagship rate and marked estimated.
- Token attribution within a prompt is an estimate scaled to the exact total.
- Not metered: batches, embeddings, images, audio, Bedrock and Vertex.
- WebSocket transports are refused; Codex falls back to HTTP streaming.

## How it works

[docs/how-it-works.md](docs/how-it-works.md) covers routing, admission and reservations,
token attribution, cache diagnosis and what is stored.

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
