# skinflint 0.1.1

Released 2026-10-30. Bug fixes to budget accounting and two behaviour changes. No ledger
schema change. Full list: this page plus [CHANGELOG.md](../CHANGELOG.md#011---2026-10-30).

## Behaviour change

- `skinflint run` sets `ENABLE_TOOL_SEARCH=true` for the child unless the variable is already
  set. Behind a custom base URL Claude Code otherwise turns tool search off, which made each
  request about 67% larger in the measured runs
  ([results, section 6](results.md#6-fixed-context-overhead-by-version)).
- The ledger's `model` column keeps the model the client requested instead of the one the
  response names, so `report --by model` shows `gpt-5.4` rather than a dated snapshot. The
  cost still uses the response's model.
- OpenAI `POST .../responses/compact` is now metered and capped, since it bills tokens like
  any other response. It used to pass through unrecorded.

## Fixes

- A budget with an exact `model` stopped counting spend when the response named a dated
  snapshot of it: admission matched the requested id, the spend query the reported one, so the
  cap never tripped.
- A budget glob with `[!a]` matched at admission but not in the spend query, for the same
  result. Both now use one matcher.
- A lone surrogate in a request (`\ud83d`, a cut emoji) sent the request through unmetered and
  uncapped when it was in the first message, and failed it with a 500 when skinflint rewrote
  the body.
- `unmetered = "block"` now also refuses metered requests whose body cannot be read.
- `skinflint run --cap nan` and `--cap inf` are refused instead of disabling the cap.
- A stream that opens with the provider's error event (`overloaded_error`) is no longer
  charged the conservative prompt estimate.
- `skinflint run` gives its child a valid URL when the config sets an IPv6 host.
- On Linux and macOS, `skinflint serve` no longer reports the port as in use after a restart
  while connections from the previous process are in `TIME_WAIT`.

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

## Prices and packaging

- Prices checked against the provider pages on 2026-11-06; added `gpt-6.1-terra`.
- `aiohttp>=3.14.4`, which excludes the versions with published advisories.
- The sdist includes `scripts/`, which its tests need.
- CI actions are pinned to commit SHAs and CI installs with `uv sync --locked`.

## Install

```sh
uv tool install git+https://github.com/h-a-forster/skinflint@v0.1.1
```
