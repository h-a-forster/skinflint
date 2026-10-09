# Client setup

Start the proxy first:

```sh
skinflint serve
```

Or skip the server and wrap a single command, which starts a private proxy on a free port
for the lifetime of that command:

```sh
skinflint run --cap 5 -- claude
```

## Claude Code

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:4100
claude
```

Works with an API key and with a Claude subscription login. Each Claude Code conversation
becomes a skinflint session automatically (`x-claude-code-session-id`), so `per = "session"`
budgets apply per conversation.

To label a project, put a scope in the URL:

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:4100/s/my-project
```

or send it as a header:

```sh
export ANTHROPIC_CUSTOM_HEADERS="x-skinflint-scope: my-project"
```

When a budget blocks, Claude Code shows the message and stops without retrying:

```text
API Error: 402 skinflint: budget 'session' reached: $5.01 of $5.00 used this session (3af4ca3c-...).
```

Set `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` to have Claude Code label each request as `main`,
`subagent`, `compaction` or `auxiliary`; `skinflint report --by request_class` then splits
spend that way.

### Status line

Add to `~/.claude/settings.json`:

```json
{
  "statusLine": { "type": "command", "command": "skinflint statusline" }
}
```

It prints one line from the ledger, for example:

```text
skinflint $0.42 session · $3.10/$20 today · cache 91% · 5h 47%
```

`5h` is the subscription's five-hour usage, read from Anthropic's rate-limit headers.

## Codex CLI

Codex reads its base URL from `~/.codex/config.toml` (not from `OPENAI_BASE_URL`).

With an API key:

```toml
openai_base_url = "http://127.0.0.1:4100/v1"
```

Signed in with ChatGPT, Codex talks to a different backend. Point skinflint at it in
`~/.skinflint/config.toml`:

```toml
[upstream]
openai = "https://chatgpt.com/backend-api/codex"
```

and Codex at skinflint:

```toml
openai_base_url = "http://127.0.0.1:4100"
```

For a single run, pass it on the command line instead:

```sh
codex exec -c openai_base_url='"http://127.0.0.1:4100"' "..."
```

Codex first tries a WebSocket connection. skinflint refuses it with `426`, and Codex falls
back to HTTP streaming, which skinflint meters. Codex logs one error line about the refused
WebSocket; it is expected.

## Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:4100/s/my-app")
```

or set `ANTHROPIC_BASE_URL`. Pass a session id to group requests:

```python
client.messages.create(..., extra_headers={"x-skinflint-session": "job-42"})
```

## OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:4100/s/my-app/v1")
```

or set `OPENAI_BASE_URL=http://127.0.0.1:4100/s/my-app/v1`. Chat Completions and Responses
are metered, streaming or not.

## Anything else

Any client that lets you set the Anthropic or OpenAI base URL works. Put `/v1` on the end
for OpenAI-style clients if they expect it. Check with:

```sh
skinflint report --since 10m
```

## Budget refusals by client

| Client | Status | What happens |
|---|---|---|
| Claude Code | 402 | Shows `API Error: 402 <message>`, stops, no retry. |
| Anthropic SDKs | 402 | Raises an `APIStatusError` (no retry: `x-should-retry: false`). |
| OpenAI SDKs | 429 | Raises `RateLimitError` with code `skinflint_budget_exceeded` (no retry: `x-should-retry: false`). |
| Codex | 429 | Reports the quota error and stops. |
