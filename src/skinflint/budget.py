"""Budget rules: admission control against the ledger.

A rule applies to a request when its scope and model globs match (``fnmatch.fnmatchcase``,
case-sensitive like SQLite GLOB). Rules group requests by ``per``:

- ``all``: every matching request in the window;
- ``scope``: matching requests with this request's scope;
- ``session``: matching requests with this request's session id. A request without a session
  id is not checked against per-session rules, and ``status()`` reports such rules as n/a;
- ``request``: the request's own reservation against ``usd`` (window ignored).

Windows are calendar periods in local time (hour, day, week from Monday, month), computed
with wall-clock datetime arithmetic so they stay correct across DST changes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, tzinfo
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol

from skinflint.fmt import money
from skinflint.model import (
    Action,
    BudgetRule,
    Decision,
    Per,
    Price,
    Provider,
    Record,
    RequestInfo,
    Spend,
    State,
    Window,
)
from skinflint.store import Store, Txn

# Worst-case assumptions where the request body does not say:
DEFAULT_MAX_OUTPUT = 128_000  # no max_tokens / max_output_tokens: the largest model limit
SERVER_CONTEXT_TOKENS = 1_050_000  # server-held prompt we never saw: the largest context window
UNCAPPED_WEB_SEARCHES = 50  # a web search tool without max_uses
DEFAULT_PENDING_TTL = 900.0

WINDOW_PHRASE = {
    Window.HOUR: "this hour",
    Window.DAY: "today",
    Window.WEEK: "this week",
    Window.MONTH: "this month",
    Window.TOTAL: "in total",
}


class PricingLike(Protocol):
    def lookup(self, provider: Provider, model: str) -> tuple[Price | None, bool]: ...

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
    ) -> float: ...


def _local(now: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(now, tz)


def _start_dt(window: Window, now: float, tz: tzinfo | None) -> datetime | None:
    dt = _local(now, tz)
    if window is Window.HOUR:
        return dt.replace(minute=0, second=0, microsecond=0)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    if window is Window.DAY:
        return midnight
    if window is Window.WEEK:
        return midnight - timedelta(days=dt.weekday())
    if window is Window.MONTH:
        return midnight.replace(day=1)
    return None


def window_start(window: Window, now: float, tz: tzinfo | None = None) -> float | None:
    """Start of the window containing `now` (local time unless `tz`); None for TOTAL."""
    dt = _start_dt(window, now, tz)
    return dt.timestamp() if dt is not None else None


def window_end(window: Window, now: float, tz: tzinfo | None = None) -> float | None:
    """When the window containing `now` resets; None for TOTAL."""
    start = _start_dt(window, now, tz)
    if start is None:
        return None
    if window is Window.HOUR:
        return start.timestamp() + 3600
    if window is Window.DAY:
        end = start + timedelta(days=1)
    elif window is Window.WEEK:
        end = start + timedelta(days=7)
    else:
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else (start.replace(month=start.month + 1))
        )
    return end.timestamp()


def limit_text(usd: float) -> str:
    """A configured limit as written: $0, $0.01, $0.025, $5.00."""
    if usd == 0:
        return "$0"
    if usd < 0.00005:
        return "<$0.0001"
    if round(usd, 4) >= 1:
        return f"${usd:,.2f}"
    whole, frac = f"{usd:.4f}".split(".")
    return f"${whole}.{frac.rstrip('0').ljust(2, '0')}"


def tokens_text(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


@dataclass(slots=True)
class RuleStatus:
    """Spend against one rule in its current window. `spend` is None when the rule does not
    apply to the given scope / session (see `note`)."""

    rule: BudgetRule
    start: float | None
    end: float | None
    spend: Spend | None
    limits: dict[str, float]
    remaining: dict[str, float]
    fraction: float | None  # highest used / limit across the rule's limits
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rule"] = {
            k: (str(v) if k in ("window", "per", "action") else v)
            for k, v in asdict(self.rule).items()
        }
        return d


def _limits(rule: BudgetRule) -> dict[str, float]:
    out: dict[str, float] = {}
    if rule.usd is not None:
        out["usd"] = rule.usd
    if rule.tokens is not None:
        out["tokens"] = rule.tokens
    if rule.requests is not None:
        out["requests"] = rule.requests
    return out


class Budget:
    def __init__(
        self,
        rules: list[BudgetRule],
        store: Store,
        pricing: PricingLike,
        reserve: str = "estimate",
        clock: Callable[[], float] = time.time,
        *,
        config_path: Path | str | None = None,
        pending_ttl: float = DEFAULT_PENDING_TTL,
        tz: tzinfo | None = None,
    ):
        if reserve not in ("estimate", "worst_case"):
            raise ValueError(f"reserve must be 'estimate' or 'worst_case', got {reserve!r}")
        self.rules = list(rules)
        self.store = store
        self.pricing = pricing
        self.reserve = reserve
        self.clock = clock
        self.config_path = config_path
        self.pending_ttl = pending_ttl
        self.tz = tz

    def applies(self, rule: BudgetRule, scope: str, model: str) -> bool:
        return fnmatchcase(scope, rule.scope) and fnmatchcase(model, rule.model)

    def reservation(self, info: RequestInfo, txn: Txn | None = None) -> tuple[float, int] | None:
        """(usd, tokens) to hold while the request is in flight; None if the model has no
        price (unknown_model = 'block'). `txn` lets the worst case look up the size of an
        OpenAI ``previous_response_id`` in the ledger."""
        price, _exact = self.pricing.lookup(info.provider, info.model)
        if price is None:
            return None
        if self.reserve == "worst_case":
            prompt = max(info.max_prompt_tokens, info.est_prompt_tokens, 0)
            prompt += self._server_context(info, txn)
            out = info.max_output_tokens
            out = DEFAULT_MAX_OUTPUT if out is None else out
            searches = info.web_searches
            usd = self.pricing.max_cost(
                info.provider,
                info.model,
                prompt,
                out,
                speed=info.speed,
                service_tier=info.service_tier,
                inference_geo=info.inference_geo,
                web_searches=UNCAPPED_WEB_SEARCHES if searches is None else searches,
            )
            return usd, prompt + out
        prompt = max(info.est_prompt_tokens, 0)
        return prompt * price.input / 1_000_000, prompt

    @staticmethod
    def _server_context(info: RequestInfo, txn: Txn | None) -> int:
        """Tokens of a prompt the provider holds (previous_response_id, conversation)."""
        if info.server_context:
            return SERVER_CONTEXT_TOKENS
        if info.previous_response_id is None:
            return 0
        known = txn.response_tokens(info.provider, info.previous_response_id) if txn else None
        return SERVER_CONTEXT_TOKENS if known is None else known

    def admit(self, record: Record, info: RequestInfo) -> Decision:
        """Check every applicable rule and insert the ledger row, atomically."""
        now = self.clock()
        warnings: list[str] = []
        with self.store.immediate() as txn:
            res = self.reservation(info, txn)
            if res is None:
                msg = (
                    f"skinflint: no price known for model '{info.model}' and unknown models "
                    f'are blocked. Add a price for it or set limits.unknown_model = "max" '
                    f"in {self._where()}."
                )
                self._insert_blocked(txn, record, None, msg)
                return Decision(allowed=False, message=msg, record_id=record.id)
            res_usd, res_tokens = res
            blocker: tuple[BudgetRule, str] | None = None
            for rule in self.rules:
                if not self.applies(rule, record.scope, record.model):
                    continue
                if rule.per is Per.SESSION and record.session is None:
                    continue
                reason = self._check(txn, rule, record, now, res_usd, res_tokens)
                if reason is None:
                    continue
                if rule.action is Action.WARN:
                    warnings.append(f"skinflint: warning: budget '{rule.name}' {reason}.")
                elif blocker is None:
                    blocker = (rule, reason)
            if blocker is not None:
                rule, reason = blocker
                fix = rule.hint or f"Edit or remove it in {self._where()}."
                msg = f"skinflint: budget '{rule.name}' {reason}. {fix}"
                self._insert_blocked(txn, record, rule.name, msg)
                return Decision(
                    allowed=False,
                    message=msg,
                    rule=rule,
                    warnings=warnings,
                    record_id=record.id,
                )
            record.state = State.PENDING
            record.reserved_usd = res_usd
            record.reserved_tokens = res_tokens
            txn.insert(record)
        return Decision(allowed=True, warnings=warnings, record_id=record.id)

    def status(
        self, scope: str | None = None, session: str | None = None, now: float | None = None
    ) -> list[RuleStatus]:
        """Per rule: spend vs limits in the current window."""
        now = self.clock() if now is None else now
        out = []
        with self.store.read() as txn:
            for rule in self.rules:
                start = window_start(rule.window, now, self.tz)
                end = window_end(rule.window, now, self.tz)
                limits = _limits(rule)
                note = None
                if rule.per is Per.REQUEST:
                    note = "n/a: checked per request"
                elif rule.per is Per.SESSION and session is None:
                    note = "n/a: no session"
                elif rule.per is Per.SCOPE and scope is None:
                    note = "n/a: no scope"
                elif scope is not None and not fnmatchcase(scope, rule.scope):
                    note = "n/a: scope does not match"
                if note:
                    out.append(RuleStatus(rule, start, end, None, limits, {}, None, note))
                    continue
                spend = txn.spend(
                    start,
                    scope_glob=rule.scope,
                    model_glob=rule.model,
                    scope=scope if rule.per is Per.SCOPE else None,
                    session=session if rule.per is Per.SESSION else None,
                    now=now,
                    pending_ttl=self.pending_ttl,
                )
                used = {"usd": spend.usd, "tokens": spend.tokens, "requests": spend.requests}
                remaining = {k: max(v - used[k], 0) for k, v in limits.items()}
                fractions = [
                    used[k] / v if v else (1.0 if used[k] else 0.0) for k, v in limits.items()
                ]
                out.append(
                    RuleStatus(
                        rule,
                        start,
                        end,
                        spend,
                        limits,
                        remaining,
                        max(fractions) if fractions else None,
                    )
                )
        return out

    def _check(
        self,
        txn: Txn,
        rule: BudgetRule,
        record: Record,
        now: float,
        res_usd: float,
        res_tokens: int,
    ) -> str | None:
        """Why this rule refuses the request (phrase for the message), or None."""
        if rule.per is Per.REQUEST:
            if rule.usd is not None and res_usd > rule.usd:
                return (
                    f"reached: this request may cost {money(res_usd)}, over the "
                    f"{limit_text(rule.usd)} per-request limit"
                )
            return None
        spend = txn.spend(
            window_start(rule.window, now, self.tz),
            scope_glob=rule.scope,
            model_glob=rule.model,
            scope=record.scope if rule.per is Per.SCOPE else None,
            session=record.session if rule.per is Per.SESSION else None,
            now=now,
            pending_ttl=self.pending_ttl,
        )
        where = self._window_text(rule, record, now)
        if rule.usd is not None and (spend.usd >= rule.usd or spend.usd + res_usd > rule.usd):
            text = f"reached: {money(spend.usd)} of {limit_text(rule.usd)} used {where}"
            if spend.usd < rule.usd:
                text += f"; this request needs up to {money(res_usd)}"
            return text
        if rule.tokens is not None and (
            spend.tokens >= rule.tokens or spend.tokens + res_tokens > rule.tokens
        ):
            text = (
                f"reached: {tokens_text(spend.tokens)} of {tokens_text(rule.tokens)} tokens "
                f"used {where}"
            )
            if spend.tokens < rule.tokens:
                text += f"; this request needs up to {tokens_text(res_tokens)}"
            return text
        if rule.requests is not None and spend.requests + 1 > rule.requests:
            return f"reached: {spend.requests} of {rule.requests} requests used {where}"
        return None

    def _window_text(self, rule: BudgetRule, record: Record, now: float) -> str:
        if rule.per is Per.SESSION and rule.window is Window.TOTAL:
            phrase = f"this session ({record.session})"
        else:
            phrase = WINDOW_PHRASE[rule.window]
            if rule.per is Per.SESSION:
                phrase += f" in session {record.session}"
        if rule.per is Per.SCOPE:
            phrase += f" in scope '{record.scope}'"
        end = window_end(rule.window, now, self.tz)
        if end is not None:
            dt = datetime.fromtimestamp(end, self.tz)
            if rule.window in (Window.HOUR, Window.DAY):
                when = f"{dt:%H:%M}"
            elif rule.window is Window.WEEK:
                when = f"{dt:%a %H:%M}"
            else:
                when = f"{dt:%b} {dt.day}"
            phrase += f" (resets {when})"
        return phrase

    def _where(self) -> str:
        return str(self.config_path) if self.config_path else "the skinflint config"

    def _insert_blocked(
        self, txn: Txn, record: Record, rule_name: str | None, message: str
    ) -> None:
        record.state = State.BLOCKED
        record.blocked_by = rule_name
        record.error = message
        record.cost_usd = 0.0
        record.reserved_usd = 0.0
        record.reserved_tokens = 0
        txn.insert(record)
