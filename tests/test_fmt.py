from datetime import datetime

import pytest

from skinflint import fmt


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0"),
        (950, "950"),
        (999, "999"),
        (1000, "1k"),
        (1234, "1.2k"),
        (57_818, "57.8k"),
        (99_949, "99.9k"),
        (99_960, "100k"),
        (250_400, "250k"),
        (999_600, "1M"),
        (1_200_000, "1.2M"),
        (2_000_000_000, "2B"),
        (-57_818, "-57.8k"),
        (None, "-"),
    ],
)
def test_tokens(n, expected):
    assert fmt.tokens(n) == expected


@pytest.mark.parametrize(
    ("usd", "expected"),
    [
        (0, "$0"),
        (0.0731, "$0.0731"),
        (0.115836, "$0.1158"),
        (0.00001, "<$0.0001"),
        (0.99996, "$1.00"),
        (1, "$1.00"),
        (20.04, "$20.04"),
        (1234.5, "$1,234.50"),
        (-0.5, "-$0.5000"),
        (None, "-"),
    ],
)
def test_money(usd, expected):
    assert fmt.money(usd) == expected


def test_percent():
    assert fmt.percent(0.423) == "42%"
    assert fmt.percent(0.4237, 1) == "42.4%"
    assert fmt.percent(None) == "-"


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        (0.35, "350ms"),
        (1.94, "1.9s"),
        (42.4, "42s"),
        (432, "7m12s"),
        (3 * 3600 + 5 * 60 + 9, "3h05m"),
        (2 * 86400 + 3 * 3600, "2d03h"),
        (None, "-"),
    ],
)
def test_duration(s, expected):
    assert fmt.duration(s) == expected


def test_timestamps():
    now = datetime(2026, 10, 9, 15, 30).timestamp()
    assert fmt.timestamp(datetime(2026, 10, 9, 9, 5).timestamp(), now) == "09:05"
    assert fmt.timestamp(datetime(2026, 3, 2, 9, 5).timestamp(), now) == "Mar 02 09:05"
    assert fmt.timestamp(datetime(2025, 3, 2, 9, 5).timestamp(), now) == "2025-03-02 09:05"
    assert fmt.relative(now - 2, now) == "just now"
    assert fmt.relative(now - 30, now) == "30s ago"
    assert fmt.relative(now - 300, now) == "5m00s ago"
    assert fmt.relative(now + 3 * 3600 + 300, now) == "in 3h05m"


def test_table_alignment():
    out = fmt.table(
        [["claude-opus-5-5", 57_818, "$0.0731", "42%"], ["gpt-5.4", 950, "$12.40", "-"]],
        ["model", "tokens", "cost", "hit"],
        title="By model",
    )
    assert out.splitlines() == [
        "By model",
        "model            tokens     cost  hit",
        "---------------  ------  -------  ---",
        "claude-opus-5-5   57818  $0.0731  42%",
        "gpt-5.4             950   $12.40    -",
    ]


def test_table_explicit_align_and_ragged_rows():
    out = fmt.table([["a", "1"], ["bb"]], align="ll")
    assert out.splitlines() == ["a   1", "bb"]


def test_table_shrink():
    rows = [["a-very-long-label-that-will-not-fit", "$1.00"]]
    out = fmt.table(rows, ["label", "cost"], shrink=0, width=20)
    lines = out.splitlines()
    assert all(len(line) <= 20 for line in lines)
    assert lines[2].startswith("a-very-lon...")
    assert lines[2].endswith("$1.00")


def test_table_shrink_respects_minimum():
    out = fmt.table([["abcdefghijklmnop", "x"]], shrink=0, width=5, min_shrink=8)
    assert out == "abcde...  x"


def test_table_empty():
    assert fmt.table([]) == ""
    assert fmt.table([], ["a", "b"]).splitlines() == ["a  b", "-  -"]
