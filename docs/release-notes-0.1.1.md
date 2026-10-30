# skinflint 0.1.1

Released 2026-10-30. Bug fixes to budget accounting and one behaviour change in `run`. No
ledger schema change. Full list: [CHANGELOG.md](../CHANGELOG.md#011---2026-10-30).

## Behaviour change

- `skinflint run` sets `ENABLE_TOOL_SEARCH=true` for the child unless the variable is already
  set. Behind a custom base URL Claude Code otherwise turns tool search off, which made each
  request about 67% larger in the measured runs
  ([results, section 6](results.md#6-fixed-context-overhead-by-version)).

## Fixes

- Requests in flight for more than 15 minutes keep their reservation: the proxy refreshes
  each in-flight ledger row every minute. Run one skinflint version per ledger; 0.1.0
  processes still expire reservations 15 minutes after arrival
  ([reservations](configuration.md#reservations)).
- Streams cut off before final usage (Esc, dropped connection) are settled with output
  estimated from the streamed content and marked estimated, instead of booking almost no
  output. Their input is priced with the cache state left by an earlier request in the same
  conversation when one exists.
- `reserve = "worst_case"` no longer under-reserves: it prices fast mode, `inference_geo`,
  OpenAI service tiers, web search fees, server-held prompts and inputs, OpenAI Chat `n`,
  and 128,000 output tokens when a request sets no limit.
- On Windows the listener binds with `SO_EXCLUSIVEADDRUSE`, so a second process cannot take
  the same port.

## Docs

- Measured results for Claude Code 2.1.250-2.1.296: fixed context, caps under 4-8 parallel
  agents with killed clients, and nine cache breakers checked against the API's diagnostics
  ([docs/results.md](results.md), raw data in `measurements/2026-10-10`).
- Corrected the worst-case premise and the estimate-mode overshoot bound ($2.56 per in-flight
  Opus 5.5 request).

## Install

```sh
uv tool install git+https://github.com/h-a-forster/skinflint@v0.1.1
```
