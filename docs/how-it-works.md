# How it works

```
 agent / SDK ──HTTP──▶ skinflint (127.0.0.1:4100) ──HTTPS──▶ api.anthropic.com / api.openai.com
                         │  1. admit: check budgets, reserve
                         │  2. forward unchanged, stream back as bytes arrive
                         │  3. read usage from the response
                         ▼  4. settle: real cost replaces the reservation
                       SQLite ledger (~/.skinflint/skinflint.db)
                         ▲
              skinflint report / profile / cache / budget / statusline
```

## Routing

A request goes to Anthropic if it has an `anthropic-version` header or its path starts with
`/v1/messages` or `/v1/complete`. Everything else goes to the OpenAI upstream. An optional
`/s/<scope>/` path prefix sets the scope and is removed before forwarding.

Only these are metered; everything else passes through untouched and unrecorded:

| Provider | Metered |
|---|---|
| Anthropic | `POST /v1/messages` |
| OpenAI | `POST .../chat/completions`, `POST .../responses` |

## Forwarding

Request and response bodies are forwarded as received. skinflint removes hop-by-hop
headers and its own `x-skinflint-*` headers. Everything else, including `anthropic-beta`
and auth headers, goes upstream unchanged.

Streams are relayed chunk by chunk. A parser reads the server-sent events as they pass and
keeps the latest usage numbers. It never delays a chunk.

Two body changes, both optional:

- **Anthropic cache diagnostics.** skinflint adds `"diagnostics": {"previous_message_id": ...}`
  with the id of the previous response in the same conversation. The API then reports why
  it could not reuse the cached prefix (`tools_changed`, `system_changed`, ...). Only for
  `https://api.anthropic.com`. Disable with `[anthropic] cache_diagnostics = false`.
- **OpenAI stream usage.** Chat Completions streams carry usage only if the client asked.
  skinflint asks, reads the usage chunk, and drops that chunk so the client sees the stream
  it requested.

## Admission and budgets

Before a metered request is sent, skinflint opens a write transaction on the ledger
(`BEGIN IMMEDIATE`), sums the spend of every budget rule that matches the request, adds the
reservations of requests still in flight, and either:

- inserts a `pending` row holding this request's reservation, then sends the request; or
- inserts a `blocked` row and returns an error in the provider's format, with
  `x-should-retry: false`. The request never reaches the provider.

Because the check and the reservation happen in one transaction, parallel requests, and
separate skinflint processes sharing a ledger, cannot all pass the same remaining budget.

When the response finishes, the row is updated with the real usage and cost and the
reservation is cleared. If the client disconnects mid-stream, the row is settled with the
usage seen so far and marked `aborted`.

Costs come from a built-in price table ([`prices.toml`](../src/skinflint/data/prices.toml))
covering uncached input, 5-minute and 1-hour cache writes, cache reads, output, long-context
tiers, fast mode, data-residency and service-tier multipliers, and web search.

## Token attribution

Providers report one number for the whole prompt. skinflint splits the request body into
**segments** in the order the model reads it: each tool definition, each system block, each
message content block. Each segment gets a kind, a group, a size and a hash.

Claude Code's context reminders are recognised and labelled: instruction files (`CLAUDE.md`,
`AGENTS.md`), the skills list, MCP server instructions, the agent list, environment details.
MCP tools are grouped by server (`mcp__<server>__<tool>`). Tool results are labelled with
the tool that produced them.

Segment sizes become token estimates (characters per token by kind; images fixed at 1,600),
then are scaled so they sum to the real prompt token count. The total is exact; the split is
an estimate.

Cost per group uses the rate each token was actually billed at. The prompt is read in prefix
order: the leading part was a cache read, the next part a cache write, the tail uncached.

`skinflint profile --session` adds up a whole session and lists tools and MCP servers that
were sent on every request but never called, with what they cost.

The ledger keeps segment sizes and hashes, not their content, unless `store_bodies = true`.

## Cache diagnosis

Prompt caching matches an exact prefix: tools, then system, then messages, up to a
`cache_control` breakpoint. One changed byte early in the prefix re-bills everything after
it at the cache-write rate.

For each request, `skinflint cache` finds its predecessor: the latest earlier request in the
same session and agent that shares the longest prefix of segment hashes. Then it decides:

| Verdict | Meaning |
|---|---|
| `hit` | At least 90% of the reusable prefix was read from cache. |
| `prefix_changed` | A segment before the breakpoint differs from the predecessor. The cause names it: `tools changed: mcp__github__create_issue schema changed`, `system[2] changed`, `messages[3] edited`, `model changed`. |
| `api_reported` | Anthropic's cache diagnostics reported the miss. The local comparison names the exact block. |
| `expired` | Nothing changed, but the gap since the predecessor exceeded its TTL (5 minutes or 1 hour). |
| `cold` | No earlier request to reuse. |
| `no_cache_control` | An Anthropic request with no breakpoints and a prompt long enough to cache. |
| `below_minimum` | The cacheable prefix is shorter than the model's minimum (512 to 4,096 tokens). |
| `partial`, `unexplained` | Some or no cache reads, with no identified cause. |

The extra cost of a miss is the tokens that could have been read, priced at what was paid
(write or uncached rate) minus the read rate.

Agents are told apart by Claude Code's `x-claude-code-agent-id` header when present, and
otherwise by a fingerprint of their tool names and system prompt, so a subagent or a side
request does not look like a cache break in the main conversation.

## What is stored

| Table | Contents |
|---|---|
| `requests` | One row per metered request: time, scope, session, agent, client, model, state, status, token counts by class, cost, reservation, latency, provider request id, cache-miss reason, error text. |
| `profiles` | Per request: segment kind, label, group, size, hash and breakpoint, compressed. Raw body only with `store_bodies = true`. |
| `ratelimits` | Latest rate-limit headers: Anthropic `anthropic-ratelimit-*` (including subscription plan usage), OpenAI `x-ratelimit-*`, Codex `x-codex-*`. |

API keys, `Authorization` headers and response content are never stored.

## Subscription traffic

Claude Code signed in with a Claude subscription sends an OAuth token, not an API key.
skinflint forwards it like any other credential. These requests are marked `plan`: their
cost is what the same tokens would cost on the API, not what you are billed. Budgets still
count them, which is useful as a burn-rate limit.
