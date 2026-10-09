import json
import time
from pathlib import Path

import pytest

from skinflint.model import Endpoint, Provider
from skinflint.segments import calibrate, canonical, classify_text, fingerprint, segment

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
A = (Provider.ANTHROPIC, Endpoint.MESSAGES)


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.request.json").read_text(encoding="utf-8"))


def test_first_turn_tools_and_groups():
    segs = segment(*A, load("first_turn"))
    tools = [s for s in segs if s.section == "tools"]
    assert len(tools) == 42
    mcp = [s for s in tools if s.group == "tools: mcp claude_ai_Claude_Docs"]
    assert len(mcp) == 8
    assert all(s.kind == "tool" for s in tools)
    assert sum(s.group == "tools: built-in" for s in tools) == 34
    assert segs[0].label == "Agent"


def test_first_turn_system_and_context():
    segs = segment(*A, load("first_turn"))
    system = [s for s in segs if s.section == "system"]
    assert [s.label for s in system] == ["billing header", "system[1]", "system[2]"]
    assert [s.breakpoint for s in system] == [False, True, True]
    assert system[1].ttl == "1h"
    msgs = [s for s in segs if s.section == "messages"]
    assert [s.label for s in msgs] == [
        "environment",
        "model",
        "agent types",
        "mcp instructions",
        "skills",
        "instructions",  # sanitising removed the "Contents of .../CLAUDE.md" lines
        "user context",
        "date",
        "attribution",
        "user prompt",
    ]
    assert msgs[5].kind == "instructions"
    assert msgs[4].group == "context: skills"
    assert msgs[-1].kind == "user_text" and msgs[-1].breakpoint and msgs[-1].ttl == "1h"
    assert all(s.message == 0 for s in msgs)
    assert sum(s.chars for s in tools_of(segs)) > 160_000


def tools_of(segs):
    return [s for s in segs if s.section == "tools"]


def test_tool_turn_blocks():
    segs = segment(*A, load("tool_turn"))
    tail = [(s.kind, s.label, s.group, s.message) for s in segs[-5:]]
    assert tail == [
        ("thinking", "thinking", "thinking", 1),
        ("tool_use", "Read", "tool calls", 1),
        ("tool_use", "Glob", "tool calls", 1),
        ("tool_result", "Read", "tool results: Read", 2),
        ("tool_result", "Glob", "tool results: Glob", 2),
    ]
    assert segs[-1].breakpoint and segs[-3].breakpoint


def test_hash_ignores_cache_control_and_key_order():
    first = segment(*A, load("first_turn"))
    tool = segment(*A, load("tool_turn"))
    # The user prompt lost its cache_control in the next turn but hashes the same.
    assert first[-1].breakpoint and not tool[54].breakpoint
    assert first[-1].hash == tool[54].hash
    assert len(first[-1].hash) == 16
    assert canonical({"b": 1, "a": {"cache_control": {"type": "ephemeral"}, "x": [1]}}) == (
        '{"a":{"x":[1]},"b":1}'
    )


def test_instruction_file_labels():
    text = (
        "<system-reminder>\nCodebase and user instructions are shown below.\n\n"
        "Contents of C:\\Users\\me\\.claude\\CLAUDE.md (user's private global instructions):\n"
        "be concise\n\nContents of /home/me/proj/AGENTS.md (project instructions):\nx\n"
    )
    assert classify_text(text) == ("instructions", "CLAUDE.md, AGENTS.md", "instruction files")
    assert classify_text("plain prompt") is None
    assert classify_text("<system-reminder>\nsomething new")[1] == "other"


def test_string_system_and_content_and_automatic_cache_control():
    body = {
        "system": "be brief",
        "cache_control": {"type": "ephemeral"},
        "tools": [
            {"name": "mcp__github__create_issue", "input_schema": {}},
            {"type": "web_search_20250305", "name": "web_search"},
        ],
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
                    {"type": "document", "source": {"type": "text", "data": "doc"}},
                ],
            },
        ],
    }
    segs = segment(*A, body)
    assert [(s.kind, s.group) for s in segs] == [
        ("tool", "tools: mcp github"),
        ("server_tool", "tools: server"),
        ("system", "system prompt"),
        ("user_text", "user prompts"),
        ("assistant_text", "assistant text"),
        ("image", "images"),
        ("document", "documents"),
    ]
    assert segs[-1].breakpoint and segs[-1].ttl == "5m"
    assert sum(s.breakpoint for s in segs) == 1


def test_calibrate_sums_exactly():
    segs = segment(*A, load("first_turn"))
    calibrate(segs, 57_818)
    assert sum(s.est_tokens for s in segs) == 57_818
    for target in (1, 7, 999_983):
        calibrate(segs, target)
        assert sum(s.est_tokens for s in segs) == target
    calibrate(segs, 0)
    assert sum(s.est_tokens for s in segs) > 40_000


def test_calibrate_images_fixed():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": "A" * 100_000}},
                    {"type": "text", "text": "x" * 4000},
                ],
            }
        ]
    }
    segs = segment(*A, body)
    calibrate(segs, 3000)
    assert segs[0].est_tokens == 1600
    assert segs[1].est_tokens == 1400
    calibrate(segs, 0)
    assert segs[0].est_tokens == 1600
    assert 950 < segs[1].est_tokens < 1050


def test_fingerprint():
    main = fingerprint(segment(*A, load("first_turn")))
    assert len(main) == 8
    assert fingerprint(segment(*A, load("tool_turn"))) == main
    assert fingerprint(segment(*A, load("fresh_session"))) == main
    assert fingerprint(segment(*A, load("side_request"))) != main
    sub = load("first_turn")
    sub["tools"] = sub["tools"][:10]
    assert fingerprint(segment(*A, sub)) != main
    billing = load("first_turn")
    billing["system"][0]["text"] = "x-anthropic-billing-header: cc_version=9.9.9;"
    assert fingerprint(segment(*A, billing)) == main


def test_openai_chat():
    body = {
        "tools": [
            {"type": "function", "function": {"name": "shell", "parameters": {}}},
            {"type": "function", "function": {"name": "mcp__linear__search", "parameters": {}}},
        ],
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "developer", "content": [{"type": "text", "text": "dev"}]},
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        ],
    }
    segs = segment(Provider.OPENAI, Endpoint.CHAT, body)
    assert [(s.section, s.kind, s.label, s.group) for s in segs] == [
        ("tools", "tool", "shell", "tools: built-in"),
        ("tools", "tool", "mcp__linear__search", "tools: mcp linear"),
        ("system", "system", "system[0]", "system prompt"),
        ("system", "system", "developer[1]", "system prompt"),
        ("messages", "user_text", "user prompt", "user prompts"),
        ("messages", "tool_use", "shell", "tool calls"),
        ("messages", "tool_result", "shell", "tool results: shell"),
        ("messages", "image", "image", "images"),
    ]
    assert segs[4].message == 2 and segs[6].message == 4


def test_openai_responses():
    body = {
        "instructions": "you are codex",
        "tools": [
            {"type": "function", "name": "shell", "parameters": {}},
            {"type": "web_search"},
            {"type": "mcp", "server_label": "deepwiki", "server_url": "https://x"},
        ],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions for /repo\n\nrules"},
                    {
                        "type": "input_text",
                        "text": "<environment_context>cwd</environment_context>",
                    },
                ],
            },
            {"role": "user", "content": "fix the bug"},
            {"type": "reasoning", "summary": []},
            {"type": "function_call", "call_id": "k", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "k", "output": "ok"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        ],
    }
    segs = segment(Provider.OPENAI, Endpoint.RESPONSES, body)
    assert [(s.kind, s.label, s.group) for s in segs] == [
        ("tool", "shell", "tools: built-in"),
        ("server_tool", "web_search", "tools: server"),
        ("server_tool", "deepwiki", "tools: mcp deepwiki"),
        ("system", "instructions", "system prompt"),
        ("instructions", "AGENTS.md", "instruction files"),
        ("reminder", "environment", "context: environment"),
        ("user_text", "user prompt", "user prompts"),
        ("thinking", "reasoning", "thinking"),
        ("tool_use", "shell", "tool calls"),
        ("tool_result", "shell", "tool results: shell"),
        ("assistant_text", "assistant", "assistant text"),
    ]
    simple = segment(Provider.OPENAI, Endpoint.RESPONSES, {"input": "hi"})
    assert [(s.kind, s.message) for s in simple] == [("user_text", 0)]


def test_malformed_bodies_do_not_raise():
    assert segment(*A, {}) == []
    assert segment(*A, {"messages": [None, {"role": "user", "content": [1, None]}]})
    assert segment(Provider.OPENAI, Endpoint.CHAT, {"messages": "nope", "tools": [3]}) == []


def big_body(n: int) -> dict:
    body = load("tool_turn")
    msgs = body["messages"]
    for i in range(n - len(msgs)):
        if i % 2 == 0:
            msgs.append(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me look. " * 20},
                        {
                            "type": "tool_use",
                            "id": f"t{i}",
                            "name": "Read",
                            "input": {"file_path": f"f{i}"},
                        },
                    ],
                }
            )
        else:
            msgs.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i - 1}",
                            "content": "line\n" * 400,
                        },
                    ],
                }
            )
    return body


def test_segment_300_messages_fast():
    body = big_body(300)
    assert len(body["messages"]) == 300
    best = float("inf")
    for _ in range(3):
        t = time.perf_counter()
        segs = segment(*A, body)
        best = min(best, time.perf_counter() - t)
    assert len(segs) > 500
    assert best < 0.05, best


@pytest.mark.parametrize("name", ["first_turn", "tool_turn", "fresh_session", "side_request"])
def test_fixture_chars_cover_body(name):
    body = load(name)
    segs = segment(*A, body)
    total = sum(s.chars for s in segs)
    raw = len(json.dumps({k: body[k] for k in ("tools", "system", "messages") if k in body}))
    assert 0.8 * raw < total <= raw


def _sha(block) -> str:
    import hashlib

    return hashlib.sha256(canonical(block).encode()).hexdigest()[:16]


def test_anthropic_tool_result_media_split():
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "A" * 400_000},
    }
    pdf = {"type": "document", "source": {"type": "base64", "data": "B" * 200_000}}
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "a.png"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "x" * 350}, image, pdf],
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
            },
        ]
    }
    segs = segment(*A, body)
    assert [(s.kind, s.label, s.group, s.message) for s in segs[1:]] == [
        ("tool_result", "Read", "tool results: Read", 1),
        ("image", "tool_result:Read image", "images", 1),
        ("document", "tool_result:Read document", "documents", 1),
    ]
    assert segs[2].hash == _sha(image) and segs[3].hash == _sha(pdf)
    assert segs[1].chars < 500
    assert [s.breakpoint for s in segs] == [False, False, False, True]
    assert segs[3].ttl == "1h"
    calibrate(segs, 3400)
    assert segs[2].est_tokens == 1600  # images are fixed; documents scale with size
    assert segs[3].est_tokens > segs[1].est_tokens
    assert sum(s.est_tokens for s in segs) == 3400
    # The text segment's hash does not depend on the image bytes.
    body["messages"][1]["content"][0]["content"][1] = {**image, "source": {"data": "C"}}
    again = segment(*A, body)
    assert again[1].hash == segs[1].hash and again[2].hash != segs[2].hash


def test_openai_media_in_tool_results():
    data_uri = "data:image/png;base64," + "A" * 300_000
    chat = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "screenshot"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri}}]},
        ]
    }
    segs = segment(Provider.OPENAI, Endpoint.CHAT, chat)
    assert [(s.kind, s.label, s.group) for s in segs] == [
        ("tool_use", "screenshot", "tool calls"),
        ("tool_result", "screenshot", "tool results: screenshot"),
        ("image", "tool_result:screenshot image", "images"),
        ("image", "image", "images"),
    ]
    assert segs[2].hash == segs[3].hash == _sha(chat["messages"][2]["content"][0])
    calibrate(segs, 3300)
    assert segs[2].est_tokens == segs[3].est_tokens == 1600

    responses = {
        "input": [
            {"type": "function_call", "call_id": "k", "name": "view_image", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "k",
                "output": [
                    {"type": "input_image", "image_url": data_uri},
                    {"type": "input_file", "file_data": "Z" * 1000},
                ],
            },
            {"role": "user", "content": [{"type": "input_image", "image_url": data_uri}]},
        ]
    }
    segs = segment(Provider.OPENAI, Endpoint.RESPONSES, responses)
    assert [(s.kind, s.label) for s in segs] == [
        ("tool_use", "view_image"),
        ("tool_result", "view_image"),
        ("image", "tool_result:view_image image"),
        ("document", "tool_result:view_image document"),
        ("image", "image"),
    ]
    assert segs[1].chars < 200
    string_output = segment(
        Provider.OPENAI,
        Endpoint.RESPONSES,
        {"input": [{"type": "function_call_output", "call_id": "z", "output": "plain"}]},
    )
    assert [s.kind for s in string_output] == ["tool_result"]
