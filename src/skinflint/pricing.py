"""Model prices: the bundled table, config overrides, model-id matching and cost maths."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from skinflint.model import Price, Provider, Usage

CACHE_KEYS = ("cache_write_5m", "cache_write_1h", "cache_read")

_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}(?:-[a-z]+)?\.)?anthropic\.")
_BEDROCK_VERSION = re.compile(r"-v\d+(?::\d+)?$")
_BRACKET_SUFFIX = re.compile(r"\[[^\]]*\]$")


@dataclass(slots=True)
class _ProviderData:
    ratios: dict[str, float]
    web_search_per_1k: float = 0.0
    service_tier: dict[str, float] = field(default_factory=dict)
    inference_geo: dict[str, float] = field(default_factory=dict)
    models: dict[str, Price] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    model_tiers: dict[str, dict[str, float]] = field(default_factory=dict)
    fallback: str | None = None


class Pricing:
    """Price lookups and cost calculation for every known model."""

    def __init__(self, providers: dict[Provider, _ProviderData], unknown_model: str = "max"):
        if unknown_model not in ("max", "block"):
            raise ValueError(f"unknown_model must be 'max' or 'block', got {unknown_model!r}")
        self._p = providers
        self.unknown_model = unknown_model
        self.as_of: str | None = None

    @classmethod
    def load(cls, overrides: dict[str, dict] | None = None, unknown_model: str = "max") -> Pricing:
        """Bundled prices.toml plus config overrides (model id -> price fields)."""
        text = resources.files("skinflint.data").joinpath("prices.toml").read_text("utf-8")
        return cls.from_dict(tomllib.loads(text), overrides, unknown_model)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        overrides: dict[str, dict] | None = None,
        unknown_model: str = "max",
    ) -> Pricing:
        providers: dict[Provider, _ProviderData] = {}
        for provider in Provider:
            section = data.get(provider.value, {})
            defaults = dict(section.get("defaults", {}))
            pd = _ProviderData(
                ratios={k: float(defaults.get(k, 1.0)) for k in CACHE_KEYS},
                web_search_per_1k=float(defaults.get("web_search_per_1k", 0.0)),
                service_tier=dict(section.get("service_tier", {})),
                inference_geo=dict(section.get("inference_geo", {})),
                fallback=section.get("fallback"),
            )
            for model_id, raw in section.get("models", {}).items():
                pd.models[model_id] = _build(provider, pd, raw)
                for alias in raw.get("aliases", []):
                    pd.aliases[alias] = model_id
                if "service_tier" in raw:
                    pd.model_tiers[model_id] = dict(raw["service_tier"])
            providers[provider] = pd
        pricing = cls(providers, unknown_model)
        pricing.as_of = data.get("meta", {}).get("as_of")
        for model_id, raw in (overrides or {}).items():
            pricing._override(model_id, raw)
        return pricing

    def _override(self, model_id: str, raw: dict[str, Any]) -> None:
        provider = Provider(raw["provider"]) if "provider" in raw else None
        target = None
        for p in [provider] if provider else list(Provider):
            pd = self._p[p]
            known = model_id if model_id in pd.models else pd.aliases.get(model_id)
            if known:
                provider, target = p, known
                break
        if provider is None:
            provider = Provider.ANTHROPIC if model_id.startswith("claude") else Provider.OPENAI
        pd = self._p[provider]
        base = pd.models.get(target) if target else None
        pd.models[target or model_id] = _build(provider, pd, raw, like=base)
        if "service_tier" in raw:
            pd.model_tiers[target or model_id] = dict(raw["service_tier"])

    def lookup(self, provider: Provider, model: str) -> tuple[Price | None, bool]:
        """(price, exact). Unknown models: the provider's fallback model, or None for 'block'."""
        _, price, exact = self._resolve(provider, model)
        return price, exact

    def _resolve(self, provider: Provider, model: str) -> tuple[str | None, Price | None, bool]:
        pd = self._p[provider]
        hit = self._match(pd, model)
        if hit is not None:
            model_id, exact = hit
            return model_id, pd.models[model_id], exact
        if self.unknown_model == "block" or not pd.models:
            return None, None, False
        model_id = pd.fallback
        if model_id not in pd.models:
            model_id = max(pd.models, key=lambda m: pd.models[m].input + pd.models[m].output)
        return model_id, pd.models[model_id], False

    @staticmethod
    def _match(pd: _ProviderData, model: str) -> tuple[str, bool] | None:
        if model in pd.models:
            return model, True
        name = _normalise(model)
        for candidate in (name, _DATE_SUFFIX.sub("", name)):
            if candidate in pd.models:
                return candidate, True
            if candidate in pd.aliases:
                return pd.aliases[candidate], True
        name = _DATE_SUFFIX.sub("", name)
        best = None
        for known in pd.models:
            if name.startswith(known + "-") and (best is None or len(known) > len(best)):
                best = known
        return (best, False) if best else None

    def cost(self, provider: Provider, model: str, usage: Usage) -> tuple[float, bool]:
        """(usd, estimated) for one request's usage."""
        model_id, price, exact = self._resolve(provider, model)
        if price is None:
            return 0.0, True
        tier = price
        if usage.speed == "fast" and price.fast is not None:
            tier = price.fast
        elif price.long is not None and usage.prompt_tokens > (price.long_context_threshold or 0):
            tier = price.long
        usd = (
            usage.input_tokens * tier.input
            + usage.cache_write_5m * tier.cache_write_5m
            + usage.cache_write_1h * tier.cache_write_1h
            + usage.cache_read * tier.cache_read
            + usage.output_tokens * tier.output
        ) / 1_000_000
        usd *= self._multiplier(provider, model_id, usage)
        usd += usage.web_search_requests * price.web_search_per_1k / 1000
        return usd, not exact

    def _multiplier(self, provider: Provider, model_id: str | None, usage: Usage) -> float:
        pd = self._p[provider]
        mult = 1.0
        if usage.service_tier:
            tiers = {**pd.service_tier, **pd.model_tiers.get(model_id or "", {})}
            mult *= tiers.get(usage.service_tier, 1.0)
        if usage.inference_geo:
            mult *= pd.inference_geo.get(usage.inference_geo, 1.0)
        return mult

    def max_cost(
        self,
        provider: Provider,
        model: str,
        prompt_tokens: int,
        output_tokens: int,
        *,
        speed: str | None = None,
        service_tier: str | None = None,
        inference_geo: str | None = None,
        web_searches: int = 0,
    ) -> float:
        """Upper bound for a request of this size.

        Every input token is priced at the highest input-side rate (uncached, 5m or 1h cache
        write). ``speed``, ``service_tier`` and ``inference_geo`` are the request's own
        settings. ``service_tier`` and ``inference_geo`` left unset (or ``"auto"``) are
        priced at the dearest option, since an account or project default can select it;
        ``speed`` is only priced fast when the request says ``"fast"``, as no default can
        select it. ``web_searches`` is the most server-side searches the request allows.
        """
        model_id, price, _ = self._resolve(provider, model)
        if price is None:
            return 0.0
        tiers = [price]
        if price.long is not None and prompt_tokens > (price.long_context_threshold or 0):
            tiers = [price.long]
        if speed == "fast" and price.fast is not None:
            tiers.append(price.fast)
        in_rate = max(max(t.input, t.cache_write_5m, t.cache_write_1h) for t in tiers)
        out_rate = max(t.output for t in tiers)
        usd = (prompt_tokens * in_rate + output_tokens * out_rate) / 1_000_000
        pd = self._p[provider]
        usd *= _worst(service_tier, {**pd.service_tier, **pd.model_tiers.get(model_id or "", {})})
        usd *= _worst(inference_geo, pd.inference_geo)
        return usd + max(web_searches, 0) * price.web_search_per_1k / 1000

    def models(self) -> list[tuple[Provider, str, Price]]:
        return [(p, m, price) for p, pd in self._p.items() for m, price in pd.models.items()]

    def aliases(self, provider: Provider) -> dict[str, str]:
        return dict(self._p[provider].aliases)


_STANDARD = frozenset({"default", "standard", "standard_only", "global"})


def _worst(value: str | None, multipliers: dict[str, float]) -> float:
    """The multiplier a request setting bills at: its own when known, else the dearest."""
    if value in multipliers:
        return float(multipliers[value])
    if value in _STANDARD:
        return 1.0
    return max([1.0, *map(float, multipliers.values())])


def _normalise(model: str) -> str:
    name = model.strip().lower()
    name = name.rsplit("/", 1)[-1]
    name = _BRACKET_SUFFIX.sub("", name)
    if _BEDROCK_PREFIX.match(name):
        name = _BEDROCK_VERSION.sub("", _BEDROCK_PREFIX.sub("", name))
    return name.replace("@", "-")


def _rates(
    provider: Provider, raw: dict[str, Any], ratios: dict[str, float], fallback: Price | None
) -> dict[str, float]:
    inp = float(raw.get("input", fallback.input if fallback else 0.0))
    out = float(raw.get("output", fallback.output if fallback else 0.0))
    rates = {"input": inp, "output": out}
    for key in CACHE_KEYS:
        if key in raw:
            rates[key] = float(raw[key])
        elif key == "cache_write_1h" and provider is Provider.OPENAI:
            rates[key] = rates["cache_write_5m"]
        else:
            rates[key] = inp * ratios[key]
    return rates


def _ratios_of(price: Price | None, default: dict[str, float]) -> dict[str, float]:
    if price is None or price.input <= 0:
        return default
    return {k: getattr(price, k) / price.input for k in CACHE_KEYS}


def _build(
    provider: Provider, pd: _ProviderData, raw: dict[str, Any], like: Price | None = None
) -> Price:
    """Resolve one model entry. Missing cache rates follow `like`'s ratios, else the defaults."""
    rates = _rates(provider, raw, _ratios_of(like, pd.ratios), None)
    web = float(raw.get("web_search_per_1k", pd.web_search_per_1k))
    base = Price(**rates, web_search_per_1k=web)
    tier_ratios = _ratios_of(base, pd.ratios)
    long = threshold = fast = None
    if "long_context" in raw:
        lc = raw["long_context"]
        threshold = int(lc["threshold"])
        long = Price(**_rates(provider, lc, tier_ratios, base), web_search_per_1k=web)
    if "fast" in raw:
        fast = Price(**_rates(provider, raw["fast"], tier_ratios, base), web_search_per_1k=web)
    return Price(
        **rates, web_search_per_1k=web, long_context_threshold=threshold, long=long, fast=fast
    )
