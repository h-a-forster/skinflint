# Backlog

## In progress

## Waiting on me

- 0.1.1 is released: branch `polish-2026-10-30` merged by PR with a merge commit, `v0.1.1` tagged, GitHub release published from `docs/release-notes-0.1.1.md`, and the release date in `CHANGELOG.md`, `CITATION.cff` and the notes set to the real tag date.
- `CHANGELOG.md` covers the 2026-11-06 fixes, or you decide the release notes replace it (agents do not hand-edit it, so its 0.1.1 entry lacks them).
- Dependabot PR #9 (setup-uv 10.3.3) is merged or closed after the SHA pins land.
- Remote branch `claude/fix-review-2026-10-10` stays until `docs/results.md` stops citing its commit `0f57ee1`, which is not in main's history.

## Decided

- 2026-11-06: the ledger's `model` column keeps the requested id; the response's model only prices the request. Budget rules and spend queries then agree without a schema change.

## Proposed

- `gpt-5.6-sol` is re-priced after its promotion ends (2026-11-21 at the earliest, per `prices.toml`).
- OpenAI 30-minute and 12-hour cache writes are billed at their own rates (`cache_write_tokens_details`; today every write uses the 30-minute rate).
- OpenAI web search fees reach the ledger (billed per call; usage does not report them).
- A non-streaming response cut off mid-body is settled with an estimate, as cut-off streams are, instead of $0.
- `skinflint serve` settles in-flight requests on SIGTERM, as it does on Ctrl+C.
- The README's open questions (overhead vs MCP servers and skills, real-session cache breaks, a tighter worst-case bound, overshoot at higher parallelism, the 163-524-token direct/proxied gap, Codex overhead) are answered by new live measurements; each costs API or subscription spend.
- Sections 6-9 of `docs/results.md` are re-measured on the released 0.1.1; they ran on `0f57ee1`, before reservation heartbeats and cache-aware estimates.
