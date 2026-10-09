# Configuration

skinflint reads one TOML file. Every section is optional; with no file, it proxies and
records but enforces no caps.

Location, first match wins:

1. `--config PATH` on the command line
2. `$SKINFLINT_CONFIG`
3. `$SKINFLINT_HOME/config.toml` (default `~/.skinflint/config.toml`)

`skinflint init` writes a starter file. Unknown keys are errors, so a typo cannot silently
disable a cap. The proxy reads the file at startup; restart it after editing.

## `[server]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `"127.0.0.1"` | Interface to bind. skinflint warns when this is not a loopback address: anyone who can reach the port can spend through it. |
| `port` | `4100` | Port to listen on. |

## `[upstream]`

| Key | Default | Meaning |
|---|---|---|
| `anthropic` | `"https://api.anthropic.com"` | Where Anthropic-format requests go. |
| `openai` | `"https://api.openai.com"` | Where OpenAI-format requests go. |

The request path is appended unchanged. A client using `OPENAI_BASE_URL=http://127.0.0.1:4100/v1`
requests `/v1/chat/completions`, which goes to `https://api.openai.com/v1/chat/completions`.

Any OpenAI-compatible service works if its usage fields match OpenAI's. Codex signed in with
ChatGPT talks to `https://chatgpt.com/backend-api/codex`; set `openai` to that and point
Codex's `openai_base_url` at `http://127.0.0.1:4100`.

A request is routed to Anthropic when it carries an `anthropic-version` header or its path
starts with `/v1/messages` or `/v1/complete`. Everything else goes to OpenAI.

## `[storage]`

| Key | Default | Meaning |
|---|---|---|
| `path` | `"$SKINFLINT_HOME/skinflint.db"` | SQLite ledger. Relative paths resolve against the config file's directory. |
| `keep_profiles_days` | `30` | Request profiles (segment sizes and hashes) older than this are dropped by `skinflint prune`. `0` keeps them. Ledger rows are kept. |
| `store_bodies` | `false` | Also store raw request bodies. They contain your prompts, code and tool output. |

skinflint never stores API keys or `Authorization` headers.

## `[limits]`

| Key | Default | Meaning |
|---|---|---|
| `reserve` | `"estimate"` | What an in-flight request holds against budgets. See [Reservations](#reservations). |
| `unknown_model` | `"max"` | Models without a price: `"max"` prices them at the provider's flagship model and marks the cost as estimated; `"block"` refuses them. |
| `unmetered` | `"allow"` | `POST` requests to endpoints skinflint cannot meter (batches, embeddings, images). `"block"` refuses them while any budget is configured. Token counting endpoints stay open. |

## `[anthropic]`

| Key | Default | Meaning |
|---|---|---|
| `cache_diagnostics` | `true` | Add `diagnostics.previous_message_id` to Messages requests so the API reports why a prompt-cache prefix was not reused. Only sent to `https://api.anthropic.com` and only when the request has a session id. If the API rejects the field, skinflint retries without it and stops adding it. |

## `[openai]`

| Key | Default | Meaning |
|---|---|---|
| `inject_stream_usage` | `true` | Chat Completions streams report usage only when the client asks for it. skinflint asks, reads the usage chunk, and removes that chunk before the client sees it. |

## `[[budget]]`

Each `[[budget]]` table is one rule. A request must pass every rule that matches it.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Shown in errors and reports. Unique. |
| `usd` | - | Spend limit in US dollars. |
| `tokens` | - | Token limit: uncached input + cache writes + cache reads + output. |
| `requests` | - | Request count limit. |
| `window` | `"day"` | `"hour"`, `"day"`, `"week"` (from Monday), `"month"` or `"total"`. Calendar windows in local time. |
| `per` | `"all"` | `"all"`: one total for every matching request. `"scope"`: a total per scope. `"session"`: a total per session. `"request"`: each request alone (`usd` only, checked before sending). |
| `scope` | `"*"` | Glob over scope names. Case-sensitive. |
| `model` | `"*"` | Glob over model ids, e.g. `"claude-opus-*"`. |
| `action` | `"block"` | `"block"` refuses requests over the limit. `"warn"` only logs. |

At least one of `usd`, `tokens`, `requests` is required.

Per-session rules apply only to requests that carry a session id. Claude Code always sends
one. Other clients can send `x-skinflint-session`.

### Examples

```toml
# $20 a day across everything.
[[budget]]
name = "daily"
usd = 20

# $5 per agent session, ever.
[[budget]]
name = "session"
usd = 5
per = "session"
window = "total"

# Opus at most $10 an hour.
[[budget]]
name = "opus-hourly"
usd = 10
window = "hour"
model = "claude-opus-*"

# Each CI job gets $2 in total; CI jobs use scopes like ci-1234.
[[budget]]
name = "ci-job"
usd = 2
per = "scope"
scope = "ci-*"
window = "total"

# Refuse any single request that could cost more than $1.
[[budget]]
name = "big-request"
usd = 1
per = "request"

# Log when a scope passes 50,000 requests a month.
[[budget]]
name = "volume"
requests = 50000
per = "scope"
window = "month"
action = "warn"
```

### Reservations

The real cost of a request is known only when it finishes. While it runs, skinflint holds a
reservation against every matching budget, so parallel requests cannot all slip under a cap
at once. Reservations are written to the ledger in the same transaction as the budget check,
so this also holds across several skinflint processes sharing one ledger.

- `reserve = "estimate"` holds the estimated prompt cost at the uncached input rate. A
  request is admitted while spend plus reservations stays under the cap. Requests already in
  flight can still finish, so spend can overshoot the cap by their output.
- `reserve = "worst_case"` holds the prompt at the highest rate it could be billed at, plus
  `max_tokens` of output (4,096 if the request sets none). Spend never exceeds the cap,
  but requests are refused earlier, and parallel requests reserve a lot.

When a request finishes, its reservation is replaced by its real cost. Reservations left by a
crashed process stop counting after 15 minutes.

### What a refusal looks like

The request is not sent upstream. The client gets the provider's own error format, with
`x-should-retry: false` so official SDKs, Claude Code and Codex do not retry:

| Provider | Status | Body |
|---|---|---|
| Anthropic | `402` | `{"type":"error","error":{"type":"billing_error","message":"skinflint: budget 'daily' reached: ..."}}` |
| OpenAI | `429` | `{"error":{"type":"insufficient_quota","code":"skinflint_budget_exceeded","message":"skinflint: budget 'daily' reached: ..."}}` |

## `[prices."<model>"]`

Override or add model prices, in USD per million tokens.

```toml
[prices."my-finetune"]
provider = "openai"
input = 3.00
output = 12.00
cache_read = 1.50

[prices."claude-sonnet-5-5"]
input = 2.00
output = 10.00
cache_read = 0.10
```

| Key | Meaning |
|---|---|
| `provider` | `"anthropic"` or `"openai"`. Needed for new models; inferred for known ones. |
| `input`, `output` | Required. |
| `cache_write_5m`, `cache_write_1h`, `cache_read` | Optional. Defaults: Anthropic 1.25x, 2x and 0.1x of `input`; OpenAI writes at `input`, reads at `input`. |
| `web_search_per_1k` | Server-side web search, per 1,000 searches. |
| `long_context` | `{ threshold = 100000, input = ..., output = ..., ... }`: rates for the whole request when the prompt exceeds `threshold` tokens. |
| `fast` | `{ input = ..., output = ... }`: rates when the response reports fast mode. |

Built-in prices are in [`src/skinflint/data/prices.toml`](../src/skinflint/data/prices.toml),
with the date they were checked. `skinflint prices` lists them.

## Scopes and sessions

A **scope** is a label you choose per agent, project or job. Put it in the base URL:

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:4100/s/my-project
export OPENAI_BASE_URL=http://127.0.0.1:4100/s/my-project/v1
```

or send an `x-skinflint-scope` header (Claude Code: `ANTHROPIC_CUSTOM_HEADERS`). Without
either, the scope is `default`. Scope names are 1-64 characters from `A-Z a-z 0-9 . _ : @ + -`.

A **session** is one agent conversation. skinflint reads it from:

1. the `x-skinflint-session` header;
2. Claude Code's `x-claude-code-session-id` header or `metadata.user_id`;
3. Codex's `session-id` header.

`x-skinflint-*` headers are removed before the request is forwarded.
