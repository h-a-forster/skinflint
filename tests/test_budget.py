import threading
from datetime import UTC, datetime, timedelta, tzinfo

import pytest

from skinflint.budget import Budget, window_end, window_start
from skinflint.model import (
    Action,
    BudgetRule,
    Endpoint,
    Per,
    Price,
    Provider,
    Record,
    RequestInfo,
    State,
    Usage,
    Window,
)
from skinflint.store import Store

H = timedelta(hours=1)


class CET(tzinfo):
    """Central European time for 2026 only: DST from Mar 29 01:00 UTC to Oct 25 01:00 UTC."""

    START = datetime(2026, 3, 29, 1)  # UTC
    END = datetime(2026, 10, 25, 1)  # UTC

    def utcoffset(self, dt):
        w = dt.replace(tzinfo=None)
        if datetime(2026, 3, 29, 3) <= w < datetime(2026, 10, 25, 2):
            return 2 * H
        if datetime(2026, 10, 25, 2) <= w < datetime(2026, 10, 25, 3):
            return H if dt.fold else 2 * H
        return H

    def dst(self, dt):
        return self.utcoffset(dt) - H

    def tzname(self, dt):
        return "CET"

    def fromutc(self, dt):
        u = dt.replace(tzinfo=None)
        if self.START <= u < self.END:
            return (u + 2 * H).replace(tzinfo=self)
        fold = int(self.END <= u < self.END + H)
        return (u + H).replace(tzinfo=self, fold=fold)


TZ = CET()


def utc(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def local(dt: float) -> datetime:
    return datetime.fromtimestamp(dt, TZ)


PRICES = {
    "claude-sonnet-5": Price(3.0, 15.0, 3.75, 6.0, 0.3),
    "cheap": Price(1.0, 2.0, 1.25, 2.0, 0.1),
}


class FakePricing:
    def lookup(self, provider, model):
        p = PRICES.get(model)
        return p, p is not None

    def max_cost(self, provider, model, prompt_tokens, output_tokens):
        p = PRICES[model]
        return (prompt_tokens * p.input + output_tokens * p.output) / 1e6


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


NOW = utc(2026, 6, 10, 12)  # Wednesday


def info(model="claude-sonnet-5", prompt=1_000_000, max_out=None) -> RequestInfo:
    return RequestInfo(Provider.ANTHROPIC, Endpoint.MESSAGES, model, True, max_out, prompt)


def rec(clock, scope="default", model="claude-sonnet-5", session=None) -> Record:
    return Record(
        ts=clock(),
        provider=Provider.ANTHROPIC,
        endpoint=Endpoint.MESSAGES,
        scope=scope,
        model=model,
        state=State.PENDING,
        session=session,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "b.db")
    yield s
    s.close()


@pytest.fixture
def clock():
    return Clock(NOW)


def make(store, clock, *rules, reserve="estimate", **kw):
    return Budget(list(rules), store, FakePricing(), reserve, clock, tz=TZ, **kw)


def admit(budget, clock, model="claude-sonnet-5", prompt=1_000_000, max_out=None, **kw):
    return budget.admit(rec(clock, model=model, **kw), info(model, prompt, max_out))


def settle(store, rid, cost, tokens=0):
    r = store.get(rid)
    r.state = State.OK
    r.cost_usd = cost
    r.usage = Usage(input_tokens=tokens)
    store.finish(rid, r, None)


# windows


def test_windows_basic():
    now = utc(2026, 6, 10, 12, 34, 56)  # local 14:34:56 CEST, Wednesday
    assert local(window_start(Window.HOUR, now, TZ)) == datetime(2026, 6, 10, 14, tzinfo=TZ)
    assert window_end(Window.HOUR, now, TZ) - window_start(Window.HOUR, now, TZ) == 3600
    assert window_start(Window.DAY, now, TZ) == utc(2026, 6, 9, 22)
    assert window_end(Window.DAY, now, TZ) == utc(2026, 6, 10, 22)
    assert window_start(Window.WEEK, now, TZ) == utc(2026, 6, 7, 22)  # Monday 8th local
    assert window_end(Window.WEEK, now, TZ) == utc(2026, 6, 14, 22)
    assert window_start(Window.MONTH, now, TZ) == utc(2026, 5, 31, 22)
    assert window_end(Window.MONTH, now, TZ) == utc(2026, 6, 30, 22)
    assert window_start(Window.TOTAL, now, TZ) is None
    assert window_end(Window.TOTAL, now, TZ) is None


def test_windows_across_dst():
    # Spring forward: Sunday Mar 29 is 23 hours long; the week started Monday Mar 23 (CET).
    now = utc(2026, 3, 29, 12)
    assert window_start(Window.DAY, now, TZ) == utc(2026, 3, 28, 23)
    assert window_end(Window.DAY, now, TZ) == utc(2026, 3, 29, 22)
    assert window_start(Window.WEEK, now, TZ) == utc(2026, 3, 22, 23)
    assert window_end(Window.WEEK, now, TZ) == utc(2026, 3, 29, 22)
    assert window_start(Window.MONTH, now, TZ) == utc(2026, 2, 28, 23)
    assert window_end(Window.MONTH, now, TZ) == utc(2026, 3, 31, 22)
    # Fall back: Sunday Oct 25 is 25 hours long; the repeated hour is its own hour window.
    now = utc(2026, 10, 25, 12)
    assert window_end(Window.DAY, now, TZ) - window_start(Window.DAY, now, TZ) == 25 * 3600
    assert window_start(Window.MONTH, now, TZ) == utc(2026, 9, 30, 22)
    assert window_end(Window.MONTH, now, TZ) == utc(2026, 10, 31, 23)
    first, second = utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 1, 30)  # both 02:30 local
    assert window_start(Window.HOUR, first, TZ) == utc(2026, 10, 25, 0)
    assert window_start(Window.HOUR, second, TZ) == utc(2026, 10, 25, 1)
    # December rolls into January; Monday itself starts its week.
    now = utc(2026, 12, 14, 10)
    assert window_end(Window.MONTH, now, TZ) == utc(2026, 12, 31, 23)
    assert window_start(Window.WEEK, now, TZ) == utc(2026, 12, 13, 23)


def test_windows_contain_now_local_time():
    t = utc(2026, 1, 1)
    for _ in range(400):
        for w in (Window.HOUR, Window.DAY, Window.WEEK, Window.MONTH):
            start, end = window_start(w, t), window_end(w, t)
            assert start <= t < end
            s = datetime.fromtimestamp(start)
            assert (s.minute, s.second) == (0, 0)
            if w is not Window.HOUR:
                assert s.hour == 0
            if w is Window.WEEK:
                assert s.weekday() == 0
            if w is Window.MONTH:
                assert s.day == 1
        t += 86400 * 0.97


# admission


def test_daily_cap_blocks_with_message(store, clock):
    b = make(store, clock, BudgetRule("daily", usd=20.0), config_path="/cfg.toml")
    rid = store.insert(rec(clock))
    settle(store, rid, 20.04)
    d = admit(b, clock)
    assert not d.allowed and d.rule.name == "daily"
    assert d.message == (
        "skinflint: budget 'daily' reached: $20.04 of $20.00 used today (resets 00:00). "
        "Edit or remove it in /cfg.toml."
    )
    row = store.get(d.record_id)
    assert row.state is State.BLOCKED and row.blocked_by == "daily" and row.cost_usd == 0
    assert row.error == d.message


def test_admit_inserts_pending_with_reservation(store, clock):
    b = make(store, clock, BudgetRule("daily", usd=20.0))
    d = admit(b, clock, prompt=500_000)
    assert d.allowed and d.message == "" and d.warnings == []
    row = store.get(d.record_id)
    assert row.state is State.PENDING
    assert row.reserved_usd == pytest.approx(1.5) and row.reserved_tokens == 500_000


def test_estimate_vs_worst_case(store, clock):
    rule = BudgetRule("cap", usd=4.0)
    est = make(store, clock, rule)
    d = admit(est, clock, max_out=200_000)  # $3 estimate fits
    assert d.allowed and store.get(d.record_id).reserved_usd == pytest.approx(3.0)
    store.mark_lost(clock() + 1)
    worst = make(store, clock, rule, reserve="worst_case")
    d = admit(worst, clock, max_out=200_000)  # $3 + $3 output
    assert not d.allowed and "this request needs up to $6.00" in d.message
    d = admit(worst, clock, prompt=100_000)  # default 4096 output tokens
    row = store.get(d.record_id)
    assert row.reserved_usd == pytest.approx(0.3 + 4096 * 15 / 1e6)
    assert row.reserved_tokens == 100_000 + 4096


def test_reservation_would_exceed(store, clock):
    b = make(store, clock, BudgetRule("cap", usd=10.0))
    assert admit(b, clock).allowed  # $3
    assert admit(b, clock).allowed  # $6
    assert admit(b, clock).allowed  # $9
    d = admit(b, clock)
    assert not d.allowed
    assert "$9.00 of $10.00 used today" in d.message and "needs up to $3.00" in d.message
    assert admit(b, clock, model="cheap").allowed  # $1 -> exactly $10


def test_pending_counts_until_settled_and_expires(store, clock):
    b = make(store, clock, BudgetRule("cap", usd=5.0), pending_ttl=900)
    d1 = admit(b, clock)  # $3 reserved
    assert not admit(b, clock).allowed
    settle(store, d1.record_id, 0.5)  # actual cost was lower
    assert admit(b, clock).allowed  # 0.5 + 3 <= 5
    clock.t += 901  # the pending reservation expires
    assert admit(b, clock).allowed  # 0.5 + 3 (new pending) ... old one no longer counts
    clock.t += 10
    assert not admit(b, clock).allowed  # 0.5 + 3 + 3 > 5


def test_lost_rows_count_nothing(store, clock):
    b = make(store, clock, BudgetRule("cap", usd=5.0))
    assert admit(b, clock).allowed
    assert not admit(b, clock).allowed
    assert store.mark_lost(clock() + 1) == 1
    assert admit(b, clock).allowed


def test_requests_and_tokens_limits(store, clock):
    b = make(store, clock, BudgetRule("reqs", requests=2, window=Window.HOUR))
    assert admit(b, clock, prompt=10).allowed
    assert admit(b, clock, prompt=10).allowed
    d = admit(b, clock, prompt=10)
    assert not d.allowed and "2 of 2 requests used this hour (resets 15:00)" in d.message
    clock.t += 3600
    assert admit(b, clock, prompt=10).allowed

    b = make(store, clock, BudgetRule("toks", tokens=1000, window=Window.WEEK))
    assert admit(b, clock, prompt=600).allowed  # 10 live pending tokens + 600
    d = admit(b, clock, prompt=600)
    assert not d.allowed and "tokens used this week (resets Mon 00:00)" in d.message
    assert "needs up to 600" in d.message


def test_settled_tokens_count(store, clock):
    b = make(store, clock, BudgetRule("toks", tokens=1000, window=Window.MONTH))
    rid = store.insert(rec(clock))
    settle(store, rid, 0.0, tokens=1000)
    d = admit(b, clock, prompt=1)
    assert not d.allowed and "1.0k of 1.0k tokens used this month (resets Jul 1)" in d.message


def test_per_scope(store, clock):
    b = make(store, clock, BudgetRule("each", usd=4.0, per=Per.SCOPE))
    assert admit(b, clock, scope="a").allowed
    d = admit(b, clock, scope="a")
    assert not d.allowed and "in scope 'a'" in d.message
    assert admit(b, clock, scope="b").allowed


def test_per_session_and_sessionless(store, clock):
    b = make(store, clock, BudgetRule("sess", usd=4.0, per=Per.SESSION, window=Window.TOTAL))
    assert admit(b, clock, session="s1").allowed
    d = admit(b, clock, session="s1")
    assert not d.allowed
    assert "$3.00 of $4.00 used this session (s1)" in d.message and "resets" not in d.message
    assert admit(b, clock, session="s2").allowed
    assert admit(b, clock).allowed  # no session: per-session rules do not apply
    assert admit(b, clock).allowed
    st = b.status()
    assert st[0].spend is None and st[0].note == "n/a: no session"
    st = b.status(session="s1")
    assert st[0].spend.usd == pytest.approx(3.0) and st[0].fraction == pytest.approx(0.75)


def test_per_all_total(store, clock):
    b = make(store, clock, BudgetRule("all", usd=4.0, window=Window.TOTAL))
    assert admit(b, clock, scope="a").allowed
    d = admit(b, clock, scope="b")
    assert not d.allowed and "used in total" in d.message and "resets" not in d.message


def test_per_request(store, clock):
    b = make(store, clock, BudgetRule("one", usd=2.0, per=Per.REQUEST), reserve="worst_case")
    d = admit(b, clock, prompt=500_000, max_out=100_000)  # 1.5 + 1.5
    assert not d.allowed
    assert "this request may cost $3.00, over the $2.00 per-request limit" in d.message
    assert admit(b, clock, prompt=100_000, max_out=1000).allowed
    for _ in range(5):  # no accumulation
        assert admit(b, clock, prompt=100_000, max_out=1000).allowed


def test_globs(store, clock):
    b = make(
        store,
        clock,
        BudgetRule("sonnet", usd=4.0, model="claude-sonnet-*"),
        BudgetRule("work", usd=1.5, scope="work/*", model="cheap"),
    )
    assert admit(b, clock).allowed
    assert not admit(b, clock).allowed
    assert admit(b, clock, model="cheap").allowed  # sonnet rule does not match
    assert admit(b, clock, model="cheap", scope="work/x").allowed
    assert not admit(b, clock, model="cheap", scope="work/y").allowed
    assert admit(b, clock, model="cheap", scope="Work/y").allowed  # case-sensitive


def test_warn_never_blocks(store, clock):
    b = make(
        store,
        clock,
        BudgetRule("soft", usd=1.0, action=Action.WARN),
        BudgetRule("hard", usd=100.0),
    )
    d = admit(b, clock)
    assert d.allowed and len(d.warnings) == 1
    assert d.warnings[0].startswith("skinflint: warning: budget 'soft' reached:")
    d = admit(b, clock)
    assert d.allowed and d.warnings


def test_block_reports_warnings_and_first_blocking_rule(store, clock):
    b = make(
        store,
        clock,
        BudgetRule("soft", usd=1.0, action=Action.WARN),
        BudgetRule("first", usd=2.0),
        BudgetRule("second", usd=2.5),
    )
    d = admit(b, clock)
    assert not d.allowed and d.rule.name == "first" and len(d.warnings) == 1
    assert "the skinflint config" in d.message


def test_unknown_model_blocked(store, clock):
    b = make(store, clock)
    d = admit(b, clock, model="mystery-1")
    assert not d.allowed and "no price known for model 'mystery-1'" in d.message
    assert store.get(d.record_id).state is State.BLOCKED


def test_status(store, clock):
    b = make(
        store,
        clock,
        BudgetRule("daily", usd=10.0, requests=4),
        BudgetRule("scoped", usd=10.0, per=Per.SCOPE),
        BudgetRule("one", usd=1.0, per=Per.REQUEST),
        BudgetRule("work", usd=10.0, scope="work"),
    )
    rid = store.insert(rec(clock, scope="a"))
    settle(store, rid, 2.0)
    st = {s.rule.name: s for s in b.status(scope="a")}
    daily = st["daily"]
    assert daily.spend.usd == 2.0 and daily.spend.requests == 1
    assert daily.limits == {"usd": 10.0, "requests": 4}
    assert daily.remaining == {"usd": 8.0, "requests": 3}
    assert daily.fraction == pytest.approx(0.25)
    assert daily.start == window_start(Window.DAY, NOW, TZ)
    assert st["scoped"].spend.usd == 2.0
    assert st["one"].spend is None and st["work"].spend is None
    assert b.status()[1].note == "n/a: no scope"
    d = daily.to_dict()
    assert d["rule"]["window"] == "day" and d["spend"]["usd"] == 2.0


def test_concurrent_admission_two_processes(tmp_path):
    path = tmp_path / "shared.db"
    stores = [Store(path), Store(path)]
    clock = Clock(NOW)
    rule = BudgetRule("cap", usd=10.0)
    budgets = [make(s, clock, rule) for s in stores]
    results: list[bool] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(20)

    def worker(i):
        try:
            barrier.wait()
            d = admit(budgets[i % 2], clock, model="cheap")  # $1 each
            results.append(d.allowed)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert results.count(True) == 10 and results.count(False) == 10
        assert len(stores[0].records(states=[State.PENDING])) == 10
        assert len(stores[1].records(states=[State.BLOCKED])) == 10
    finally:
        for s in stores:
            s.close()
