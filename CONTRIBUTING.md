# Contributing

Bug reports, price updates, client recipes and fixes are welcome.

## Setup

You need [uv](https://docs.astral.sh/uv/) and git.

```sh
uv sync --group dev
uv run skinflint --version
```

## Checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/smoke.py      # end-to-end: real server, fake upstream
uv run python scripts/bench.py      # proxy overhead
```

CI runs tests and lint on Linux, macOS and Windows with Python 3.11 to 3.13, then installs the
built wheel and runs the smoke test.

Test rules:

- No network. Use the fake upstream in `tests/fakes.py`.
- Use `tmp_path` for every file, including the ledger.
- Must pass on Windows: `pathlib`, no shell assumptions.

## Layout

| Path | Contents |
|---|---|
| `src/skinflint/model.py` | Shared types. Imports nothing from the package. |
| `src/skinflint/config.py` | TOML config, strict validation. |
| `src/skinflint/proxy.py` | aiohttp server: routing, admission, forwarding, settling. |
| `src/skinflint/providers/` | Per-provider request parsing, usage extraction, stream tracking, error shapes. |
| `src/skinflint/sse.py` | Incremental server-sent events parser. |
| `src/skinflint/pricing.py`, `data/prices.toml` | Price table and cost calculation. |
| `src/skinflint/store.py` | SQLite ledger. |
| `src/skinflint/budget.py` | Budget rules, windows, atomic admission. |
| `src/skinflint/segments.py` | Splits request bodies into segments; token attribution. |
| `src/skinflint/profile.py` | Request and session profiles, diffs, suggestions. |
| `src/skinflint/cachedoctor.py` | Cache-miss verdicts and causes. |
| `src/skinflint/cli.py`, `report.py`, `statusline.py`, `fmt.py` | Command line and text output. |

## Guidelines

- Proxying must never fail because metering failed. Parsing code catches its own errors,
  keeps the best data it has, and records what went wrong.
- Never log or store credentials. Request bodies are stored only when the user opts in.
- Runtime dependencies: aiohttp, and backports.zstd before Python 3.14. Discuss new ones in
  an issue first.
- Match the surrounding style; ruff enforces the rest (line length 100).

## Updating prices

Edit `src/skinflint/data/prices.toml`, update `as_of`, and cite the provider's pricing page
in the pull request. `tests/test_pricing.py` checks that the table is consistent.

## Pull requests

One change per pull request, with tests and docs. Add a line under "Unreleased" in
[CHANGELOG.md](CHANGELOG.md) for user-visible changes.

Security issues: see [SECURITY.md](SECURITY.md).
