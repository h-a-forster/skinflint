import sqlite3
import threading
from datetime import datetime

import pytest

from skinflint.model import Endpoint, Provider, Record, Segment, State, Usage
from skinflint.store import SCHEMA_VERSION, Store, StoreError

NOW = 1_790_000_000.0


def rec(**kw) -> Record:
    base = dict(
        ts=NOW,
        provider=Provider.ANTHROPIC,
        endpoint=Endpoint.MESSAGES,
        scope="default",
        model="claude-sonnet-5",
        state=State.OK,
    )
    base.update(kw)
    return Record(**base)


def full_record() -> Record:
    return Record(
        ts=NOW + 0.123456,
        provider=Provider.OPENAI,
        endpoint=Endpoint.RESPONSES,
        scope="proj/a",
        model="gpt-6",
        state=State.ERROR,
        session="sess-1",
        agent="fp:deadbeef",
        client="codex/1.0",
        stream=True,
        status=500,
        usage=Usage(1, 2, 3, 4, 5, 6, 7, "fast", "priority", "us"),
        cost_usd=0.123456789,
        cost_estimated=True,
        reserved_usd=0.5,
        reserved_tokens=99,
        duration_ms=1234,
        ttft_ms=56,
        upstream_id="resp_1",
        blocked_by=None,
        error="boom",
        plan=True,
        request_class="subagent",
        cache_miss_reason="tools_changed",
        cache_missed_tokens=4321,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "ledger.db")
    yield s
    s.close()


def test_migration_from_empty_file(tmp_path):
    path = tmp_path / "empty.db"
    path.write_bytes(b"")
    with Store(path) as s:
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master")}
        assert {"requests", "profiles", "ratelimits", "requests_scope_ts"} <= tables
        s.insert(rec())
    with Store(path) as s:
        assert len(s.records()) == 1


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(StoreError, match="newer"):
        Store(path)


def test_record_round_trip(store):
    r = full_record()
    rid = store.insert(r)
    assert r.id == rid
    assert store.get(rid) == r
    minimal = rec()
    store.insert(minimal)
    assert store.get(minimal.id) == minimal
    assert store.get(9999) is None


def test_finish_and_segments(store):
    r = rec(state=State.PENDING, reserved_usd=1.5, reserved_tokens=1000)
    rid = store.insert(r)
    segs = [
        Segment("tools", "tool", "Bash", "tools: built-in", 100, "a" * 16),
        Segment("messages", "user_text", "prompt", "user", 20, "b" * 16, 0, True, "1h", 7),
    ]
    final = rec(
        state=State.OK,
        status=200,
        usage=Usage(10, 20, 0, 30, 40),
        cost_usd=0.25,
        model="claude-sonnet-5-20260101",
        upstream_id="msg_1",
        reserved_usd=1.5,
    )
    store.finish(rid, final, segs, body=b"{}")
    got = store.get(rid)
    assert got.state is State.OK and got.status == 200 and got.cost_usd == 0.25
    assert got.usage == Usage(10, 20, 0, 30, 40)
    assert got.reserved_usd == 0 and got.reserved_tokens == 0
    assert got.model == "claude-sonnet-5-20260101"
    assert store.segments(rid) == segs
    assert store.body(rid) == b"{}"
    rid2 = store.insert(rec(state=State.PENDING))
    store.finish(rid2, rec(), None)
    assert store.segments(rid2) is None and store.body(rid2) is None


def test_spend_semantics(store):
    store.insert(rec(state=State.OK, cost_usd=1.0, usage=Usage(100, output_tokens=10)))
    store.insert(rec(state=State.ERROR, cost_usd=0.5, usage=Usage(cache_read=5)))
    store.insert(rec(state=State.ABORTED, cost_usd=0.25))
    store.insert(rec(state=State.PENDING, reserved_usd=2.0, reserved_tokens=200))
    store.insert(rec(state=State.PENDING, ts=NOW - 1000, reserved_usd=8.0, reserved_tokens=8))
    store.insert(rec(state=State.BLOCKED, cost_usd=0.0, reserved_usd=9.0))
    store.insert(rec(state=State.LOST, reserved_usd=9.0, reserved_tokens=9))
    with store.immediate() as txn:
        sp = txn.spend(None, now=NOW, pending_ttl=900)
    assert sp.usd == pytest.approx(3.75)
    assert sp.tokens == 110 + 5 + 200
    assert sp.requests == 4
    with store.immediate() as txn:
        sp = txn.spend(None, now=NOW, pending_ttl=2000)
    assert sp.usd == pytest.approx(11.75) and sp.requests == 5
    with store.immediate() as txn:
        assert txn.spend(NOW + 1, now=NOW).requests == 0


def test_spend_filters_and_globs(store):
    store.insert(rec(scope="work", model="claude-opus-5", cost_usd=1, session="s1"))
    store.insert(rec(scope="work", model="claude-haiku-5", cost_usd=2, session="s2"))
    store.insert(rec(scope="home", model="gpt-6", cost_usd=4))
    store.insert(rec(scope="Work", model="claude-opus-5", cost_usd=8))
    with store.immediate() as txn:
        assert txn.spend(None, scope_glob="w*", now=NOW).usd == 3
        assert txn.spend(None, model_glob="claude-*", now=NOW).usd == 11
        assert txn.spend(None, scope_glob="[wW]ork", model_glob="*opus*", now=NOW).usd == 9
        assert txn.spend(None, scope="home", now=NOW).usd == 4
        assert txn.spend(None, session="s2", now=NOW).usd == 2


def test_immediate_rolls_back(store):
    with pytest.raises(RuntimeError), store.immediate() as txn:
        txn.insert(rec())
        raise RuntimeError
    assert store.records() == []


def test_mark_lost(store):
    old = store.insert(rec(state=State.PENDING, ts=NOW - 5000))
    new = store.insert(rec(state=State.PENDING, ts=NOW))
    done = store.insert(rec(state=State.OK, ts=NOW - 5000))
    assert store.mark_lost(NOW - 1000) == 1
    assert store.get(old).state is State.LOST
    assert store.get(new).state is State.PENDING
    assert store.get(done).state is State.OK


def test_ratelimit(store):
    assert store.ratelimit(Provider.ANTHROPIC) is None
    store.save_ratelimit(Provider.ANTHROPIC, {"a": "1"}, NOW)
    store.save_ratelimit(Provider.ANTHROPIC, {"b": "2"}, NOW + 1)
    assert store.ratelimit(Provider.ANTHROPIC) == (NOW + 1, {"b": "2"})


def test_records_and_last(store):
    for i in range(5):
        store.insert(rec(ts=NOW + i, session="s" if i % 2 else None, scope=f"sc{i % 2}"))
    store.insert(rec(ts=NOW + 10, state=State.BLOCKED, model="m2"))
    assert [r.ts for r in store.records(since=NOW + 1, until=NOW + 4)] == [
        NOW + 1,
        NOW + 2,
        NOW + 3,
    ]
    assert len(store.records(session="s")) == 2
    assert len(store.records(scope="sc0")) == 3
    assert len(store.records(model="m2")) == 1
    assert len(store.records(states=[State.BLOCKED])) == 1
    assert [r.ts for r in store.records(order="desc", limit=2)] == [NOW + 10, NOW + 4]
    assert store.last().ts == NOW + 10
    assert store.last(session="s").ts == NOW + 3
    assert store.last(scope="nope") is None


def test_sessions(store):
    store.insert(
        rec(
            ts=NOW,
            session="a",
            scope="x",
            model="m1",
            cost_usd=1,
            usage=Usage(10, cache_read=30),
            client="c/1",
        )
    )
    store.insert(
        rec(
            ts=NOW + 5,
            session="a",
            scope="y",
            model="m2",
            cost_usd=2,
            cost_estimated=True,
            client="c/2",
        )
    )
    store.insert(rec(ts=NOW + 6, session="a", state=State.BLOCKED, model="m1"))
    store.insert(rec(ts=NOW + 1, session="b", plan=True))
    store.insert(rec(ts=NOW + 9))
    sess = store.sessions()
    assert [s.id for s in sess] == ["a", "b"]
    a = sess[0]
    assert (a.first_ts, a.last_ts, a.requests, a.blocked) == (NOW, NOW + 6, 2, 1)
    assert a.scope == "default"  # latest request
    assert a.cost_usd == 3 and a.cost_estimated and not a.plan
    assert a.models == ["m1", "m2"]
    assert a.usage.input_tokens == 10 and a.usage.cache_read == 30
    assert sess[1].plan
    d = a.to_dict()
    assert d["usage"]["prompt_tokens"] == 40 and d["models"] == a.models
    assert [s.id for s in store.sessions(since=NOW + 2)] == ["a"]


def test_aggregate(store):
    store.insert(
        rec(
            scope="x",
            model="m1",
            cost_usd=1,
            usage=Usage(10, cache_read=90),
            agent="A",
            client="c",
            request_class="main",
            session="s1",
        )
    )
    store.insert(rec(scope="x", model="m2", cost_usd=2, state=State.ERROR, agent="A"))
    store.insert(
        rec(scope="y", model="m1", cost_usd=4, provider=Provider.OPENAI, endpoint=Endpoint.CHAT)
    )
    store.insert(rec(scope="y", model="m1", state=State.BLOCKED))
    by_scope = {a.key: a for a in store.aggregate("scope")}
    assert by_scope["x"].requests == 2 and by_scope["x"].errors == 1
    assert by_scope["x"].cost_usd == 3 and by_scope["x"].cache_hit_rate == 0.9
    assert by_scope["y"].blocked == 1 and by_scope["y"].requests == 1
    assert [a.key for a in store.aggregate("scope")] == ["y", "x"]  # by cost
    assert {a.key: a.cost_usd for a in store.aggregate("model")} == {"m1": 5, "m2": 2}
    assert {a.key for a in store.aggregate("provider")} == {"anthropic", "openai"}
    assert {a.key for a in store.aggregate("agent")} == {"A", None}
    assert {a.key for a in store.aggregate("client")} == {"c", None}
    assert {a.key for a in store.aggregate("request_class")} == {"main", None}
    assert {a.key for a in store.aggregate("session")} == {"s1", None}
    assert [a.key for a in store.aggregate("scope", scope="x")] == ["x"]
    assert store.aggregate("scope", since=NOW + 1) == []
    assert by_scope["x"].to_dict()["cache_hit_rate"] == 0.9
    with pytest.raises(ValueError):
        store.aggregate("nope")


def test_aggregate_day_is_local(store):
    day1 = datetime(2026, 3, 3, 23, 59).timestamp()
    day2 = datetime(2026, 3, 4, 0, 1).timestamp()
    store.insert(rec(ts=day1, cost_usd=1))
    store.insert(rec(ts=day2, cost_usd=2))
    store.insert(rec(ts=day2 + 60, cost_usd=4))
    days = store.aggregate("day")
    assert [(a.key, a.cost_usd) for a in days] == [("2026-03-03", 1), ("2026-03-04", 6)]


def test_prune(store):
    segs = [Segment("tools", "tool", "Bash", "g", 1, "a" * 16)]
    ids = []
    for i, state in enumerate([State.OK, State.OK, State.PENDING, State.OK]):
        rid = store.insert(rec(ts=NOW + i * 100, state=state))
        store.finish(rid, rec(ts=NOW + i * 100, state=state), segs)
        ids.append(rid)
    pending = store.insert(rec(ts=NOW - 50, state=State.PENDING))
    assert store.prune(profiles_before=NOW + 250, records_before=NOW + 50) == (3, 1)
    assert store.get(ids[0]) is None
    assert store.get(pending) is not None
    assert store.segments(ids[1]) is None and store.segments(ids[3]) == segs
    assert store.prune(None, None) == (0, 0)


def test_open_readonly(tmp_path):
    path = tmp_path / "ro.db"
    with pytest.raises(StoreError):
        Store.open_readonly(path)
    assert not path.exists()
    writer = Store(path)
    writer.insert(rec(cost_usd=1))
    ro = Store.open_readonly(path)
    try:
        with writer.immediate() as txn:  # writer holds the write lock; reader still works
            txn.insert(rec(cost_usd=2))
            assert len(ro.records()) == 1
        assert len(ro.records()) == 2
        with ro.read() as txn:
            assert txn.spend(None, now=NOW).usd == 3
        with pytest.raises(sqlite3.OperationalError):
            ro.insert(rec())
    finally:
        ro.close()
        writer.close()


def test_two_stores_concurrent_writes(tmp_path):
    path = tmp_path / "c.db"
    a, b = Store(path), Store(path)
    errors: list[BaseException] = []
    n = 200

    def hammer():
        try:
            for _ in range(n):
                a.insert(rec(cost_usd=1.0))
        except BaseException as e:
            errors.append(e)

    def checker():
        try:
            for _ in range(n):
                with b.immediate() as txn:
                    before = txn.spend(None, now=NOW).requests
                    txn.insert(rec(cost_usd=1.0, scope="checker"))
                    after = txn.spend(None, now=NOW).requests
                    assert after == before + 1
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer), threading.Thread(target=checker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert len(a.records()) == 2 * n
        assert len(b.records(scope="checker")) == n
    finally:
        a.close()
        b.close()
