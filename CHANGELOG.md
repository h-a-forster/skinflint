# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed

- `reserve = "worst_case"` could under-reserve. It now prices fast mode, `inference_geo`,
  OpenAI service tiers (the dearest one when the request leaves the tier unset or `auto`,
  since a project default can select priority), web search fees (`max_uses`, 50 searches
  when uncapped), the server-held prompt behind `previous_response_id` (looked up in the
  ledger, else 1.05M tokens) and `conversation`, and 128,000 output tokens when the request
  sets no limit. It sizes the prompt at 2.5 chars per token and 5,000 tokens per image.
- Streams cut off before their final usage (a client pressing Esc, a dropped connection)
  booked almost no output: Anthropic sends `output_tokens` only in the last `message_delta`,
  OpenAI sends no usage until the end. They are now settled with output estimated from the
  streamed content (3 chars per token) and, for OpenAI, input from the request estimate. The
  ledger marks the cost as estimated.
- On Windows the listener no longer sets `SO_REUSEADDR`, which let a second process bind
  port 4100 at the same time.

### Docs

- README wording on provider limits and request forwarding.
- Quantified how far `reserve = "estimate"` can overshoot.

## 0.1.0 - 2026-10-09

### Added

- Proxy for the Anthropic Messages API and the OpenAI Chat Completions and Responses APIs,
  streaming and not. Bodies and streams are relayed unchanged.
- Budgets in dollars, tokens or requests; per scope, session or request; by hour, day,
  week, month or in total. Checks and reservations are atomic across processes.
- Refusals in each provider's error format with `x-should-retry: false`.
- Context profiler: per-request and per-session token and cost attribution to tools, MCP
  servers, system prompt, instruction files, skills, tool results and messages. Flags tools
  and MCP servers that are sent but never called.
- Cache doctor: per-request verdicts, the block that broke the prefix, TTL expiry, and the
  extra cost. Uses Anthropic cache diagnostics when available.
- SQLite ledger with `report`, `sessions`, `profile`, `diff`, `cache`, `budget`, `prices`,
  `prune` and `--json` output.
- `run` wraps one command in a private, optionally capped proxy.
- Claude Code status line, including subscription five-hour usage.
- Claude Code (API key and subscription) and Codex CLI (API key and ChatGPT login) support.
- Built-in prices for 55 models, overridable in config.
