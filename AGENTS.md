# AGENTS.md

Guide for coding agents (and humans) working on skinflint. [CONTRIBUTING.md](CONTRIBUTING.md)
has the layout table and the full guidelines; [docs/how-it-works.md](docs/how-it-works.md)
explains routing, admission, reservations and attribution.

## What this is

A local proxy for the Anthropic Messages API and the OpenAI Chat Completions and Responses
APIs: hard spend caps (atomic across processes), a per-request context profiler and a
prompt-cache doctor, with a SQLite ledger. Measured results are in
[docs/results.md](docs/results.md), raw data in `measurements/`, scripts in `scripts/measure/`.

## Conventions

- uv + hatchling, `src/` layout, Python >= 3.11, `ruff check` + `ruff format` (line length
  100), pytest with `asyncio_mode = "auto"`. Runtime dependencies: aiohttp, and
  `backports.zstd` before Python 3.14; discuss new ones first.
- Proxying must never fail because metering failed: parsing code catches its own errors,
  keeps the best data it has and records what went wrong.
- Never log or store credentials. Request bodies are stored only on opt-in.
- Tests: no network (fake upstream in `tests/fakes.py`), `tmp_path` for every file including
  the ledger, must pass on Windows (`pathlib`, no shell assumptions).
- Every number in README or docs traces to `docs/results.md`, which traces to
  `measurements/<date>/`. Live measurements spend real money or subscription quota: run them
  only when asked, and keep `MEASURE_STOP_AT` set.
- The version lives in both `pyproject.toml` and `src/skinflint/__init__.py` (and
  `CITATION.cff`, `uv.lock`); bump them together. User-visible changes get a line under
  "Unreleased" in `CHANGELOG.md`.
- Releases are published on GitHub only; users install from the git URL (no PyPI release).
- Scratch files go in `tmp/` (gitignored). Do not create `.bak` copies; use git.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. The repository
  is public: change it through pull requests with CI, not direct pushes to main. No personal
  names, local user paths or machine details in committed files, commits or logs.

## How to test

```sh
uv sync --group dev
uv run pytest                       # offline, about 1.5 min
uv run ruff check .
uv run ruff format --check .
uv run python scripts/smoke.py      # end-to-end: real server, fake upstream
uv run python scripts/bench.py      # proxy overhead (noisy; order of magnitude only)
```

CI (`.github/workflows/ci.yml`) runs lint and tests on Linux, macOS and Windows with Python
3.11-3.13, then builds the wheel, installs it into a fresh environment and runs the CLI
against a fake upstream.

## Status and next steps

Updated: 2026-10-30 (0.1.1 prepared on a branch, not tagged).

Done:
- 0.1.0 tagged and released on GitHub (2026-10-09).
- Since then: in-flight reservation refresh, estimated settlement of cut-off streams (with
  cache-aware input), a complete worst-case bound, Windows exclusive bind, `run` setting
  `ENABLE_TOOL_SEARCH=true`, measured results (sections 6-9) and CITATION.cff. All under
  0.1.1 in the changelog, release notes in `docs/release-notes-0.1.1.md`.
- Checks on 2026-10-30: `pytest` 554 passed, `ruff check` and `ruff format --check` clean,
  `scripts/smoke.py` passed 5 of 6 local runs (see 2 below).

Open:
1. Release 0.1.1: merge the branch, tag `v0.1.1`, publish the GitHub release with
   `docs/release-notes-0.1.1.md` (owner action).
2. `scripts/smoke.py` failed once locally on Windows with "skinflint serve did not become
   healthy" (20 s health wait) on the first run of a session, then passed five times in a
   row. Probably a cold start; not reproduced or diagnosed.
3. `docs/results.md` sections 6-9 cite commit `0f57ee1`, which exists only on the remote
   branch `claude/fix-review-2026-10-10` (merged into main as a squash in #4). Keep that branch, or
   re-cite the measured build, before pruning old remote branches.
4. The open questions in README (overhead vs MCP servers and skills, real-session cache
   breaks, a tighter worst-case bound, estimate overshoot at higher parallelism, the
   163-524-token direct/proxied gap, Codex overhead) need new live measurements; each costs
   API or subscription spend.
