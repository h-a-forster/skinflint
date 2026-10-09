import tomllib
from pathlib import Path

import pytest

from skinflint import config
from skinflint.config import ConfigError, parse
from skinflint.model import Action, Per, Provider, Window


def test_defaults():
    cfg = parse({})
    assert cfg.host == "127.0.0.1"
    assert cfg.port == config.DEFAULT_PORT
    assert cfg.upstreams[Provider.ANTHROPIC] == "https://api.anthropic.com"
    assert cfg.budgets == []
    assert cfg.reserve == "estimate"
    assert cfg.is_loopback


def test_default_config_text_parses():
    cfg = parse(tomllib.loads(config.DEFAULT_CONFIG))
    assert [r.name for r in cfg.budgets] == ["daily", "session"]
    assert cfg.budgets[1].per is Per.SESSION
    assert cfg.budgets[1].window is Window.TOTAL


def test_budget_rule_fields():
    cfg = parse(
        {
            "budget": [
                {
                    "name": "opus",
                    "usd": 5,
                    "tokens": 1000,
                    "requests": 10,
                    "window": "hour",
                    "per": "scope",
                    "scope": "ci-*",
                    "model": "claude-opus-*",
                    "action": "warn",
                }
            ]
        }
    )
    rule = cfg.budgets[0]
    assert rule.usd == 5.0 and isinstance(rule.usd, float)
    assert (rule.tokens, rule.requests) == (1000, 10)
    assert (rule.window, rule.per, rule.action) == (Window.HOUR, Per.SCOPE, Action.WARN)
    assert (rule.scope, rule.model) == ("ci-*", "claude-opus-*")


@pytest.mark.parametrize(
    "rule, message",
    [
        ({"name": "x", "ussd": 1}, r"unknown key\(s\) 'ussd'"),
        ({"usd": 1}, "name is required"),
        ({"name": "x"}, "set at least one of"),
        ({"name": "x", "usd": -1}, "non-negative"),
        ({"name": "x", "usd": True}, "non-negative"),
        ({"name": "x", "usd": float("inf")}, "non-negative"),
        ({"name": "x", "usd": 1, "window": "fortnight"}, "expected one of"),
        ({"name": "x", "usd": 1, "per": "user"}, "expected one of"),
        ({"name": "x", "tokens": 1.5}, "expected an integer"),
        ({"name": "x", "requests": 5, "per": "request"}, "supports usd only"),
    ],
)
def test_invalid_budget_rules(rule, message):
    with pytest.raises(ConfigError, match=message):
        parse({"budget": [rule]})


def test_duplicate_budget_names():
    with pytest.raises(ConfigError, match="duplicate"):
        parse({"budget": [{"name": "a", "usd": 1}, {"name": "a", "usd": 2}]})


def test_budget_must_be_array_of_tables():
    with pytest.raises(ConfigError, match=r"\[\[budget\]\]"):
        parse({"budget": {"name": "a", "usd": 1}})


@pytest.mark.parametrize(
    "data, message",
    [
        ({"servr": {}}, "unknown key"),
        ({"server": {"port": 70000}}, "out of range"),
        ({"server": {"port": "80"}}, "expected an integer"),
        ({"upstream": {"anthropic": "ftp://x"}}, "http"),
        ({"upstream": {"gemini": "https://x"}}, "unknown key"),
        ({"limits": {"reserve": "always"}}, "expected one of"),
        ({"storage": {"store_bodies": "yes"}}, "true or false"),
        ({"prices": {"m": {"input": 1}}}, "required"),
        ({"prices": {"m": {"input": 1, "output": 2, "provider": "google"}}}, "anthropic or openai"),
        ({"prices": {"m": {"input": 1, "output": 2, "long_context": {"input": 2}}}}, "threshold"),
        ({"prices": {"m": {"input": 1, "output": 2, "fast": {"threshold": 2}}}}, "unknown key"),
    ],
)
def test_invalid_sections(data, message):
    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_upstream_trailing_slash_removed():
    cfg = parse({"upstream": {"openai": "https://chatgpt.com/backend-api/codex/"}})
    assert cfg.upstreams[Provider.OPENAI] == "https://chatgpt.com/backend-api/codex"


def test_relative_db_path_resolves_against_config_dir(tmp_path):
    cfg = parse({"storage": {"path": "ledger.db"}}, base_dir=tmp_path)
    assert cfg.db_path == tmp_path / "ledger.db"


def test_load_missing_default_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SKINFLINT_HOME", str(tmp_path))
    monkeypatch.delenv("SKINFLINT_CONFIG", raising=False)
    cfg = config.load()
    assert cfg.source is None
    assert cfg.db_path == tmp_path / "skinflint.db"


def test_load_missing_explicit_file_fails(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        config.load(tmp_path / "nope.toml")


def test_load_reports_toml_errors_with_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server\nport = 1", encoding="utf-8")
    with pytest.raises(ConfigError, match="config.toml"):
        config.load(path)


def test_load_sets_source(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[budget]]\nname = "d"\nusd = 1\n', encoding="utf-8")
    cfg = config.load(path)
    assert cfg.source == path
    assert cfg.budgets[0].name == "d"


def test_skinflint_config_env(tmp_path, monkeypatch):
    path = tmp_path / "elsewhere.toml"
    monkeypatch.setenv("SKINFLINT_CONFIG", str(path))
    assert config.default_config_path() == Path(path)


@pytest.mark.parametrize(
    "host, loopback",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("myhost", False),
    ],
)
def test_is_loopback(host, loopback):
    assert parse({"server": {"host": host}}).is_loopback is loopback
