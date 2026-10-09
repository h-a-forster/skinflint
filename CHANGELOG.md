# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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
- Built-in prices for 57 models, overridable in config.
