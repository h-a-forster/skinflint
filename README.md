# skinflint

[![CI](https://github.com/h-a-forster/skinflint/actions/workflows/ci.yml/badge.svg)](https://github.com/h-a-forster/skinflint/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A local proxy for the Anthropic and OpenAI APIs. It puts hard spend caps on coding agents,
shows where every prompt token goes, and explains each prompt-cache miss.

Point Claude Code, Codex or any SDK at `http://127.0.0.1:4100`. Requests are forwarded
as sent, apart from two optional additions that make metering work
([details](docs/how-it-works.md#forwarding)). Each one is checked against your budgets first
and recorded after.

## Why

- **Agents spend unattended.** Provider limits are monthly, per organisation or workspace
  (Anthropic) or per project (OpenAI). `claude -p --max-budget-usd` caps one run and proxies
  like LiteLLM cap per key, but each needs setup per run or key. skinflint caps every Claude
  Code and Codex session by the session ids they already send. No keys; it runs locally.
- **Most of the prompt is not your prompt.** Claude Code 2.1.287 sent 43,325 tokens to
  answer `Reply with exactly: hi`. 76% were definitions of 39 tools; none was called. With
  six tools the same request was 13,884 tokens. See [results](docs/results.md).
- **Cache misses are silent.** One changed tool schema or a timestamp in the system prompt
  re-bills the whole prefix at the cache-write rate. The response only shows a bigger number.

## What it does

- **Hard caps.** Budgets per day, hour, week, month or forever; per scope, per session or per
  request; in dollars, tokens or requests. Over the cap, the request is refused with the
  provider's own error format and `x-should-retry: false`, so clients stop instead of
  retrying. Checks and reservations are atomic, so parallel agents cannot all pass the same
  remaining budget.
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
uv tool install git+https://github.com/h-a-forster/skinflint
skinflint init     # writes ~/.skinflint/config.toml: $20/day, $5/session
skinflint serve
```

With pipx: `pipx install git+https://github.com/h-a-forster/skinflint`.

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

One request from Claude Code, as seen by `skinflint profile`:

```text
request #1  21:27  claude-haiku-4-5-20251001  prompt 43.3k (0% cached)  output 40  $0.0868

group                             tokens  share      cost
--------------------------------  ------  -----  --------
tools: built-in                    31.3k    72%   $0.0626
system prompt                       6.4k    15%   $0.0129
context: skills                     1.9k     4%   $0.0038
tools: mcp claude_ai_Claude_Docs    1.4k     3%   $0.0029
context: agent types                 585     1%   $0.0012
instruction files                    520     1%   $0.0010
...
user prompts                          11     0%  <$0.0001
```

A resumed session with two tools removed, as seen by `skinflint cache`:

```text
verdicts: 2 hit, 1 partial, 1 api_reported
extra cost from misses: $0.0101 (5.3k tokens not read from cache)
top causes:
  1 x api_reported: tools_changed: WebFetch, WebSearch removed  $0.0101
  1 x partial
```

When a cap is reached, Claude Code shows the refusal and stops:

```text
API Error: 402 skinflint: budget 'session' reached: $0 of $0.01 used this session (e2452a83-...); this request needs up to $0.0397. Edit or remove it in config.toml.
```

Codex shows `Quota exceeded.` and stops. More measurements: [docs/results.md](docs/results.md).

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
  can finish, so spend can overshoot by their output: up to $0.64 per in-flight Claude Code
  request on Claude Opus 5.5 ($1.28 in fast mode). `reserve = "worst_case"` reserves the most
  each request body allows and refuses earlier; server-side tool input is the one thing it
  cannot bound. See [reservations](docs/configuration.md#reservations).
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
