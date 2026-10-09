import io
import json
import time

import pytest

from skinflint import statusline
from skinflint.model import Endpoint, Provider, Record, State, Usage
from skinflint.store import Store

NOW = time.time()
SESSION = "c0ffee00-1111-4222-8333-444455556666"
CLAUDE_CODE_INPUT = {
    "hook_event_name": "Status",
    "session_id": SESSION,
    "transcript_path": "/home/u/.claude/projects/x/c0ffee00.jsonl",
    "cwd": "/home/u/proj",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "workspace": {"current_dir": "/home/u/proj", "project_dir": "/home/u/proj"},
    "version": "2.1.287",
    "output_style": {"name": "default"},
    "cost": {"total_cost_usd": 0.4213, "total_duration_ms": 45000, "total_api_duration_ms": 2300},
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SKINFLINT_HOME", str(tmp_path))
    monkeypatch.delenv("SKINFLINT_CONFIG", raising=False)
    return tmp_path


def add(store: Store, **kw) -> None:
    base = dict(
        ts=NOW - 60,
        provider=Provider.ANTHROPIC,
        endpoint=Endpoint.MESSAGES,
        scope="default",
        model="claude-opus-5-5",
        state=State.OK,
        session=SESSION,
    )
    base.update(kw)
    store.insert(Record(**base))


@pytest.fixture
def ledger(home):
    store = Store(home / "skinflint.db")
    add(store, usage=Usage(input_tokens=100, cache_read=900), cost_usd=0.42)
    add(store, session="other", usage=Usage(input_tokens=1000), cost_usd=2.68)
    add(store, ts=NOW - 3 * 86400, session=None, cost_usd=100.0)
    yield store
    store.close()


def render(payload) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return statusline.render(text, now=NOW)


def test_session_today_and_cache(ledger):
    assert render(CLAUDE_CODE_INPUT) == "skinflint $0.42 session · $3.10 today · cache 90%"


def test_missing_db(home):
    assert render(CLAUDE_CODE_INPUT) == statusline.NO_DATA


@pytest.mark.parametrize("payload", ["", "{not json", "[1, 2]", "null", '{"session_id": 7}'])
def test_bad_input_still_shows_today(ledger, payload):
    assert render(payload) == "skinflint $3.10 today · cache 45%"


def test_unknown_session_omits_session_part(ledger):
    assert render({"session_id": "nope"}).startswith("skinflint $3.10 today")


def test_nothing_today(home):
    store = Store(home / "skinflint.db")
    add(store, ts=NOW - 5 * 86400, session=None, cost_usd=1.0)
    store.close()
    assert render({}) == statusline.NO_DATA


def test_ratelimits(ledger):
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.47",
        "anthropic-ratelimit-unified-5h-reset": str(int(NOW + 3600)),
        "anthropic-ratelimit-unified-7d-utilization": "0.55",
        "anthropic-ratelimit-unified-7d-reset": str(int(NOW - 10)),  # already reset
    }
    ledger.save_ratelimit(Provider.ANTHROPIC, headers, NOW - 30)
    assert render(CLAUDE_CODE_INPUT).endswith("cache 90% · 5h 47%")
    headers["anthropic-ratelimit-unified-7d-reset"] = str(int(NOW + 86400))
    headers["anthropic-ratelimit-unified-5h-utilization"] = "garbage"
    ledger.save_ratelimit(Provider.ANTHROPIC, headers, NOW - 20)
    assert render(CLAUDE_CODE_INPUT).endswith("cache 90% · 7d 55%")


def test_budgets_show_limits(ledger, home):
    (home / "config.toml").write_text(
        '[[budget]]\nname = "daily"\nusd = 20\n\n'
        '[[budget]]\nname = "tight"\nusd = 5\n\n'
        '[[budget]]\nname = "session"\nusd = 5\nper = "session"\nwindow = "total"\n',
        encoding="utf-8",
    )
    assert render(CLAUDE_CODE_INPUT) == "skinflint $0.42/$5 session · $3.10/$5 today · cache 90%"


def test_bad_config_is_ignored(ledger, home):
    (home / "config.toml").write_text("[[budget]]\nnmae = 1\n", encoding="utf-8")
    assert render(CLAUDE_CODE_INPUT).startswith("skinflint $0.42 session")


def test_main_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(statusline, "render", boom)
    out = io.StringIO()
    assert statusline.main(io.StringIO("{}"), out) == 0
    assert out.getvalue() == statusline.NO_DATA + "\n"


def test_main_writes_utf8(ledger):
    class Out(io.StringIO):
        buffer = io.BytesIO()

    out = Out()
    statusline.main(io.StringIO(json.dumps(CLAUDE_CODE_INPUT)), out)
    assert Out.buffer.getvalue().decode("utf-8").startswith("skinflint $0.42 session · ")


def test_cli_entry(ledger, monkeypatch, capsysbinary):
    from skinflint.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(CLAUDE_CODE_INPUT)))
    assert main(["statusline"]) == 0
    assert capsysbinary.readouterr().out.decode().startswith("skinflint $0.42 session")
