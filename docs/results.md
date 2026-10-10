# Results

Measurements taken through skinflint 0.1.0. They show what the profiler, the cache doctor
and the caps report on real agent traffic. Sections 1-5 were taken on 2026-10-09 on Windows;
sections 6-9 on 2026-10-10 on Linux, with raw data and scripts in
[`measurements/2026-10-10`](../measurements/2026-10-10) and
[`scripts/measure`](../scripts/measure).

**Correction (2026-10-10).** Behind a custom `ANTHROPIC_BASE_URL`, Claude Code turns tool
search off and sends every tool definition in full. All of sections 1-3 were measured that
way. Sent to the API directly, the same one-word request is about 40% smaller: 21,266 prompt
tokens on Claude Code 2.1.287 instead of 35,966 (Linux, no connectors). Set
`ENABLE_TOOL_SEARCH=true` to get the direct behaviour through skinflint. See section 6.

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

Section 6 has the same request measured on three Claude Code versions, with and without tool
search and MCP servers.

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

## 6. Fixed context overhead by version

Setup for sections 6-9:

| | |
|---|---|
| Machine | Linux container, Python 3.13, skinflint at commit `0f57ee1` |
| Claude Code | 2.1.250, 2.1.287 and 2.1.296 from npm, signed in with a Claude subscription |
| Environment | `PATH` and `HOME` only; empty working directory; no `CLAUDE.md`; `--strict-mcp-config` |
| MCP servers | `filesystem`, `memory`, `everything`, `sequential-thinking` (`@modelcontextprotocol/server-*`, stdio) |
| Ledger | a fresh `SKINFLINT_HOME` per run |

Prompt: `claude -p "Reply with exactly: hi" --model claude-haiku-4-5`. The table shows the
largest request of each run. Two runs per cell; the prompt size was identical in both, except
2.1.287 without MCP (35,442 and 35,611). "Direct" is the prompt Claude Code reported when
talking to `api.anthropic.com` without the proxy, summed over its calls.

| Version | MCP | Direct, tool search default | Through skinflint, default | Through skinflint, `ENABLE_TOOL_SEARCH=true` | Cold cost, default / `true` |
|---|---|---:|---:|---:|---:|
| 2.1.250 | none | 20,618 | 35,228 | 20,455 | $0.0705 / $0.0409 |
| 2.1.250 | 4 servers | 21,451 | 42,272 | 21,288 | $0.0845 / $0.0426 |
| 2.1.287 | none | 21,266 | 35,442 | 21,001 | $0.0709 / $0.0420 |
| 2.1.287 | 4 servers | 22,112 | 43,030 | 21,847 | $0.0861 / $0.0437 |
| 2.1.296 | none | 21,593 | 35,995 | 21,328 | $0.0720 / $0.0427 |
| 2.1.296 | 4 servers | 22,439 | 43,586 | 22,174 | $0.0872 / $0.0443 |

Cold cost is the prompt written to the 1-hour cache at Haiku 4.5's $2/MTok. Direct runs with
`ENABLE_TOOL_SEARCH=false` sent 35,391 to 43,941 tokens, the same as the proxied default.

Split of the 2.1.296 requests, from `skinflint profile`:

| Configuration | Built-in tools | MCP tools | System prompt | Skills list | Prompt |
|---|---:|---:|---:|---:|---:|
| tool search off, no MCP | 27,014 | 0 | 6,367 | 1,627 | 35,995 |
| tool search off, 4 servers | 27,530 | 6,687 | 6,352 | 1,623 | 43,586 |
| tool search on, no MCP | 12,275 | 0 | 6,333 | 1,619 | 21,328 |
| tool search on, 4 servers | 12,336 | 0 | 6,365 | 1,627 | 22,174 |

On 2.1.296, other models, tool search off / on: Haiku 5.5 32,397 / 17,338 tokens, Sonnet 5.5
31,622 / 16,563. With four MCP servers: Haiku 5.5 42,669 / 18,562, Sonnet 5.5 41,894 / 17,787.

Findings:

- Through a proxy, Claude Code's default prompt is 67% larger than direct (35,995 against
  21,593 tokens on 2.1.296). `ENABLE_TOOL_SEARCH=true` removes the difference.
- With tool search on, four MCP servers cost 846 tokens per request: their tools are deferred
  and only a list of names (449 tokens) and the servers' instructions (410) are sent. With it
  off they cost 7,591, of which 6,687 are tool definitions.
- From 2.1.250 to 2.1.296 (six weeks), the default prompt grew by 767 tokens (2.2%) with
  tool search off and by 873 (4.3%) with it on.
- Newer models tokenise the same request differently: 35,995 tokens on Haiku 4.5, 32,397 on
  Haiku 5.5, 31,622 on Sonnet 5.5.

## 7. Caps under parallel load

N `claude -p` agents ran at once against one per-session budget (`per = "session"`,
`window = "total"`). Every agent sent `x-skinflint-session: <round>` through
`ANTHROPIC_CUSTOM_HEADERS`, so the cap covered all of them. Task: read each of 21 Python files
and write a summary of each, about 40 turns if never stopped. Agents marked "killed" got
`SIGKILL` once their second or later message had streamed 10-40 content deltas. Claude Code
2.1.296.

| Round | Model | Reserve | Cap | Agents | Killed | Refused requests | Spend (ledger) | Against cap |
|---|---|---|---:|---:|---:|---:|---:|---:|
| r1 | Opus 5.5 | estimate | $1 | 4 | 0 | 4 | $1.0838 | +8.4% |
| r2 | Opus 5.5 | estimate | $1 | 6 | 2 | 4 | $0.9137 | -8.6% |
| r3 | Opus 5.5 | estimate | $1 | 8 | 2 | 6 | $1.0147 | +1.5% |
| r4 | Opus 5.5 | estimate | $1 | 8 | 3 | 5 | $0.8906 | -10.9% |
| r5 | Opus 5.5 | estimate | $1 | 8 | 3 | 5 | $0.8975 | -10.3% |
| r6 | Opus 5.5 | estimate | $1 | 6 | 0 | 6 | $0.9873 | -1.3% |
| r9 | Sonnet 5.5 | estimate | $1 | 8 | 1 | 7 | $1.0896 | +9.0% |
| r7 | Sonnet 5.5 | worst_case | $1 | 6 | 0 | 6 | $0 | -100% |
| r8 | Opus 5.5 | worst_case | $1 | 4 | 0 | 4 | $0 | -100% |
| r10 | Sonnet 5.5 | worst_case | $5 | 6 | 1 | 4 | $1.5643 | -68.7% |

Findings:

- `reserve = "estimate"`: over seven rounds, spend ended between $0.8906 and $1.0896 against
  the $1 cap; three rounds went over, by at most $0.0896 (9.0%). The overshoot came from
  requests admitted in parallel. In r1 all four first requests were admitted at once: the
  estimate holds the prompt at the uncached input rate ($4/MTok on Opus 5.5), but each wrote
  32,800 tokens to the 1-hour cache at $8/MTok and cost $0.2635. The overshoot stayed far below
  the documented bound ($0.64 per in-flight Opus request) because Claude Code's turns produced
  a few hundred output tokens, not 32,000.
- It also stops early: a request is refused when its estimate does not fit, so four rounds
  ended 1-11% under the cap.
- `reserve = "worst_case"` never went over. It reserved $1.57 per Claude Code request on
  Sonnet 5.5 and $3.13 on Opus 5.5 (32,000 output tokens plus the prompt at the dearest rate),
  against an actual $0.01-0.27 per request. A $1 cap therefore refused every first request.
  With a $5 cap, three requests filled the cap with reservations at the start, so three of
  six agents were refused on their first request; the round spent $1.56.
- The refusal message counts reservations as used: "$4.70 of $5.00 used this session" was
  $0 spent plus $4.70 reserved.
- No agent retried a refused request. Claude Code printed the 402 and exited with
  `subtype: success`.

### Killed clients

12 agents were killed mid-stream. Nine left a stream cut off at the proxy, which recorded it
as `aborted` with output estimated from the streamed text. The other three had already
received the full response from the API when the kill reached the proxy, which billed them
from the final usage.

| Rounds | Aborted rows | Streamed chars | Output tokens (estimated) | Cost each (estimated) |
|---|---:|---:|---:|---:|
| r2-r4 (Opus 5.5) | 6 | 291-326 | 97-109 | $0.0112-0.0114 |
| r5 (Opus 5.5) | 2 | 1,703-1,724 | 568-575 | $0.0208-0.0209 |
| r10 (Sonnet 5.5) | 1 | 1,554 | 518 | $0.0101 |

Killed agents report no cost. In rounds with kills, Claude Code's own `total_cost_usd`
summed over the surviving agents missed 3-30% of the round's spend; the ledger kept it (for
example r5: $0.6285 reported, $0.8975 in the ledger).

There is no ground truth for the cut-off rows. The API's usage for a stream that the client
abandoned is not visible to the client or the proxy, and a subscription shows no per-request
bill. The estimate can be low by any thinking the model did not stream and by output generated
after the connection closed. As a plausibility check, a completed request at the same step of
the same task in r5 had 581 output tokens; the two cut-off ones were estimated at 568 and 575.

## 8. Cache breakers

Each trial ran `claude -p "Reply with exactly: one"`, then `claude -p --resume <session>
"Reply with exactly: two"` with one change, both through one fresh proxy with cache
diagnostics on. Haiku 4.5, Claude Code 2.1.296, tool search off, three trials per change.
The system prompt carried a per-trial nonce, so trials shared only the tool-definition prefix
(26,397 tokens) with each other.

| Change before turn 2 | Turn-2 cost | Read / written on turn 2 | API `cache_miss_reason` | skinflint verdict |
|---|---:|---|---|---|
| none (control) | $0.0039 | 35,964 / 58-81 | none | hit |
| one tool removed (`--disallowedTools NotebookEdit`) | $0.0220 | 25,921 / 9,622 | `tools_changed` | `tools changed: NotebookEdit removed` |
| one MCP server added (`--mcp-config`, memory) | $0.0759 first, $0.0223 after | 0 / 37,880, then 28,250 / 9,627-9,643 | `tools_changed` | `tools changed: ListMcpResourcesTool, ... +9 more added`, then `hit` |
| model switched to Haiku 5.5 | $0.0089 first, $0.0029 after | 0 / 44,384, then 31,571 / 12,813 | none | `cold` / `partial`: first request of this agent |
| idle 5m32s with `CLAUDE_CODE_PROMPT_CACHE_TTL=5m` (Sonnet 4.6) | $0.1321 | 0 / 35,216 | none | `expired: 5m32s ... exceeds the 5m00s cache lifetime` |
| `CLAUDE.md` edited | $0.0042 | 36,099 / 180-194 | none | hit |
| `--append-system-prompt` changed | $0.0039 | 35,964 / 55-62 | none | hit |
| `--system-prompt` replaced | $0.0039 | 35,969 / 53-63 | none | hit |
| new git commit in the working directory | $0.0039 | 36,040-36,044 / 58-65 | none | hit |

Turn-2 cost is the increase in Claude Code's `total_cost_usd`, which is cumulative on
`--resume`. Without the API diagnostics, skinflint's own verdicts were the same, with
`prefix_changed` in place of `api_reported`.

Findings:

- Tool changes break the cache from the first tool definition on. Adding one MCP server
  re-wrote 37,880 tokens and made turn 2 cost 19 times the control. A second session with the
  same change read most of it back from the first one's cache.
- The 5-minute expiry re-wrote the whole prefix (35,216 tokens). Turn 2 cost $0.1321, about
  12 times a full hit at Sonnet 4.6 prices (computed, $0.011). The API reported no reason for it; skinflint named the expiry from
  the gap. In two of the three TTL trials another trial had just re-written the shared tools
  prefix, so 25,804 tokens were read after all; skinflint still said `expired`, which
  overstates the miss for those two.
- A model switch is a fresh cache. skinflint reports it as a new agent (`cold`) rather than
  naming the switch. The API returned no diagnostics for it.
- Changing the system prompt, `CLAUDE.md` or the git state between turns broke nothing: on
  `--resume`, Claude Code 2.1.296 kept the system prompt of the original session and added the
  new `CLAUDE.md` content (109 tokens) after the cached prefix. A system-prompt break inside one
  Claude Code session was therefore not reproducible from the command line.
- The API gave a reason in 6 trials, all `tools_changed`. skinflint's local verdict named the
  same change in 4. In the other 2 (MCP, second and third trial) it said `hit`, because
  28,250 tokens were read from the first trial's cache; 9.6k tokens were still re-written.
  The API never reported a reason for the TTL expiry or the model switch.

## 9. Ledger against Claude Code's reported cost

Every completed run in sections 6-8 (111 runs: 32 overhead, 52 agents, 27 cache trials) was
compared with the `total_cost_usd` Claude Code printed. The totals were identical:
$10.107486 each; the largest difference for one run was 2e-16 dollars. Both apply list
prices to the usage the API returned, so this checks metering and pricing against Claude
Code, not against a bill.

Cost of these measurements: $12.98 API-equivalent, all subscription traffic.

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

Sections 6-9: install the Claude Code versions and MCP servers under one scratch directory
(`npm install --prefix $MEASURE_SCRATCH/cc/<version> @anthropic-ai/claude-code@<version>`,
`npm install --prefix $MEASURE_SCRATCH/mcp @modelcontextprotocol/server-filesystem ...`), then
run `scripts/measure/{overhead,direct,capstress,cachebreak,analyse}.py`. Each script charges
every nested call to `measurements/<date>/spend.jsonl` and stops before a set limit
(`MEASURE_STOP_AT`, default $28).

Numbers will differ with the Claude Code version, enabled connectors, MCP servers, skills
and instruction files. That difference is what the profiler is for.
