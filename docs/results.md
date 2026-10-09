# Results

Measurements taken through skinflint 0.1.0 on 2026-10-09. They show what the profiler, the
cache doctor and the caps report on real agent traffic.

## Setup

| | |
|---|---|
| Machine | Windows 11, Python 3.12.13, aiohttp 3.14.4 |
| Claude Code | 2.1.287, signed in with a Claude subscription |
| Codex CLI | 0.162.0, signed in with ChatGPT, `-m gpt-5.5` |
| SDKs | `anthropic` 1.13.0, `openai` 3.27.0 |
| Prices | built-in table, as of 2026-10-09 |

All traffic was subscription traffic. Costs are what the same tokens would cost on the API,
not what was billed.

Each run used a fresh ledger (`SKINFLINT_HOME`) and a minimal environment: `PATH`, home and
system directories only, so no variables from the terminal or IDE that launched it. The
machine has a global `CLAUDE.md` of about 500 tokens and one claude.ai connector
(`claude_ai_Claude_Docs`, 8 tools) enabled at account level. No project-level `CLAUDE.md`,
settings or MCP servers.

## 1. What a one-word answer costs

Prompt: `claude -p "Reply with exactly: hi"`. Claude Code called no tools. One request each,
cold cache.

| Model | Configuration | Prompt tokens | Tools sent | Tool definitions | System + context | Cold cost |
|---|---|---:|---:|---:|---:|---:|
| Haiku 4.5 | default | 43,325 | 39 | 32,728 (76%) | 10,586 | $0.0868 |
| Haiku 4.5 | `--strict-mcp-config` | 41,037 | 31 | 31,021 (76%) | 10,005 | $0.0823 |
| Haiku 4.5 | `--strict-mcp-config --tools "Read,Edit,Write,Bash,Grep,Glob"` | 13,884 | 6 | 6,411 (46%) | 7,462 | $0.0280 |
| Haiku 4.5 | `--strict-mcp-config --tools ""` | 7,215 | 0 | 0 | 7,204 | $0.0146 |
| Sonnet 5.5 | default | 44,336 | 35 | 34,121 (77%) | 10,200 | $0.1774 |
| Opus 5.5 | default | 43,981 | 35 | 33,587 (76%) | 10,379 | $0.3519 |

The same default request with a warm cache cost $0.0045 on Haiku 4.5.

Breakdown of the default Haiku 4.5 request, from `skinflint profile`:

```text
request #1  21:27  claude-haiku-4-5-20251001  prompt 43.3k (0% cached)  output 40  $0.0868
session c42d115c-729f-49c3-96b8-62eb79b2e02a  scope probe

group                             tokens  share      cost
--------------------------------  ------  -----  --------
tools: built-in                    31.3k    72%   $0.0626
system prompt                       6.4k    15%   $0.0129
context: skills                     1.9k     4%   $0.0038
tools: mcp claude_ai_Claude_Docs    1.4k     3%   $0.0029
context: agent types                 585     1%   $0.0012
instruction files                    520     1%   $0.0010
context: mcp instructions            493     1%   $0.0010
context: attribution                 466     1%   $0.0009
context: environment                 126     0%   $0.0003
context: model                        47     0%   $0.0001
context: date                         21     0%  <$0.0001
user prompts                          11     0%  <$0.0001
```

Largest tool definitions, tokens per request:

| Tool | Tokens |
|---|---:|
| Bash | 3,521 |
| PowerShell | 2,766 |
| DesignSync | 2,570 |
| Agent | 2,463 |
| Monitor | 2,239 |

Findings:

- Tool definitions are three quarters of the prompt. The question itself is 11 tokens.
- Six tools and no MCP servers cut the prompt and the cold cost by 68%.
- One account-level connector added 1,433 tokens of tool definitions and 493 of
  instructions to every request, used or not.
- Model choice barely changes the prompt size; it changes the price per token.
- Token counts per group are estimates scaled to the exact total. See
  [token attribution](how-it-works.md#token-attribution).

## 2. A multi-turn session

Task: add a function and its tests in a small Python project (`claude -p`, Haiku 4.5), then
resume the same conversation with `claude -c` after 7 minutes idle. `skinflint cache`,
with rows and the `extra` column trimmed:

```text
session f43e4ca4-3136-4754-a03f-a984f8a064f4: 11 requests, hit rate 98%

 #  time   agent        prompt   read  write  verdict  cause
--  -----  -----------  ------  -----  -----  -------  --------------------------------------
 1  21:29  fp:71deb99a   43.4k  38.3k     5k  partial  no earlier request from this agent;...
 2  21:29  fp:71deb99a   43.8k  43.3k    419  hit
 ...
 8  21:29  fp:71deb99a   45.5k  45.3k    187  hit
 9  21:36  fp:71deb99a   45.6k  45.5k     83  hit
10  21:36  fp:71deb99a   46.2k  45.6k    592  hit
11  21:36  fp:71deb99a   46.3k  46.2k    172  hit
```

Findings:

- 11 requests sent 495k prompt tokens; 98% were cache reads. Prompt cost $0.0648.
- Request 1 read 38.3k from a cache written by an earlier session. Identical tools and
  system prompts share a cache across sessions.
- Request 9 came after 7 minutes idle and still hit. Claude Code marks its breakpoints with
  the 1-hour TTL, so the 5-minute expiry does not apply.
- The profiler flagged 28 built-in tools and one MCP server (8 tools) that were sent on
  all 11 requests and never called: 304k and 15.7k tokens, 65% of the session's prompt.

## 3. A cache break

A three-request session was resumed with two tools removed:
`claude -c --disallowedTools "WebSearch,WebFetch"`. Tools come first in the prefix, so
nothing after them could be read from cache.

First attempt:

| Verdict | Cause | Tokens rewritten | Extra cost (Haiku 4.5) |
|---|---|---:|---:|
| `prefix_changed` | tools changed: WebFetch, WebSearch removed | 42.6k | $0.0809 |

On a repeat, the first attempt had already cached the new prefix, so only 5.3k tokens were
rewritten. The API's own cache diagnostics named the same cause:

```text
verdicts: 2 hit, 1 partial, 1 api_reported
extra cost from misses: $0.0101 (5.3k tokens not read from cache)
top causes:
  1 x api_reported: tools_changed: WebFetch, WebSearch removed  $0.0101
  1 x partial
```

On Opus 5.5, the first miss would have cost about $0.33.

## 4. Caps

Claude Code and Codex were run against a `$0.01` per-session budget. The first request of
each needed more than that, so it was refused before reaching the provider. The SDKs were
run against a `requests = 0` rule.

| Client | Status | Shown | Attempts |
|---|---|---|---:|
| Claude Code 2.1.287 | 402 | `API Error: 402 skinflint: budget 'session' reached: $0 of $0.01 used this session (...); this request needs up to $0.0397. Edit or remove it in config.toml.` | 1 |
| Codex CLI 0.162.0 | 429 | `Quota exceeded. Check your plan and billing details.` | 1 |
| `anthropic` 1.13.0 | 402 | `APIStatusError`, type `billing_error` | 1 |
| `openai` 3.27.0 | 429 | `RateLimitError`, code `skinflint_budget_exceeded` | 1 |

No client retried. Every refused request was recorded as `blocked` and never reached the
provider.

Codex without a cap: one request, 17.1k prompt tokens, 98% cached, $0.0103
API-equivalent. The session id came from Codex's `session-id` header, and the request was
marked as subscription traffic from its `chatgpt-account-id` header.

## 5. Overhead

`scripts/bench.py`: proxy and a fake upstream on localhost, 200 requests per row. The
226 KB body is a real Claude Code request. The machine was running other work; treat the
numbers as an order of magnitude.

| Request | Metric | Direct p50 ms | Overhead p50 ms | p95 ms | p99 ms |
|---|---|---:|---:|---:|---:|
| small, JSON | total | 1.89 | +5.08 | +7.74 | +9.24 |
| small, SSE | total | 1.28 | +5.80 | +7.22 | +9.00 |
| Claude Code 226 KB, JSON | total | 2.80 | +13.13 | +17.09 | +19.20 |
| Claude Code 226 KB, SSE | ttfb | 1.80 | +12.75 | +18.77 | +21.81 |

A model's time to first token is hundreds of milliseconds to seconds, so 5 to 15 ms is
under the noise. Overhead grows with body size because the request is parsed before it is
forwarded, to check budgets. Full profiling runs in a worker thread in parallel with the
upstream call.

With 32 concurrent streams the proxy sustained 187 requests per second against 497 direct
on this run. One agent sends a few requests per minute.

## Reproduce

```sh
skinflint serve --config config.toml          # empty config; set SKINFLINT_HOME to a fresh dir
ANTHROPIC_BASE_URL=http://127.0.0.1:4100/s/a-default claude -p "Reply with exactly: hi" --model haiku
ANTHROPIC_BASE_URL=http://127.0.0.1:4100/s/c-six-tools claude -p "Reply with exactly: hi" --model haiku \
  --strict-mcp-config --tools "Read,Edit,Write,Bash,Grep,Glob"
skinflint report --by scope
skinflint profile last
skinflint cache --session last
uv run python scripts/bench.py
```

Numbers will differ with the Claude Code version, enabled connectors, MCP servers, skills
and instruction files. That difference is what the profiler is for.
