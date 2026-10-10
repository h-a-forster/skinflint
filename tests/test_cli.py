import asyncio
import json
import socket
import sys
import time

import pytest
from aiohttp import web

from skinflint import __version__, cli
from skinflint.model import Endpoint, Provider, Record, State, Usage
from skinflint.segments import calibrate, segment
from skinflint.store import Store

NOW = time.time()
S1 = "aaaa1111-0000-4000-8000-000000000001"
S2 = "aaaa2222-0000-4000-8000-000000000002"


def body(n: int) -> dict:
    return {
        "model": "claude-sonnet-5-5",
        "max_tokens": 100,
        "system": [
            {"type": "text", "text": "You are terse. " * 50, "cache_control": {"type": "ephemeral"}}
        ],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file. " * 30,
                "input_schema": {"type": "object"},
            },
            {"name": "mcp__gh__issue", "description": "Make an issue. " * 30, "input_schema": {}},
        ],
        "messages": [{"role": "user", "content": f"question {i}"} for i in range(n)],
    }


def add(store: Store, segs_body: dict | None = None, **kw) -> int:
    base = dict(
        ts=NOW - 600,
        provider=Provider.ANTHROPIC,
        endpoint=Endpoint.MESSAGES,
        scope="proj",
        model="claude-sonnet-5-5",
        state=State.OK,
        session=S1,
        client="claude-cli/2.1.287",
        usage=Usage(input_tokens=100, cache_write_5m=900, output_tokens=50),
        cost_usd=0.01,
    )
    base.update(kw)
    r = Record(**base)
    rid = store.insert(r)
    if segs_body is not None:
        segs = segment(Provider.ANTHROPIC, Endpoint.MESSAGES, segs_body)
        calibrate(segs, r.usage.prompt_tokens)
        store.finish(rid, r, segs)
    return rid


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SKINFLINT_HOME", str(tmp_path))
    monkeypatch.delenv("SKINFLINT_CONFIG", raising=False)
    return tmp_path


@pytest.fixture
def ledger(home):
    store = Store(home / "skinflint.db")
    add(store, body(1))
    add(
        store,
        body(3),
        ts=NOW - 500,
        usage=Usage(input_tokens=20, cache_read=900, output_tokens=40),
        cost_usd=0.002,
    )
    add(store, ts=NOW - 450, state=State.BLOCKED, usage=Usage(), cost_usd=0, blocked_by="daily")
    add(
        store,
        ts=NOW - 400,
        session=S2,
        scope="ci-1",
        model="gpt-x",
        provider=Provider.OPENAI,
        endpoint=Endpoint.CHAT,
        cost_estimated=True,
    )
    add(store, ts=NOW - 10 * 86400, session=None, cost_usd=5.0)
    store.close()
    return home


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_version(capsys):
    code, out, _ = run(capsys, "--version")
    assert code == 0 and out.strip() == f"skinflint {__version__}"


def test_usage_errors(capsys, home):
    assert run(capsys)[0] == 2
    assert run(capsys, "nope")[0] == 2
    code, _, err = run(capsys, "report", "--by", "color")
    assert code == 2 and "invalid choice" in err
    code, _, err = run(capsys, "report", "--since", "yesterday")
    assert code == 2 and err.startswith("skinflint: error: bad time 'yesterday'")


def test_parse_time():
    now = 1_000_000.0
    assert cli.parse_time("30m", now) == now - 1800
    assert cli.parse_time("24h", now) == now - 86400
    assert cli.parse_time("7d", now) == now - 7 * 86400
    assert cli.parse_time("2w", now) == now - 14 * 86400
    assert cli.parse_time("all", now) is None
    assert cli.parse_time("2026-10-01") == time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, -1))
    for bad in ("5y", "h", "-3d", "2026-13-01"):
        with pytest.raises(cli.CliError):
            cli.parse_time(bad, now)


def test_no_ledger_yet(capsys, home):
    for argv in (["report"], ["sessions"], ["profile"], ["cache"], ["diff", "1", "2"]):
        code, out, _ = run(capsys, *argv)
        assert code == 0
        assert out.strip() == "no requests recorded yet; start the proxy with `skinflint serve`"
    code, out, _ = run(capsys, "report", "--json")
    assert code == 0 and json.loads(out)["rows"] == []
    code, out, _ = run(capsys, "sessions", "--json")
    assert json.loads(out) == []
    assert not (home / "skinflint.db").exists()


def test_report_text_and_json(capsys, ledger):
    code, out, _ = run(capsys, "report")
    assert code == 0
    lines = out.splitlines()
    assert lines[1].split()[0] == "model"
    assert any(ln.startswith("claude-sonnet-5-5") and "$0.0120" in ln for ln in lines)
    assert any(ln.startswith("gpt-x") and ln.endswith("$0.0100~") for ln in lines)
    assert "~ estimated" in out
    code, out, _ = run(capsys, "report", "--json", "--since", "all", "--by", "scope")
    data = json.loads(out)
    assert data["by"] == "scope" and data["since"] is None
    assert {r["key"] for r in data["rows"]} == {"proj", "ci-1"}
    assert data["total"]["requests"] == 4 and data["total"]["blocked"] == 1
    assert data["total"]["cost_estimated"] is True


@pytest.mark.parametrize("by", cli.BY)
def test_report_by_every_key(capsys, ledger, by):
    code, out, _ = run(capsys, "report", "--by", by, "--json")
    assert code == 0 and json.loads(out)["by"] == by


def test_report_filters_and_session_prefix(capsys, ledger):
    code, out, _ = run(capsys, "report", "--session", "aaaa1", "--json")
    data = json.loads(out)
    assert code == 0 and data["filters"] == {"session": S1}
    assert data["total"]["requests"] == 2
    code, _, err = run(capsys, "report", "--session", "aaaa")
    assert code == 1 and "ambiguous" in err and S1 in err and S2 in err
    code, _, err = run(capsys, "report", "--session", "aa")
    assert code == 1 and "at least 4" in err
    code, _, err = run(capsys, "report", "--session", "zzzz")
    assert code == 1 and "no session matches" in err
    code, out, _ = run(capsys, "report", "--scope", "ci-1", "--json")
    assert json.loads(out)["total"]["requests"] == 1


def test_sessions(capsys, ledger):
    code, out, _ = run(capsys, "sessions")
    lines = out.splitlines()
    assert code == 0 and lines[2].startswith("aaaa2222  ci-1")
    code, out, _ = run(capsys, "sessions", "--json", "--limit", "1")
    data = json.loads(out)
    assert len(data) == 1 and data[0]["id"] == S2
    assert data[0]["usage"]["prompt_tokens"] == 1000


def test_profile_request_and_session(capsys, ledger):
    code, out, _ = run(capsys, "profile")
    assert code == 0 and out.startswith("request #2 ")
    assert "tools: built-in" in out
    code, out, _ = run(capsys, "profile", "1", "--json")
    data = json.loads(out)
    assert data["record"]["id"] == 1 and data["profile"]["total_tokens"] == 1000
    code, _, err = run(capsys, "profile", "4")
    assert code == 1 and "no stored profile" in err
    code, _, err = run(capsys, "profile", "99")
    assert code == 1 and "no request #99" in err
    code, _, err = run(capsys, "profile", "abc")
    assert code == 1 and "bad request id" in err
    code, out, _ = run(capsys, "profile", "--session", S1[:8])
    assert code == 0 and out.startswith(f"session {S1}  2 requests")
    assert "unused MCP server" in out
    code, out, _ = run(capsys, "profile", "--session", "last", "--json")
    assert json.loads(out)["session"] == S2


def test_diff(capsys, ledger):
    code, out, _ = run(capsys, "diff", "1", "2")
    assert code == 0 and out.startswith("#1 -> #2: ")
    assert "+  user prompts" in out
    code, out, _ = run(capsys, "diff", "1", "last", "--json")
    data = json.loads(out)
    assert data["a"] == 1 and data["b"] == 2 and len(data["diff"]["added"]) == 2


def test_cache(capsys, ledger):
    code, out, _ = run(capsys, "cache", "--session", S1)
    assert code == 0 and out.startswith(f"session {S1}: 2 requests, hit rate ")
    assert "verdicts: " in out
    code, out, _ = run(capsys, "cache", "--session", S1, "--json")
    data = json.loads(out)
    assert [e["cache_read"] for e in data["events"]] == [0, 900]
    assert data["summary"]["requests"] == 2


def test_budget(capsys, ledger):
    code, out, _ = run(capsys, "budget")
    assert code == 0 and out.startswith("no budgets configured")
    (ledger / "config.toml").write_text(
        '[[budget]]\nname = "daily"\nusd = 1\n\n'
        '[[budget]]\nname = "sess"\nusd = 0.5\nper = "session"\nwindow = "total"\n',
        encoding="utf-8",
    )
    code, out, _ = run(capsys, "budget")
    assert code == 0
    assert any(ln.startswith("daily") and "$0.0220 / $1.00" in ln for ln in out.splitlines())
    code, out, _ = run(capsys, "budget", "--session", S1, "--json")
    data = {d["rule"]["name"]: d for d in json.loads(out)}
    assert data["sess"]["spend"]["usd"] == pytest.approx(0.012)
    assert data["daily"]["remaining"]["usd"] == pytest.approx(1 - 0.022)


def test_config_error_is_clean(capsys, home):
    (home / "config.toml").write_text("[[budget]]\nname = 'x'\nusdd = 1\n", encoding="utf-8")
    code, out, err = run(capsys, "budget")
    assert code == 1 and err.startswith("skinflint: error: budget[0]: unknown key")
    assert "Traceback" not in err
    code, _, err = run(capsys, "report", "--config", str(home / "missing.toml"))
    assert code == 1 and "config file not found" in err


def test_prices(capsys, home):
    code, out, _ = run(capsys, "prices")
    assert code == 0 and out.startswith("USD per million tokens, as of ")
    code, out, _ = run(capsys, "prices", "--provider", "openai", "--json")
    data = json.loads(out)
    assert data["as_of"] and all(m["provider"] == "openai" for m in data["models"])
    code, out, _ = run(capsys, "prices", "claude-sonnet-5-5")
    assert out.startswith("claude-sonnet-5-5 -> anthropic claude-sonnet-5-5 (exact)")
    code, out, _ = run(capsys, "prices", "claude-sonnet-5-5-20261001", "--json")
    data = json.loads(out)
    assert data["model"] == "claude-sonnet-5-5" and data["match"].startswith("normalised")
    code, out, _ = run(capsys, "prices", "claude-sonnet-5-5-preview", "--json")
    assert "prefix" in json.loads(out)["match"]
    code, out, _ = run(capsys, "prices", "totally-unknown", "--json")
    assert json.loads(out)["match"].startswith("unknown model")
    (home / "config.toml").write_text('[limits]\nunknown_model = "block"\n', encoding="utf-8")
    code, _, err = run(capsys, "prices", "totally-unknown")
    assert code == 1 and "no price" in err


def test_init(capsys, home):
    code, out, _ = run(capsys, "init")
    path = home / "config.toml"
    assert code == 0 and out.strip() == str(path)
    assert path.read_text(encoding="utf-8").startswith("# skinflint configuration")
    code, _, err = run(capsys, "init")
    assert code == 1 and "already exists" in err
    path.write_text("# mine\n", encoding="utf-8")
    assert run(capsys, "init", "--force")[0] == 0
    assert "[[budget]]" in path.read_text(encoding="utf-8")


def test_prune(capsys, ledger):
    code, out, _ = run(capsys, "prune", "--records", "5")
    assert code == 0 and out.startswith("removed 0 profiles (older than 30d), 1 records")
    code, out, _ = run(capsys, "prune", "--profiles", "0")
    assert "kept forever" in out
    code, out, _ = run(capsys, "report", "--since", "all", "--json")
    assert json.loads(out)["total"]["requests"] == 3


def test_env(capsys, home):
    code, out, _ = run(capsys, "env", "--shell", "sh", "--scope", "proj")
    assert out.splitlines() == [
        "export ANTHROPIC_BASE_URL=http://127.0.0.1:4100/s/proj",
        "export OPENAI_BASE_URL=http://127.0.0.1:4100/s/proj/v1",
    ]
    _, out, _ = run(capsys, "env", "--shell", "powershell")
    assert out.splitlines()[0] == '$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4100"'
    _, out, _ = run(capsys, "env", "--shell", "cmd")
    assert out.splitlines()[1] == "set OPENAI_BASE_URL=http://127.0.0.1:4100/v1"
    _, out, _ = run(capsys, "env", "--shell", "fish")
    assert out.splitlines()[0] == "set -gx ANTHROPIC_BASE_URL http://127.0.0.1:4100"
    code, _, err = run(capsys, "env", "--scope", "bad scope!")
    assert code == 1 and "bad scope" in err
    (home / "config.toml").write_text('[server]\nhost = "0.0.0.0"\nport = 5000\n', encoding="utf-8")
    _, out, _ = run(capsys, "env", "--shell", "sh")
    assert "http://127.0.0.1:5000" in out


def test_detect_shell(monkeypatch):
    assert cli.detect_shell({"SHELL": "/usr/bin/fish"}) == "fish"
    if sys.platform == "win32":
        assert cli.detect_shell({"SHELL": "/usr/bin/bash"}) == "sh"
        system = (
            r"C:\Program Files\WindowsPowerShell\Modules;"
            r"C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules"
        )
        assert cli.detect_shell({"PSModulePath": system}) == "cmd"
        user = r"C:\Users\u\Documents\WindowsPowerShell\Modules;" + system
        assert cli.detect_shell({"PSModulePath": user}) == "powershell"
    else:
        assert cli.detect_shell({"SHELL": "/bin/zsh"}) == "sh"


def test_serve_port_in_use(capsys, home):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        code, _, err = run(capsys, "serve", "--port", str(port), "-q")
    assert code == 1
    assert f"port {port} on 127.0.0.1 is already in use" in err
    assert "Traceback" not in err


# -- run -------------------------------------------------------------------------------------

CHILD = r"""
import json, os, sys, urllib.error, urllib.request
base = os.environ["ANTHROPIC_BASE_URL"]
assert os.environ["OPENAI_BASE_URL"] == base + "/v1", os.environ["OPENAI_BASE_URL"]
req = urllib.request.Request(
    base + "/v1/messages",
    data=json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 10,
                     "messages": [{"role": "user", "content": "hi"}]}).encode(),
    headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
             "x-api-key": "test"},
)
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        json.loads(resp.read())
    sys.exit(3)
except urllib.error.HTTPError as e:
    sys.exit(4 if e.code == 402 and e.headers.get("x-should-retry") == "false" else 5)
"""


@pytest.fixture
async def upstream(aiohttp_server):
    calls = []

    async def messages(request: web.Request) -> web.Response:
        calls.append(request.path)
        await request.read()
        return web.json_response(
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    server = await aiohttp_server(app)
    server.calls = calls
    return server


def write_upstream_config(home, server) -> None:
    (home / "config.toml").write_text(
        f'[upstream]\nanthropic = "http://127.0.0.1:{server.port}"\n', encoding="utf-8"
    )


async def test_run_child_through_proxy(home, upstream, capsys):
    write_upstream_config(home, upstream)
    argv = ["run", "--scope", "job-1", "--", sys.executable, "-c", CHILD]
    code = await asyncio.to_thread(cli.main, argv)
    err = capsys.readouterr().err
    assert code == 3, err
    assert upstream.calls == ["/v1/messages"]
    assert "skinflint: job-1  1 request  prompt 1k" in err
    assert "$0.0030" in err
    with Store.open_readonly(home / "skinflint.db") as store:
        (rec,) = store.records()
    assert rec.scope == "job-1" and rec.state == State.OK


async def test_run_cap_blocks(home, upstream, capsys):
    write_upstream_config(home, upstream)
    argv = ["run", "--cap", "0", "--", sys.executable, "-c", CHILD]
    code = await asyncio.to_thread(cli.main, argv)
    err = capsys.readouterr().err
    assert code == 4, err
    assert upstream.calls == []
    assert "0 requests" in err and "$0 of $0" in err and "1 blocked" in err
    assert "skinflint: run-" in err
    with Store.open_readonly(home / "skinflint.db") as store:
        (rec,) = store.records()
    assert rec.error.endswith("Raise --cap to allow more.")


def test_child_env():
    base = "http://127.0.0.1:5000/s/job"
    env = cli.child_env({"PYTHONHOME": sys.base_prefix, "KEEP": "1"}, base)
    assert env == {
        "KEEP": "1",
        "ANTHROPIC_BASE_URL": base,
        "OPENAI_BASE_URL": base + "/v1",
        "ENABLE_TOOL_SEARCH": "true",
    }
    other = cli.child_env({"PYTHONHOME": "/opt/python"}, base)
    assert other["PYTHONHOME"] == "/opt/python"


@pytest.mark.parametrize("value", ["false", "auto", ""])
def test_child_env_keeps_tool_search_setting(value):
    env = cli.child_env({"ENABLE_TOOL_SEARCH": value}, "http://127.0.0.1:5000")
    assert env["ENABLE_TOOL_SEARCH"] == value


def test_run_usage(capsys, home):
    code, _, err = run(capsys, "run")
    assert code == 2 and "give a command" in err
    code, _, err = run(capsys, "run", "--", "definitely-not-a-command-xyz")
    assert code == 127 and "command not found" in err
