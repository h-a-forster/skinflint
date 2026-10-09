"""SQLite request ledger.

One connection per Store; several processes may each open their own Store on the same file
(WAL mode, busy timeout). Budget checks run inside ``Store.immediate()`` so that a check and
the insert that follows it are atomic across processes.

Scope and model filters given as globs use SQLite GLOB, which is case-sensitive (matching
``fnmatch.fnmatchcase`` used by the budget module).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from skinflint.model import Endpoint, Provider, Record, Segment, Spend, State, Usage

USAGE_FIELDS = (
    "input_tokens",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "output_tokens",
    "reasoning_tokens",
    "web_search_requests",
    "speed",
    "service_tier",
    "inference_geo",
)
USAGE_SUMS = USAGE_FIELDS[:7]
RECORD_FIELDS = (
    "ts",
    "provider",
    "endpoint",
    "scope",
    "model",
    "state",
    "session",
    "agent",
    "client",
    "stream",
    "status",
    "cost_usd",
    "cost_estimated",
    "reserved_usd",
    "reserved_tokens",
    "duration_ms",
    "ttft_ms",
    "upstream_id",
    "blocked_by",
    "error",
    "plan",
    "request_class",
    "cache_miss_reason",
    "cache_missed_tokens",
)
COLUMNS = RECORD_FIELDS + USAGE_FIELDS
BOOL_FIELDS = {"stream", "cost_estimated", "plan"}
SEGMENT_FIELDS = (
    "section",
    "kind",
    "label",
    "group",
    "chars",
    "hash",
    "message",
    "breakpoint",
    "ttl",
    "est_tokens",
)
SETTLED = (State.OK.value, State.ERROR.value, State.ABORTED.value)
TOTAL_TOKENS_SQL = "(input_tokens + cache_write_5m + cache_write_1h + cache_read + output_tokens)"
AGGREGATE_KEYS = {
    "scope": "scope",
    "session": "session",
    "model": "model",
    "day": "local_day(ts)",
    "agent": "agent",
    "client": "client",
    "provider": "provider",
    "request_class": "request_class",
}

MIGRATIONS: list[list[str]] = [
    [
        """CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            provider TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            scope TEXT NOT NULL,
            model TEXT NOT NULL,
            state TEXT NOT NULL,
            session TEXT,
            agent TEXT,
            client TEXT,
            stream INTEGER NOT NULL DEFAULT 0,
            status INTEGER,
            cost_usd REAL NOT NULL DEFAULT 0,
            cost_estimated INTEGER NOT NULL DEFAULT 0,
            reserved_usd REAL NOT NULL DEFAULT 0,
            reserved_tokens INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER,
            ttft_ms INTEGER,
            upstream_id TEXT,
            blocked_by TEXT,
            error TEXT,
            plan INTEGER NOT NULL DEFAULT 0,
            request_class TEXT,
            cache_miss_reason TEXT,
            cache_missed_tokens INTEGER,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_5m INTEGER NOT NULL DEFAULT 0,
            cache_write_1h INTEGER NOT NULL DEFAULT 0,
            cache_read INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            web_search_requests INTEGER NOT NULL DEFAULT 0,
            speed TEXT,
            service_tier TEXT,
            inference_geo TEXT
        )""",
        "CREATE INDEX requests_ts ON requests (ts)",
        "CREATE INDEX requests_session_ts ON requests (session, ts)",
        "CREATE INDEX requests_scope_ts ON requests (scope, ts)",
        "CREATE INDEX requests_state ON requests (state)",
        """CREATE TABLE profiles (
            request_id INTEGER PRIMARY KEY REFERENCES requests (id) ON DELETE CASCADE,
            segments BLOB,
            body BLOB
        )""",
        """CREATE TABLE ratelimits (
            provider TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            headers TEXT NOT NULL
        )""",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)


class StoreError(RuntimeError):
    pass


@dataclass(slots=True)
class SessionSummary:
    id: str
    scope: str
    first_ts: float
    last_ts: float
    requests: int = 0  # every row except BLOCKED
    blocked: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    cost_estimated: bool = False
    plan: bool = False
    models: list[str] = field(default_factory=list)
    client: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["usage"] = _usage_dict(self.usage)
        return d


@dataclass(slots=True)
class Aggregate:
    key: str | None
    requests: int = 0  # every row except BLOCKED
    blocked: int = 0
    errors: int = 0  # state ERROR
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0

    @property
    def cache_hit_rate(self) -> float | None:
        return self.usage.cache_hit_rate

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["usage"] = _usage_dict(self.usage)
        d["cache_hit_rate"] = self.cache_hit_rate
        return d


def _usage_dict(u: Usage) -> dict[str, Any]:
    d = asdict(u)
    d["prompt_tokens"] = u.prompt_tokens
    d["total_tokens"] = u.total_tokens
    return d


def _local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def _record_values(r: Record) -> list[Any]:
    out: list[Any] = []
    for name in RECORD_FIELDS:
        v = getattr(r, name)
        if name in BOOL_FIELDS:
            v = int(bool(v))
        elif name in ("provider", "endpoint", "state"):
            v = str(v)
        out.append(v)
    out.extend(getattr(r.usage, name) for name in USAGE_FIELDS)
    return out


def _row_record(row: sqlite3.Row) -> Record:
    kw = {name: row[name] for name in RECORD_FIELDS}
    for name in BOOL_FIELDS:
        kw[name] = bool(kw[name])
    kw["provider"] = Provider(kw["provider"])
    kw["endpoint"] = Endpoint(kw["endpoint"])
    kw["state"] = State(kw["state"])
    usage = Usage(**{name: row[name] for name in USAGE_FIELDS})
    return Record(id=row["id"], usage=usage, **kw)


def encode_segments(segments: list[Segment]) -> bytes:
    rows = [[getattr(s, f) for f in SEGMENT_FIELDS] for s in segments]
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode())


def decode_segments(blob: bytes) -> list[Segment]:
    rows = json.loads(zlib.decompress(blob))
    return [Segment(**dict(zip(SEGMENT_FIELDS, row, strict=True))) for row in rows]


class Txn:
    """Queries that run inside one transaction (see Store.immediate / Store.read)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def spend(
        self,
        since: float | None,
        *,
        scope_glob: str = "*",
        model_glob: str = "*",
        scope: str | None = None,
        session: str | None = None,
        now: float,
        pending_ttl: float = 900,
    ) -> Spend:
        """Settled rows count cost and tokens; live PENDING rows count their reservation;
        BLOCKED, LOST and stale PENDING rows count nothing."""
        live = f"(state = 'pending' AND ts >= {float(now - pending_ttl)!r})"
        settled = f"state IN {SETTLED!r}"
        where = ["scope GLOB ?", "model GLOB ?"]
        args: list[Any] = [scope_glob, model_glob]
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if scope is not None:
            where.append("scope = ?")
            args.append(scope)
        if session is not None:
            where.append("session = ?")
            args.append(session)
        sql = f"""SELECT
            COALESCE(SUM(CASE WHEN {settled} THEN cost_usd
                              WHEN {live} THEN reserved_usd ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN {settled} THEN {TOTAL_TOKENS_SQL}
                              WHEN {live} THEN reserved_tokens ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN {settled} OR {live} THEN 1 ELSE 0 END), 0)
            FROM requests WHERE {" AND ".join(where)}"""
        usd, tokens, requests = self._conn.execute(sql, args).fetchone()
        return Spend(usd=float(usd), tokens=int(tokens), requests=int(requests))

    def insert(self, record: Record) -> int:
        cols = ", ".join(COLUMNS)
        marks = ", ".join("?" * len(COLUMNS))
        cur = self._conn.execute(
            f"INSERT INTO requests ({cols}) VALUES ({marks})", _record_values(record)
        )
        record.id = cur.lastrowid
        return cur.lastrowid


class Store:
    def __init__(self, path: Path | str, *, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self._lock = threading.RLock()
        if readonly:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        conn.create_function("local_day", 1, _local_day, deterministic=True)
        self._conn = conn
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            if readonly:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != SCHEMA_VERSION:
                    raise StoreError(
                        f"{self.path}: schema version {version}, expected {SCHEMA_VERSION}"
                    )
            else:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA foreign_keys = ON")
                self._migrate()
        except BaseException:
            conn.close()
            raise

    @classmethod
    def open_readonly(cls, path: Path | str) -> Store:
        """Read-only connection; never creates files. Raises if the database is missing."""
        if not Path(path).exists():
            raise StoreError(f"{path}: no such database")
        return cls(path, readonly=True)

    def _migrate(self) -> None:
        with self.immediate():
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path}: schema version {version} is newer than this skinflint "
                    f"({SCHEMA_VERSION}); upgrade skinflint"
                )
            for v in range(version, SCHEMA_VERSION):
                for stmt in MIGRATIONS[v]:
                    self._conn.execute(stmt)
            if version != SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def immediate(self) -> Iterator[Txn]:
        """BEGIN IMMEDIATE ... COMMIT; ROLLBACK on exception."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield Txn(self._conn)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[Txn]:
        """A deferred (read) transaction: a consistent snapshot for several queries."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield Txn(self._conn)
            finally:
                self._conn.execute("COMMIT")

    def insert(self, record: Record) -> int:
        with self.immediate() as txn:
            return txn.insert(record)

    def finish(
        self,
        record_id: int,
        record: Record,
        segments: list[Segment] | None,
        body: bytes | None = None,
    ) -> None:
        """Settle a row with the final record (ts, provider, endpoint and scope are kept);
        zero its reservation; store the profile."""
        keep = {"ts", "provider", "endpoint", "scope"}
        names = [n for n in COLUMNS if n not in keep]
        values = dict(zip(COLUMNS, _record_values(record), strict=True))
        values["reserved_usd"] = 0.0
        values["reserved_tokens"] = 0
        sets = ", ".join(f"{n} = ?" for n in names)
        with self.immediate():
            self._conn.execute(
                f"UPDATE requests SET {sets} WHERE id = ?",
                [values[n] for n in names] + [record_id],
            )
            if segments is not None or body is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO profiles (request_id, segments, body) VALUES (?, ?, ?)",
                    (
                        record_id,
                        encode_segments(segments) if segments is not None else None,
                        body,
                    ),
                )

    def mark_lost(self, older_than: float) -> int:
        """PENDING rows that arrived before `older_than` become LOST."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET state = 'lost' WHERE state = 'pending' AND ts < ?",
                (older_than,),
            )
            return cur.rowcount

    def save_ratelimit(self, provider: Provider, headers: dict[str, str], ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ratelimits (provider, ts, headers) VALUES (?, ?, ?)",
                (str(provider), ts, json.dumps(headers, separators=(",", ":"))),
            )

    def ratelimit(self, provider: Provider) -> tuple[float, dict[str, str]] | None:
        row = self._one("SELECT ts, headers FROM ratelimits WHERE provider = ?", (str(provider),))
        return (row["ts"], json.loads(row["headers"])) if row else None

    def get(self, record_id: int) -> Record | None:
        row = self._one("SELECT * FROM requests WHERE id = ?", (record_id,))
        return _row_record(row) if row else None

    def last(self, session: str | None = None, scope: str | None = None) -> Record | None:
        found = self.records(session=session, scope=scope, limit=1, order="desc")
        return found[0] if found else None

    def records(
        self,
        since: float | None = None,
        until: float | None = None,
        scope: str | None = None,
        session: str | None = None,
        model: str | None = None,
        states: Iterable[State] | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[Record]:
        """Rows with since <= ts < until, exact scope / session / model matches."""
        if order not in ("asc", "desc"):
            raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")
        where, args = self._filters(since, until, scope, session)
        if model is not None:
            where.append("model = ?")
            args.append(model)
        if states is not None:
            states = [str(s) for s in states]
            where.append(f"state IN ({', '.join('?' * len(states))})")
            args.extend(states)
        sql = f"SELECT * FROM requests WHERE {' AND '.join(where)} ORDER BY ts {order}, id {order}"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [_row_record(r) for r in self._all(sql, args)]

    def segments(self, record_id: int) -> list[Segment] | None:
        row = self._one("SELECT segments FROM profiles WHERE request_id = ?", (record_id,))
        return decode_segments(row["segments"]) if row and row["segments"] is not None else None

    def body(self, record_id: int) -> bytes | None:
        row = self._one("SELECT body FROM profiles WHERE request_id = ?", (record_id,))
        return row["body"] if row else None

    def sessions(self, since: float | None = None) -> list[SessionSummary]:
        """One summary per session id (rows without a session are left out), newest first.
        `scope` and `client` come from the session's latest request."""
        where, args = self._filters(since, None, None, None)
        sums = ", ".join(f"SUM({n}) AS {n}" for n in USAGE_SUMS)
        latest = (
            "(SELECT {col} FROM requests l WHERE l.session = g.session "
            "ORDER BY l.ts DESC, l.id DESC LIMIT 1)"
        )
        sql = f"""SELECT g.*, {latest.format(col="scope")} AS scope,
            {latest.format(col="client")} AS client FROM (
            SELECT session, MIN(ts) AS first_ts, MAX(ts) AS last_ts,
            SUM(state != 'blocked') AS requests, SUM(state = 'blocked') AS blocked,
            {sums}, SUM(cost_usd) AS cost, MAX(cost_estimated) AS est, MAX(plan) AS plan,
            GROUP_CONCAT(DISTINCT model) AS models
            FROM requests WHERE session IS NOT NULL AND {" AND ".join(where)}
            GROUP BY session) g ORDER BY g.last_ts DESC"""
        out = []
        for r in self._all(sql, args):
            out.append(
                SessionSummary(
                    id=r["session"],
                    scope=r["scope"],
                    first_ts=r["first_ts"],
                    last_ts=r["last_ts"],
                    requests=r["requests"],
                    blocked=r["blocked"],
                    usage=Usage(**{n: r[n] for n in USAGE_SUMS}),
                    cost_usd=r["cost"],
                    cost_estimated=bool(r["est"]),
                    plan=bool(r["plan"]),
                    models=sorted(r["models"].split(",")) if r["models"] else [],
                    client=r["client"],
                )
            )
        return out

    def aggregate(
        self,
        by: str,
        since: float | None = None,
        until: float | None = None,
        scope: str | None = None,
        session: str | None = None,
    ) -> list[Aggregate]:
        """Totals grouped by `by` (scope, session, model, day, agent, client, provider,
        request_class). 'day' is the local date (YYYY-MM-DD), sorted ascending; other
        groupings are sorted by cost, highest first."""
        if by not in AGGREGATE_KEYS:
            raise ValueError(f"cannot aggregate by {by!r}; use one of {', '.join(AGGREGATE_KEYS)}")
        key = AGGREGATE_KEYS[by]
        where, args = self._filters(since, until, scope, session)
        sums = ", ".join(f"SUM({n}) AS {n}" for n in USAGE_SUMS)
        order = "k ASC" if by == "day" else "cost DESC, k ASC"
        sql = f"""SELECT {key} AS k,
            SUM(state != 'blocked') AS requests, SUM(state = 'blocked') AS blocked,
            SUM(state = 'error') AS errors, {sums}, SUM(cost_usd) AS cost
            FROM requests WHERE {" AND ".join(where)} GROUP BY k ORDER BY {order}"""
        return [
            Aggregate(
                key=r["k"],
                requests=r["requests"],
                blocked=r["blocked"],
                errors=r["errors"],
                usage=Usage(**{n: r[n] for n in USAGE_SUMS}),
                cost_usd=r["cost"],
            )
            for r in self._all(sql, args)
        ]

    def prune(self, profiles_before: float | None, records_before: float | None) -> tuple[int, int]:
        """Delete profiles of requests older than `profiles_before` and ledger rows older than
        `records_before` (never PENDING rows). Returns (profiles deleted, records deleted)."""
        profiles = records = 0
        with self.immediate():
            if records_before is not None:
                old = "SELECT id FROM requests WHERE ts < ? AND state != 'pending'"
                profiles += self._conn.execute(
                    f"DELETE FROM profiles WHERE request_id IN ({old})", (records_before,)
                ).rowcount
                records = self._conn.execute(
                    "DELETE FROM requests WHERE ts < ? AND state != 'pending'", (records_before,)
                ).rowcount
            if profiles_before is not None:
                profiles += self._conn.execute(
                    "DELETE FROM profiles WHERE request_id IN "
                    "(SELECT id FROM requests WHERE ts < ?)",
                    (profiles_before,),
                ).rowcount
        return profiles, records

    def _filters(
        self, since: float | None, until: float | None, scope: str | None, session: str | None
    ) -> tuple[list[str], list[Any]]:
        where, args = ["1"], []
        for clause, value in (
            ("ts >= ?", since),
            ("ts < ?", until),
            ("scope = ?", scope),
            ("session = ?", session),
        ):
            if value is not None:
                where.append(clause)
                args.append(value)
        return where, args

    def _one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchone()

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchall()
