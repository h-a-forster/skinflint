import tomllib
from importlib import resources

import pytest

from skinflint.model import Provider, Usage
from skinflint.pricing import Pricing

ANT = Provider.ANTHROPIC
OAI = Provider.OPENAI


@pytest.fixture(scope="module")
def pricing() -> Pricing:
    return Pricing.load()


@pytest.mark.parametrize(
    ("provider", "model", "usage", "expected"),
    [
        # Real Claude Code request on Haiku 4.5 ($1 in, $2 1h write, $5 out):
        # 10*1 + 57808*2 + 42*5 = 10 + 115616 + 210 = 115836 -> $0.115836
        (
            ANT,
            "claude-haiku-4-5-20251001",
            Usage(input_tokens=10, cache_write_1h=57808, output_tokens=42),
            0.115836,
        ),
        # Opus 5.5: 1000*4 + 2000*5 + 3000*8 + 10000*0.20 + 500*20 = 50000 -> $0.05
        (
            ANT,
            "claude-opus-5-5",
            Usage(1000, 2000, 3000, 10000, 500),
            0.05,
        ),
        # Sonnet 5.5 read at $0.10: 1_000_000 * 0.10 = $0.10
        (ANT, "claude-sonnet-5-5", Usage(cache_read=1_000_000), 0.10),
        # Fable 5.1 read 0.025x of $10 = $0.25
        (ANT, "claude-fable-5-1", Usage(cache_read=1_000_000), 0.25),
        # Opus 4.1: 100k in at $15 + 10k out at $75 = 1.5 + 0.75
        (ANT, "claude-opus-4-1-20250805", Usage(input_tokens=100_000, output_tokens=10_000), 2.25),
        # Web search: 3 searches at $10/1k = $0.03, plus 1000 in at $3 = $0.003
        (ANT, "claude-sonnet-4-6", Usage(input_tokens=1000, web_search_requests=3), 0.033),
        # Opus 5.5 fast: 1000 in at $8, 1000 out at $40, 1000 read at 0.05*8 = 0.4
        (
            ANT,
            "claude-opus-5-5",
            Usage(input_tokens=1000, cache_read=1000, output_tokens=1000, speed="fast"),
            (8000 + 400 + 40000) / 1e6,
        ),
        # Opus 4.8 fast $10/$50
        (ANT, "claude-opus-4-8", Usage(input_tokens=1000, output_tokens=1000, speed="fast"), 0.06),
        # Opus 4.6 has no fast tier: standard $5/$25
        (ANT, "claude-opus-4-6", Usage(input_tokens=1000, output_tokens=1000, speed="fast"), 0.03),
        # inference_geo us x1.1 on everything but web search: (1000*5 + 1000*25)*1.1 + 10/1000
        (
            ANT,
            "claude-opus-4-7",
            Usage(1000, output_tokens=1000, inference_geo="us", web_search_requests=1),
            0.033 + 0.01,
        ),
        (ANT, "claude-opus-4-7", Usage(1000, inference_geo="global"), 0.005),
        # OpenAI gpt-5.4: 10k uncached at 2.5, 90k cached at 0.25, 1k out at 15
        # 25000 + 22500 + 15000 = 62500 -> $0.0625
        (OAI, "gpt-5.4", Usage(input_tokens=10_000, cache_read=90_000, output_tokens=1000), 0.0625),
        # gpt-6-astra: 1k uncached at 10, 2k written at 12.5, 3k cached at 1, 100 out at 50
        # 10000 + 25000 + 3000 + 5000 = 43000 -> $0.043
        (
            OAI,
            "gpt-6-astra",
            Usage(input_tokens=1000, cache_write_5m=2000, cache_read=3000, output_tokens=100),
            0.043,
        ),
        # gpt-5.4 has no write premium: writes at the input rate
        (OAI, "gpt-5.4", Usage(cache_write_5m=100_000), 0.25),
        # gpt-5-pro has no cached price: reads at the input rate
        (OAI, "gpt-5-pro", Usage(cache_read=100_000), 1.5),
        # Service tiers: flex 0.5x, priority 2x, gpt-5.5 fast 2.5x
        (OAI, "gpt-5.4", Usage(input_tokens=100_000, service_tier="flex"), 0.125),
        (OAI, "gpt-5.4", Usage(input_tokens=100_000, service_tier="priority"), 0.5),
        (OAI, "gpt-5.4", Usage(input_tokens=100_000, service_tier="default"), 0.25),
        (OAI, "gpt-5.5", Usage(input_tokens=100_000, service_tier="fast"), 1.25),
        (ANT, "claude-haiku-4-5", Usage(input_tokens=1_000_000, service_tier="batch"), 0.5),
    ],
)
def test_cost_table(pricing, provider, model, usage, expected):
    usd, estimated = pricing.cost(provider, model, usage)
    assert usd == pytest.approx(expected)
    assert estimated is False


@pytest.mark.parametrize(
    ("provider", "model", "threshold", "short", "long"),
    [
        # Haiku 5.5: all at $0.10 vs all at $0.50 input
        (ANT, "claude-haiku-5-5", 100_000, 0.10, 0.50),
        (OAI, "gpt-5.4", 272_000, 2.5, 5.0),
        (OAI, "gpt-6-astra", 272_000, 10.0, 20.0),
        (OAI, "gpt-5.5-pro", 272_000, 30.0, 60.0),
    ],
)
def test_long_context_boundary(pricing, provider, model, threshold, short, long):
    at = pricing.cost(provider, model, Usage(input_tokens=threshold))[0]
    above = pricing.cost(provider, model, Usage(input_tokens=threshold + 1))[0]
    assert at == pytest.approx(threshold * short / 1e6)
    assert above == pytest.approx((threshold + 1) * long / 1e6)


def test_long_context_counts_cached_tokens(pricing):
    # Haiku 5.5: 1 uncached + 100_000 read = 100_001 prompt -> whole request at long rates
    usage = Usage(input_tokens=1, cache_read=100_000, output_tokens=1000)
    usd, _ = pricing.cost(ANT, "claude-haiku-5-5", usage)
    assert usd == pytest.approx((1 * 0.5 + 100_000 * 0.05 + 1000 * 2.5) / 1e6)


def test_long_tier_cache_rates(pricing):
    price, _ = pricing.lookup(OAI, "gpt-5.5-pro")
    assert price.long.cache_read == 60.0  # no cached price -> input rate
    price, _ = pricing.lookup(OAI, "gpt-5.5")
    assert price.long.cache_write_5m == 10.0  # no write premium -> input rate
    price, _ = pricing.lookup(ANT, "claude-opus-5-5")
    assert price.fast.cache_read == pytest.approx(0.4)  # 0.05x of $8
    assert price.fast.cache_write_1h == pytest.approx(16.0)


def test_max_cost(pricing):
    # Anthropic: 1h write ($2) beats input ($1) on Haiku 4.5
    assert pricing.max_cost(ANT, "claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(7.0)
    # Haiku 5.5 above 100k: long 1h write $1 + long output $2.50
    assert pricing.max_cost(ANT, "claude-haiku-5-5", 200_000, 100_000) == pytest.approx(
        (200_000 * 1.0 + 100_000 * 2.5) / 1e6
    )
    assert pricing.max_cost(ANT, "claude-haiku-5-5", 100_000, 0) == pytest.approx(0.02)
    # OpenAI gpt-6-sol: write $2.50 > input $2
    assert pricing.max_cost(OAI, "gpt-6-sol", 100_000, 0) == pytest.approx(0.25)
    assert pricing.max_cost(OAI, "gpt-5.4", 300_000, 1000) == pytest.approx(
        (300_000 * 5 + 1000 * 22.5) / 1e6
    )


@pytest.mark.parametrize(
    ("provider", "model", "expected", "exact"),
    [
        (ANT, "claude-haiku-4-5", "claude-haiku-4-5", True),
        (ANT, "claude-haiku-4-5-20251001", "claude-haiku-4-5", True),
        (ANT, "claude-opus-4-20250514", "claude-opus-4-0", True),
        (ANT, "claude-sonnet-5-5-20261001", "claude-sonnet-5-5", True),
        (ANT, "anthropic/claude-sonnet-4-6", "claude-sonnet-4-6", True),
        (ANT, "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5", True),
        (ANT, "anthropic.claude-3-5-haiku-20241022-v1:0", "claude-3-5-haiku-20241022", True),
        (ANT, "claude-opus-4-5@20251101", "claude-opus-4-5", True),
        (ANT, "claude-opus-4-6[1m]", "claude-opus-4-6", True),
        (ANT, "Claude-Opus-5-5", "claude-opus-5-5", True),
        (OAI, "gpt-5.4-mini-2026-03-01", "gpt-5.4-mini", True),
        (OAI, "gpt-4o-2024-05-13", "gpt-4o-2024-05-13", True),
        (OAI, "gpt-4o-2024-08-06", "gpt-4o", True),
        (OAI, "openai/gpt-5.4", "gpt-5.4", True),
        (OAI, "gpt-5.4-mini-high", "gpt-5.4-mini", False),
        (OAI, "gpt-6-sol-preview", "gpt-6-sol", False),
        (OAI, "o3-deep-research", "o3", False),
    ],
)
def test_lookup(pricing, provider, model, expected, exact):
    price, got_exact = pricing.lookup(provider, model)
    want, _ = pricing.lookup(provider, expected)
    assert price is want
    assert got_exact is exact


def test_prefix_needs_dash_boundary():
    p = Pricing.load(unknown_model="block")
    assert p.lookup(OAI, "gpt-5.45") == (None, False)
    assert p.lookup(OAI, "gpt-5.4x-mini") == (None, False)
    # Longest prefix wins: gpt-5.4-mini, never gpt-5.4
    price, _ = p.lookup(OAI, "gpt-5.4-mini-experimental")
    assert price.input == 0.75


def test_unknown_max(pricing):
    # Unknown models price as the provider's `fallback` flagship, flagged as estimated
    price, exact = pricing.lookup(ANT, "claude-nonexistent")
    assert exact is False
    assert price is pricing.lookup(ANT, "claude-fable-5-1")[0]
    # 100k uncached at Fable 5.1's $10
    usd, estimated = pricing.cost(ANT, "claude-nonexistent", Usage(input_tokens=100_000))
    assert estimated is True
    assert usd == pytest.approx(1.0)
    price, exact = pricing.lookup(OAI, "mystery-model")
    assert exact is False
    assert price is pricing.lookup(OAI, "gpt-6-astra")[0]


def test_unknown_max_without_fallback_uses_priciest():
    p = Pricing.from_dict(
        {
            "openai": {
                "models": {"a": {"input": 1, "output": 2}, "b": {"input": 5, "output": 9}},
            }
        }
    )
    assert p.lookup(OAI, "zzz") == (p.lookup(OAI, "b")[0], False)


def test_fallback_exists_in_table():
    raw = _raw()
    for provider in Provider:
        fallback = raw[provider.value]["fallback"]
        assert fallback in raw[provider.value]["models"], fallback


def test_service_tier_override():
    p = Pricing.load(
        overrides={"gpt-5.4": {"input": 1, "output": 2, "service_tier": {"flex": 0.25}}}
    )
    usd, _ = p.cost(OAI, "gpt-5.4", Usage(input_tokens=1000, service_tier="flex"))
    assert usd == pytest.approx(1000 * 1 * 0.25 / 1e6)
    # Without one, an override keeps the table's per-model tiers (gpt-5.5 fast 2.5x)
    p = Pricing.load(overrides={"gpt-5.5": {"input": 1, "output": 2}})
    usd, _ = p.cost(OAI, "gpt-5.5", Usage(input_tokens=1000, service_tier="fast"))
    assert usd == pytest.approx(1000 * 1 * 2.5 / 1e6)


def test_unknown_block():
    p = Pricing.load(unknown_model="block")
    assert p.lookup(ANT, "claude-nonexistent") == (None, False)
    assert p.cost(ANT, "claude-nonexistent", Usage(input_tokens=5)) == (0.0, True)
    assert p.max_cost(ANT, "claude-nonexistent", 100, 100) == 0.0
    with pytest.raises(ValueError):
        Pricing.load(unknown_model="nope")


def test_prefix_match_is_estimated(pricing):
    _, estimated = pricing.cost(OAI, "gpt-5.4-mini-high", Usage(input_tokens=1))
    assert estimated is True


def test_overrides():
    p = Pricing.load(
        overrides={
            # Replace a known model: cache rates follow its own 0.05x read ratio
            "claude-opus-5-5": {"input": 10, "output": 50},
            # New Anthropic model, provider guessed from the name: provider defaults
            "claude-custom-1": {"input": 2, "output": 8},
            # New OpenAI model with tiers
            "my-model": {
                "provider": "openai",
                "input": 1,
                "output": 2,
                "cache_read": 0.1,
                "long_context": {"threshold": 1000, "input": 3},
            },
            # Override by alias replaces the canonical entry
            "claude-haiku-4-5-20251001": {"input": 2, "output": 10, "cache_write_1h": 3},
        }
    )
    opus, exact = p.lookup(ANT, "claude-opus-5-5")
    assert exact and opus.input == 10 and opus.cache_read == pytest.approx(0.5)
    assert opus.fast is None
    custom, exact = p.lookup(ANT, "claude-custom-1")
    assert exact
    assert (custom.cache_write_5m, custom.cache_write_1h, custom.cache_read) == pytest.approx(
        (2.5, 4.0, 0.2)
    )
    assert custom.web_search_per_1k == 10.0
    mine, exact = p.lookup(OAI, "my-model")
    assert exact and mine.cache_write_5m == 1 and mine.cache_write_1h == 1
    assert mine.long_context_threshold == 1000
    assert mine.long.input == 3 and mine.long.output == 2
    assert mine.long.cache_read == pytest.approx(0.3)
    haiku, _ = p.lookup(ANT, "claude-haiku-4-5")
    assert haiku.input == 2 and haiku.cache_write_1h == 3
    assert haiku.cache_write_5m == pytest.approx(2.5)
    assert p.lookup(ANT, "claude-haiku-4-5-20251001")[0] is haiku


def test_override_from_config_parse():
    from skinflint.config import parse

    cfg = parse({"prices": {"gpt-5.4": {"input": 1, "output": 2, "fast": {"input": 4}}}})
    p = Pricing.load(overrides=cfg.prices)
    price, _ = p.lookup(OAI, "gpt-5.4")
    assert price.input == 1 and price.cache_read == pytest.approx(0.1)
    assert price.long is None
    assert price.fast.input == 4 and price.fast.output == 2


# --- prices.toml self-consistency -------------------------------------------------------------


def _raw() -> dict:
    text = resources.files("skinflint.data").joinpath("prices.toml").read_text("utf-8")
    return tomllib.loads(text)


def test_meta():
    raw = _raw()
    assert raw["meta"]["as_of"] == "2026-10-09"
    assert all(v.startswith("https://") for k, v in raw["meta"].items() if k != "as_of")
    assert Pricing.load().as_of == "2026-10-09"


def test_every_entry_resolves(pricing):
    for provider, model_id, price in pricing.models():
        got, exact = pricing.lookup(provider, model_id)
        assert got is price and exact, model_id
    for provider in Provider:
        for alias, target in pricing.aliases(provider).items():
            assert pricing.lookup(provider, alias)[0] is pricing.lookup(provider, target)[0]


def test_no_negative_and_thresholds(pricing):
    fields = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
    for _, model_id, price in pricing.models():
        for tier in (price, price.long, price.fast):
            if tier is None:
                continue
            for f in fields:
                assert getattr(tier, f) >= 0, (model_id, f)
            assert tier.cache_read <= tier.input, model_id
        assert price.input > 0 and price.output > 0, model_id
        assert (price.long is None) == (price.long_context_threshold is None), model_id
        if price.long_context_threshold is not None:
            assert price.long_context_threshold > 0
            assert price.long.input >= price.input, model_id


def test_aliases_unique():
    raw = _raw()
    seen: dict[str, str] = {}
    for provider in ("anthropic", "openai"):
        models = raw[provider]["models"]
        for model_id, entry in models.items():
            for alias in entry.get("aliases", []):
                assert alias not in seen, alias
                assert alias not in models, alias
                seen[alias] = model_id


def test_expected_models_present(pricing):
    ids = {m for _, m, _ in pricing.models()}
    for m in (
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-haiku-4-5",
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.4-mini",
        "o4-mini",
    ):
        assert m in ids
    assert "claude-3-haiku-20240307" not in ids
    assert "gpt-5.1-codex" not in ids
