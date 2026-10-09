"""Configuration: a TOML file, validated strictly.

A typo in a budget rule must not silently disable a cap, so unknown keys are errors.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skinflint.model import Action, BudgetRule, Per, Provider, Window

DEFAULT_PORT = 4100

DEFAULT_UPSTREAMS = {
    Provider.ANTHROPIC: "https://api.anthropic.com",
    Provider.OPENAI: "https://api.openai.com",
}

PRICE_KEYS = {
    "provider",
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "web_search_per_1k",
    "long_context",
    "fast",
}
RATE_KEYS = {"input", "output", "cache_write_5m", "cache_write_1h", "cache_read"}

DEFAULT_CONFIG = """\
# skinflint configuration. Every section is optional.
# Reference: https://github.com/h-a-forster/skinflint/blob/main/docs/configuration.md

[server]
host = "127.0.0.1"
port = 4100

[limits]
# How much to hold against budgets while a request is in flight:
#   "estimate"   - estimated prompt cost only (default; may overshoot by in-flight output)
#   "worst_case" - prompt plus max output tokens at full price (never overshoots)
reserve = "estimate"

# A shared daily cap across everything that goes through the proxy.
[[budget]]
name = "daily"
usd = 20.00
window = "day"

# A cap for each agent session (Claude Code sends a session id).
[[budget]]
name = "session"
usd = 5.00
per = "session"
window = "total"
"""


class ConfigError(ValueError):
    pass


def home() -> Path:
    """Directory for the config file and ledger: $SKINFLINT_HOME or ~/.skinflint."""
    env = os.environ.get("SKINFLINT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".skinflint"


def default_config_path() -> Path:
    env = os.environ.get("SKINFLINT_CONFIG")
    return Path(env).expanduser() if env else home() / "config.toml"


@dataclass(slots=True)
class Config:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    upstreams: dict[Provider, str] = field(default_factory=lambda: dict(DEFAULT_UPSTREAMS))
    db_path: Path = field(default_factory=lambda: home() / "skinflint.db")
    keep_profiles_days: int = 30  # 0 keeps profiles forever
    store_bodies: bool = False
    reserve: str = "estimate"  # estimate | worst_case
    unknown_model: str = "max"  # max | block
    unmetered: str = "allow"  # allow | block
    inject_stream_usage: bool = True  # OpenAI chat streams: ask for usage, hide the extra chunk
    cache_diagnostics: bool = True  # Anthropic: add diagnostics.previous_message_id to requests
    budgets: list[BudgetRule] = field(default_factory=list)
    prices: dict[str, dict[str, Any]] = field(default_factory=dict)  # model id -> price fields
    source: Path | None = None  # file this config was loaded from

    @property
    def is_loopback(self) -> bool:
        import ipaddress

        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


def load(path: Path | None = None) -> Config:
    """Load the config file. A missing file at the default location yields defaults."""
    explicit = path is not None
    path = path or default_config_path()
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return Config()
    import tomllib  # deferred: keeps `skinflint statusline` start-up fast

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    cfg = parse(data, base_dir=path.parent)
    cfg.source = path
    return cfg


def parse(data: dict[str, Any], base_dir: Path | None = None) -> Config:
    cfg = Config()
    _check_keys(
        data,
        {"server", "upstream", "storage", "limits", "anthropic", "openai", "budget", "prices"},
        "",
    )

    server = _table(data, "server")
    _check_keys(server, {"host", "port"}, "server")
    cfg.host = _str(server, "host", cfg.host, "server")
    cfg.port = _int(server, "port", cfg.port, "server", lo=0, hi=65535)

    upstream = _table(data, "upstream")
    _check_keys(upstream, {p.value for p in Provider}, "upstream")
    for provider in Provider:
        url = _str(upstream, provider.value, cfg.upstreams[provider], "upstream").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"upstream.{provider.value}: expected an http(s) URL, got {url!r}")
        cfg.upstreams[provider] = url

    storage = _table(data, "storage")
    _check_keys(storage, {"path", "keep_profiles_days", "store_bodies"}, "storage")
    if "path" in storage:
        p = Path(_str(storage, "path", "", "storage")).expanduser()
        cfg.db_path = p if p.is_absolute() or base_dir is None else base_dir / p
    cfg.keep_profiles_days = _int(
        storage, "keep_profiles_days", cfg.keep_profiles_days, "storage", lo=0
    )
    cfg.store_bodies = _bool(storage, "store_bodies", cfg.store_bodies, "storage")

    limits = _table(data, "limits")
    _check_keys(limits, {"reserve", "unknown_model", "unmetered"}, "limits")
    cfg.reserve = _choice(limits, "reserve", cfg.reserve, {"estimate", "worst_case"}, "limits")
    cfg.unknown_model = _choice(
        limits, "unknown_model", cfg.unknown_model, {"max", "block"}, "limits"
    )
    cfg.unmetered = _choice(limits, "unmetered", cfg.unmetered, {"allow", "block"}, "limits")

    anthropic = _table(data, "anthropic")
    _check_keys(anthropic, {"cache_diagnostics"}, "anthropic")
    cfg.cache_diagnostics = _bool(
        anthropic, "cache_diagnostics", cfg.cache_diagnostics, "anthropic"
    )

    openai = _table(data, "openai")
    _check_keys(openai, {"inject_stream_usage"}, "openai")
    cfg.inject_stream_usage = _bool(
        openai, "inject_stream_usage", cfg.inject_stream_usage, "openai"
    )

    rules = data.get("budget", [])
    if not isinstance(rules, list):
        raise ConfigError("budget: use [[budget]] tables (an array of tables)")
    names: set[str] = set()
    for i, raw in enumerate(rules):
        rule = parse_rule(raw, f"budget[{i}]")
        if rule.name in names:
            raise ConfigError(f"budget[{i}]: duplicate budget name {rule.name!r}")
        names.add(rule.name)
        cfg.budgets.append(rule)

    prices = _table(data, "prices")
    for model, entry in prices.items():
        where = f"prices.{model!r}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: expected a table")
        _check_keys(entry, PRICE_KEYS, where)
        for key, value in entry.items():
            if key == "provider":
                if value not in {p.value for p in Provider}:
                    raise ConfigError(f"{where}.provider: expected anthropic or openai")
            elif key in ("long_context", "fast"):
                allowed = RATE_KEYS | ({"threshold"} if key == "long_context" else set())
                if not isinstance(value, dict):
                    raise ConfigError(f"{where}.{key}: expected a table")
                _check_keys(value, allowed, f"{where}.{key}")
                if key == "long_context" and "threshold" not in value:
                    raise ConfigError(f"{where}.long_context: threshold is required")
                for k, v in value.items():
                    if not _is_number(v) or v < 0:
                        raise ConfigError(f"{where}.{key}.{k}: expected a non-negative number")
            elif not _is_number(value) or value < 0:
                raise ConfigError(f"{where}.{key}: expected a non-negative number")
        if "input" not in entry or "output" not in entry:
            raise ConfigError(f"{where}: input and output prices are required")
        cfg.prices[model] = dict(entry)
    return cfg


def parse_rule(raw: Any, where: str) -> BudgetRule:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    _check_keys(
        raw,
        {"name", "usd", "tokens", "requests", "window", "per", "scope", "model", "action"},
        where,
    )
    name = _str(raw, "name", "", where)
    if not name:
        raise ConfigError(f"{where}: name is required")
    usd = raw.get("usd")
    if usd is not None and (not _is_number(usd) or usd < 0 or not math.isfinite(usd)):
        raise ConfigError(f"{where}.usd: expected a non-negative number")
    tokens = _int(raw, "tokens", None, where, lo=0)
    requests = _int(raw, "requests", None, where, lo=0)
    if usd is None and tokens is None and requests is None:
        raise ConfigError(f"{where} ({name}): set at least one of usd, tokens, requests")
    per = Per(_choice(raw, "per", Per.ALL.value, {p.value for p in Per}, where))
    window = Window(_choice(raw, "window", Window.DAY.value, {w.value for w in Window}, where))
    if per is Per.REQUEST and (tokens is not None or requests is not None):
        raise ConfigError(f'{where} ({name}): per = "request" supports usd only')
    return BudgetRule(
        name=name,
        usd=float(usd) if usd is not None else None,
        tokens=tokens,
        requests=requests,
        window=window,
        per=per,
        scope=_str(raw, "scope", "*", where) or "*",
        model=_str(raw, "model", "*", where) or "*",
        action=Action(_choice(raw, "action", Action.BLOCK.value, {a.value for a in Action}, where)),
    )


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        prefix = f"{where}: " if where else ""
        raise ConfigError(
            f"{prefix}unknown key(s) {', '.join(map(repr, unknown))}; "
            f"expected one of {', '.join(sorted(allowed))}"
        )


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: expected a table")
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _str(table: dict[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key}: expected a string")
    return value


def _int(table, key, default, where, lo=None, hi=None):
    value = table.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key}: expected an integer")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(f"{where}.{key}: {value} is out of range")
    return value


def _bool(table: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: expected true or false")
    return value


def _choice(table: dict[str, Any], key: str, default: str, allowed: set[str], where: str) -> str:
    value = _str(table, key, default, where)
    if value not in allowed:
        raise ConfigError(
            f"{where}.{key}: expected one of {', '.join(sorted(allowed))}, got {value!r}"
        )
    return value
