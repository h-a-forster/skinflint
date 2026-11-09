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
  `CITATION.cff`, `uv.lock`); bump them together. User-visible changes get a note in
  `docs/release-notes-<version>.md`; do not hand-edit `CHANGELOG.md`.
- Releases are published on GitHub only; users install from the git URL (no PyPI release).
- Scratch files go in `tmp/` (gitignored). Do not create `.bak` copies; use git.
- The repository is public: change it through pull requests with CI (merge commits, never
  squash: `docs/results.md` cites commits), not direct pushes to main. No personal names, local
  user paths or machine details in committed files, commits or logs.

## How to test

```sh
uv sync --group dev
uv run pytest                       # offline, about 30 s
uv run ruff check .
uv run ruff format --check .
uv run python scripts/smoke.py      # end-to-end: real server, fake upstream
uv run python scripts/bench.py      # proxy overhead (noisy; order of magnitude only)
```

CI (`.github/workflows/ci.yml`) runs lint and tests on Linux, macOS and Windows with Python
3.11-3.13, then builds the wheel, installs it into a fresh environment and runs the CLI
against a fake upstream. Actions are pinned to commit SHAs.

Open work is in `backlog.md`.
