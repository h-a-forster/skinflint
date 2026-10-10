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

## Findings

From [measurements](docs/results.md) of Claude Code 2.1.296 and earlier, taken through skinflint.

- **A one-word answer carries a large fixed prompt.** `Reply with exactly: hi` sends 21,328
  prompt tokens with tool search on, 12,275 of them built-in tool definitions, and Claude Code called no tools
  ([section 6](docs/results.md#6-fixed-context-overhead-by-version)).
- **A custom `ANTHROPIC_BASE_URL` silently changes the prompt.** Claude Code turns tool search
  off and sends 35,995 tokens instead of 21,593 (+67%) unless `ENABLE_TOOL_SEARCH=true` is set
  ([section 6](docs/results.md#6-fixed-context-overhead-by-version)).
- **Estimate-mode caps can overshoot; worst-case mode cannot.** With `reserve = "estimate"`,
  spend ended between $0.8906 and $1.0896 on a $1 cap (up to 9.0% over) under parallel agents.
  `reserve = "worst_case"` never went over, but reserved $1.57 per Sonnet 5.5 request and
  $3.13 per Opus 5.5 request, so it needs caps of several dollars
  ([section 7](docs/results.md#7-caps-under-parallel-load)).
- **Tool changes are the expensive cache breaks.** Adding one MCP server re-wrote 37,880 tokens
  and made turn 2 cost 19 times the control; editing `CLAUDE.md` or the system prompt broke
  nothing ([section 8](docs/results.md#8-cache-breakers)).
- **The ledger agrees with Claude Code.** Across 111 runs the totals were identical,
  $10.107486 each ([section 9](docs/results.md#9-ledger-against-claude-codes-reported-cost)).

## Why

- **Agents spend unattended.** Provider limits are monthly, per organisation or workspace
  (Anthropic) or per project (OpenAI). `claude -p --max-budget-usd` caps one run and proxies
  like LiteLLM cap per key, but each needs setup per run or key. skinflint caps every Claude
  Code and Codex session by the session ids they already send. No keys; it runs locally.
- **Most of the prompt is not your prompt.** A coding agent sends its tool definitions,
  system prompt and instruction files on every request, whether or not they are used.
  See [findings](#findings).
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
export ENABLE_TOOL_SEARCH=true   # else Claude Code turns tool search off here: +67% prompt
claude
```

Or wrap one command with its own proxy and cap:

```sh
skinflint run --cap 2 -- claude -p "fix the failing test"
```

`skinflint run` sets `ENABLE_TOOL_SEARCH=true` in the child's environment unless the variable
is already set. Set `ENABLE_TOOL_SEARCH=false` to opt out.

Then:

```sh
skinflint report                  # spend by model, last 24h
skinflint profile --session last  # where the tokens went
skinflint cache --session last    # why the cache missed
```

Setup for Codex, the SDKs and the status line: [docs/clients.md](docs/clients.md).

## Example

One request from Claude Code 2.1.287 with tool search off (see above), as seen by
`skinflint profile`:

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

## Open questions

The measurements are small (one machine, one task, Claude Code only) and leave these open.

- **How does fixed overhead scale with MCP servers and skills?** Four servers cost 846 tokens
  with tool search on and 7,591 with it off; other counts and skill lists were not measured.
  Start from `scripts/measure/overhead.py` and `skinflint profile`.
- **What breaks the cache in real sessions?** Section 8 forces one change at a time in
  scripted runs. A system-prompt break inside one session was not reproducible. Start from
  `scripts/measure/cachebreak.py` and `skinflint cache --session ID` on your own sessions.
- **Can the worst-case bound be tighter?** It reserves 128,000 output tokens when a request
  sets no `max_tokens`, while turns produced a few hundred. Start from
  [reservations](docs/configuration.md#reservations) and `scripts/measure/capstress.py`.
- **How large is estimate-mode overshoot at higher parallelism or other tasks?** Seven rounds,
  4-8 agents, one task. Start from `scripts/measure/capstress.py` and
  [section 7](docs/results.md#7-caps-under-parallel-load).
- **Why does the proxied default differ from `ENABLE_TOOL_SEARCH=false` sent direct?** By
  163-524 tokens; the cause is not established. Start from `scripts/measure/direct.py` and
  `skinflint diff A B`.
- **Does the overhead pattern hold for Codex and other agents?** Only Claude Code was
  measured. Start from [docs/clients.md](docs/clients.md) and `scripts/measure/common.py`.

## Limits

- Only traffic through the proxy is capped. Keep a provider-side spend limit as a backstop.
- With the default `reserve = "estimate"`, requests already in flight when a cap is reached
  can finish, so spend can overshoot by their output: up to $2.56 per in-flight Claude Code
  request on Claude Opus 5.5 with 128,000 output tokens ($5.12 in fast mode, $1.28 on Sonnet
  5.5). Measured with 4-8 parallel Claude Code agents on a $1 cap, spend ended between $0.89
  and $1.09. `reserve = "worst_case"` reserves
  the most each request body allows and refuses earlier: $1.57 per Claude Code request on
  Sonnet 5.5, $3.13 on Opus 5.5, so it needs caps of several dollars. It cannot bound
  server-side tool input or a multi-page base64 PDF. For inputs held on the server
  (documents by file id or URL, stored prompts, previous responses and conversations) it
  reserves a full context window. See [reservations](docs/configuration.md#reservations)
  and [results](docs/results.md#7-caps-under-parallel-load).
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
