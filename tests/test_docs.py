"""Every number in README.md and CITATION.cff comes from the measurements or the price table.

`scripts/measure/analyse.py` rebuilds `measurements/<date>/summary.json` from the committed raw
files, offline; CI runs it and fails on any diff. Bounds that follow from configuration (the
default output reservation times a list price) are computed here from `prices.toml`. A claim
is a fragment of a document with `{key}` placeholders; anything else with a digit is listed as
fixed.
"""

import json
import tomllib
from pathlib import Path

import pytest
from docnumbers import check

from skinflint.budget import DEFAULT_MAX_OUTPUT

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "measurements" / "2026-10-10" / "summary.json"
PRICES = ROOT / "src" / "skinflint" / "data" / "prices.toml"


def values() -> dict[str, str]:
    out = json.loads(MEASURED.read_text(encoding="utf-8"))
    models = tomllib.loads(PRICES.read_text(encoding="utf-8"))["anthropic"]["models"]
    opus, sonnet = models["claude-opus-5-5"], models["claude-sonnet-5-5"]
    out["bound.output_tokens"] = f"{DEFAULT_MAX_OUTPUT:,}"
    out["bound.opus"] = f"{DEFAULT_MAX_OUTPUT * opus['output'] / 1e6:.2f}"
    out["bound.opus_fast"] = f"{DEFAULT_MAX_OUTPUT * opus['fast']['output'] / 1e6:.2f}"
    out["bound.sonnet"] = f"{DEFAULT_MAX_OUTPUT * sonnet['output'] / 1e6:.2f}"
    return out


README = [
    "by up to {caps.estimate.max_over_0} in",
    "sends {overhead.on.prompt} prompt tokens with tool search on, {overhead.on.builtin} of them "
    "built-in tool definitions",
    "sends {overhead.off.prompt} tokens instead of {overhead.direct.prompt} "
    "(+{overhead.proxy_vs_direct})",
    "spend ended between ${caps.estimate.min} and ${caps.estimate.max} on a "
    "${caps.estimate.cap} cap (up to {caps.estimate.max_over} over)",
    "reserved ${caps.worst_case.claude-sonnet-5-5.min} for a first Sonnet 5.5 request "
    "(${caps.worst_case.claude-sonnet-5-5.max} later in a session) and "
    "${caps.worst_case.claude-opus-5-5.min} for a first Opus 5.5 request",
    "re-wrote {cache.mcp.rewritten} tokens and made turn 2 cost {cache.mcp.times_control} times "
    "the control",
    "Across {ledger.runs_compared} runs the totals were identical, {ledger.ledger_total} each",
    "Four servers cost {overhead.mcp.on} tokens with tool search on and {overhead.mcp.off} with "
    "it off",
    "It reserves {bound.output_tokens} output tokens when a request sets no `max_tokens`",
    "{caps.estimate.agents_min}-{caps.estimate.agents_max} agents, one task",
    "By {overhead.direct_gap.min}-{overhead.direct_gap.max} tokens",
    "up to ${bound.opus} per in-flight Claude Code request on Claude Opus 5.5 with "
    "{bound.output_tokens} output tokens (${bound.opus_fast} in fast mode, ${bound.sonnet} on "
    "Sonnet 5.5)",
    "Measured with {caps.estimate.agents_min}-{caps.estimate.agents_max} parallel Claude Code "
    "agents on a ${caps.estimate.cap} cap, spend ended between ${caps.estimate.min_2} and "
    "${caps.estimate.max_2}",
    "${caps.worst_case.claude-sonnet-5-5.min} for a first Claude Code request on Sonnet 5.5 (up "
    "to ${caps.worst_case.claude-sonnet-5-5.max} later in a session), "
    "${caps.worst_case.claude-opus-5-5.min} on Opus 5.5",
]

FIXED = {
    "README.md": [
        "Python 3.11+",
        "127.0.0.1:4100",
        "Claude Code 2.1.296 and earlier",
        "Sonnet 5.5",
        "Opus 5.5",
        "Python 3.14",
        "(5m and 1h)",
        "section 6",
        "section 7",
        "section 8",
        "section 9",
        "Section 8",
        "Seven rounds",
        "turn 2 cost",
    ],
    "CITATION.cff": ["cff-version: 1.2.0", "version: 0.1.1"],
}

CLAIMS = {"README.md": README, "CITATION.cff": []}


@pytest.mark.parametrize("name", sorted(CLAIMS))
def test_every_number_is_copied_from_the_measurements(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    result = check(text, CLAIMS[name], FIXED[name], values())
    problems = [f"claim not found: {c}" for c in result.missing_claims]
    problems += [f"fixed text not found: {c}" for c in result.missing_fixed]
    problems += [f"number not from the measurements: {c}" for c in result.unchecked]
    assert not problems, "\n".join(problems)


def test_the_check_catches_a_changed_or_unlisted_number():
    text = "Spend ended at $0.8906 (9.0% over). See `gpt-5.6-sol`, 2026-10-10, v0.1.1."
    claims = ["${a} ({b} over)"]
    numbers = {"a": "0.8906", "b": "9.0%"}
    clean = check(text, claims, [], numbers)
    assert not (clean.missing_claims or clean.unchecked)
    assert check(text, claims, [], {**numbers, "a": "0.8907"}).missing_claims
    assert check(text + " Also 12 more.", claims, [], numbers).unchecked
