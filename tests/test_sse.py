import random
import time
from pathlib import Path

import pytest

from skinflint.sse import SSEEvent, SSEParser

FIXTURES = Path(__file__).parent / "fixtures"
SSE_FILES = sorted((FIXTURES / "claude_code").glob("*.sse")) + sorted(
    (FIXTURES / "openai").glob("*.sse")
)


def parse_all(data: bytes, sizes) -> list[SSEEvent]:
    parser = SSEParser()
    events: list[SSEEvent] = []
    i = 0
    for size in sizes:
        if i >= len(data):
            break
        events += parser.feed(data[i : i + size])
        i += size
    if i < len(data):
        events += parser.feed(data[i:])
    events += parser.close()
    return events


def one_byte(n: int):
    return [1] * n


def random_sizes(n: int, seed: int):
    rng = random.Random(seed)
    sizes, total = [], 0
    while total < n:
        sizes.append(rng.randint(1, 97))
        total += sizes[-1]
    return sizes


def fields(events):
    return [(e.event, e.data) for e in events if e.event is not None or e.data]


def test_basic_event_and_data():
    events = parse_all(b"event: ping\ndata: {}\n\n", [100])
    assert events == [SSEEvent("ping", "{}", b"event: ping\ndata: {}\n\n")]


def test_multiline_data_and_comment_and_no_space():
    events = parse_all(b": hello\nevent:x\ndata:a\ndata: b\nid: 7\nretry: 5\n\n", [100])
    assert fields(events) == [("x", "a\nb")]


def test_line_endings_crlf_and_cr():
    for sep in (b"\r\n", b"\r"):
        raw = sep.join([b"event: a", b"data: 1", b"", b"data: 2", b"", b""])
        events = parse_all(raw, one_byte(len(raw)))
        assert fields(events) == [("a", "1"), (None, "2")]
        assert b"".join(e.raw for e in events) == raw


def test_crlf_split_between_cr_and_lf_is_one_terminator():
    parser = SSEParser()
    assert parser.feed(b"data: 1\r\n\r") == []
    events = parser.feed(b"\ndata: 2\r\n\r\n")
    assert [(e.data, e.raw) for e in events] == [
        ("1", b"data: 1\r\n\r\n"),
        ("2", b"data: 2\r\n\r\n"),
    ]


def test_final_event_without_blank_line_needs_close():
    parser = SSEParser()
    assert parser.feed(b"event: message_stop\ndata: {}") == []
    events = parser.close()
    assert fields(events) == [("message_stop", "{}")]
    assert events[0].raw == b"event: message_stop\ndata: {}"


def test_stray_blank_lines_keep_bytes():
    raw = b"\n\ndata: x\n\n\n"
    events = parse_all(raw, [3, 4, 100])
    assert fields(events) == [(None, "x")]
    assert b"".join(e.raw for e in events) == raw


def test_non_utf8_bytes_are_replaced():
    events = parse_all(b"data: \xff\xfeok\n\n", [100])
    assert events[0].data == "��ok"


def test_multibyte_char_split_across_chunks():
    raw = "data: café ☕\n\n".encode()
    events = parse_all(raw, one_byte(len(raw)))
    assert events[0].data == "café ☕"


def test_parser_reusable_after_close():
    parser = SSEParser()
    parser.feed(b"data: 1")
    parser.close()
    assert fields(parser.feed(b"data: 2\n\n")) == [(None, "2")]


@pytest.mark.parametrize("path", SSE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("crlf", [False, True])
def test_fixtures_same_events_for_any_chunking(path, crlf):
    raw = path.read_bytes()
    if crlf:
        raw = raw.replace(b"\n", b"\r\n")
    whole = parse_all(raw, [len(raw)])
    assert b"".join(e.raw for e in whole) == raw
    for sizes in (one_byte(len(raw)), random_sizes(len(raw), 1), random_sizes(len(raw), 2)):
        events = parse_all(raw, sizes)
        assert fields(events) == fields(whole)
        assert b"".join(e.raw for e in events) == raw


def test_large_single_event_fed_in_small_chunks_is_linear():
    payload = b"data: " + b"x" * 2_000_000 + b"\n\n"
    parser = SSEParser()
    t0 = time.perf_counter()
    events = []
    for i in range(0, len(payload), 64):
        events += parser.feed(payload[i : i + 64])
    assert time.perf_counter() - t0 < 5
    assert len(events) == 1 and len(events[0].data) == 2_000_000


def test_many_events_in_one_chunk():
    raw = b"event: ping\ndata: {}\n\n" * 50_000
    t0 = time.perf_counter()
    events = SSEParser().feed(raw)
    assert time.perf_counter() - t0 < 5
    assert len(events) == 50_000
