"""Command-line interface. Heavy modules (aiohttp, the proxy) load only for serve and run."""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING, Any

from skinflint import __version__

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping

PROG = "skinflint"
SCOPE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:@+-")
SHELLS = ("sh", "powershell", "cmd", "fish")
BY = ("scope", "session", "model", "day", "agent", "client", "provider", "request_class")
UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# -- helpers --------------------------------------------------------------------------------


def parse_time(spec: str | None, now: float | None = None) -> float | None:
    """`30m`, `24h`, `7d`, `2w` (ago), `YYYY-MM-DD` (local midnight) or `all` (None)."""
    from datetime import datetime

    if spec is None:
        return None
    s = spec.strip().lower()
    if s == "all":
        return None
    now = time.time() if now is None else now
    if len(s) >= 2 and s[-1] in UNITS:
        try:
            n = float(s[:-1])
        except ValueError:
            n = -1
        if n >= 0:
            return now - n * UNITS[s[-1]]
    try:
        return datetime.strptime(s, "%Y-%m-%d").timestamp()
    except ValueError:
        raise CliError(
            f"bad time {spec!r}; use e.g. 30m, 24h, 7d, 2w, YYYY-MM-DD or all", code=2
        ) from None


def valid_scope(scope: str) -> str:
    if not 1 <= len(scope) <= 64 or not set(scope) <= SCOPE_CHARS:
        raise CliError(f"bad scope {scope!r}: use 1-64 characters from A-Z a-z 0-9 . _ : @ + -")
    return scope


def load_config(args: argparse.Namespace):
    from pathlib import Path

    from skinflint import config

    path = getattr(args, "config", None)
    try:
        return config.load(Path(path) if path else None)
    except config.ConfigError as e:
        raise CliError(str(e)) from None


def open_ledger(cfg, json_mode: bool = False):
    """Read-only Store, or None (after printing a hint) when there is no ledger yet.

    In --json mode an empty in-memory ledger stands in, so output keeps its shape.
    """
    from skinflint import report
    from skinflint.store import Store, StoreError

    if not cfg.db_path.exists():
        if json_mode:
            return Store(":memory:")
        print(report.NO_DATA)
        return None
    try:
        return Store.open_readonly(cfg.db_path)
    except (StoreError, OSError) as e:
        raise CliError(f"cannot open ledger: {e}") from None


def resolve_session(store, arg: str) -> str:
    sessions = store.sessions()
    if not sessions:
        raise CliError("no sessions recorded yet")
    if arg == "last":
        return sessions[0].id
    ids = [s.id for s in sessions]
    if arg in ids:
        return arg
    if len(arg) < 4:
        raise CliError(f"session {arg!r}: give at least 4 characters of the id")
    matches = [i for i in ids if i.startswith(arg)]
    if not matches:
        raise CliError(f"no session matches {arg!r}")
    if len(matches) > 1:
        shown = ", ".join(matches[:10]) + (" ..." if len(matches) > 10 else "")
        raise CliError(f"session prefix {arg!r} is ambiguous: {shown}")
    return matches[0]


def resolve_record(store, arg: str):
    from skinflint.model import State

    if arg == "last":
        recent = store.records(states=[State.OK], limit=200, order="desc")
        if not recent:
            raise CliError("no completed requests recorded yet")
        return next((r for r in recent if store.segments(r.id)), recent[0])
    try:
        rid = int(arg.lstrip("#"))
    except ValueError:
        raise CliError(f"bad request id {arg!r}: use a number or 'last'") from None
    rec = store.get(rid)
    if rec is None:
        raise CliError(f"no request #{rid}")
    return rec


def detect_shell(env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    shell = env.get("SHELL", "")
    if shell.endswith("fish"):
        return "fish"
    if os.name != "nt":
        return "sh"
    if shell:
        return "sh"
    ps = env.get("PSModulePath", "")
    user_modules = ("\\windowspowershell\\modules", "\\powershell\\modules")
    parts = [p.lower().rstrip("\\") for p in ps.split(";")]
    if any("\\documents\\" in p and p.endswith(user_modules) for p in parts):
        return "powershell"
    return "cmd"


def env_lines(base: str, scope: str | None, shell: str) -> list[str]:
    root = f"{base}/s/{scope}" if scope else base
    values = [("ANTHROPIC_BASE_URL", root), ("OPENAI_BASE_URL", f"{root}/v1")]
    if shell == "powershell":
        return [f'$env:{k} = "{v}"' for k, v in values]
    if shell == "cmd":
        return [f"set {k}={v}" for k, v in values]
    if shell == "fish":
        return [f"set -gx {k} {v}" for k, v in values]
    return [f"export {k}={v}" for k, v in values]


def base_url(host: str, port: int) -> str:
    if host in ("0.0.0.0", "", "*"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def dump(obj: Any) -> None:
    import json

    print(json.dumps(obj, indent=2, default=str))


def setup_logging(level: int) -> None:
    import logging

    logger = logging.getLogger("skinflint")
    for h in list(logger.handlers):
        if getattr(h, "_skinflint_cli", False):
            logger.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    handler._skinflint_cli = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def check_port(host: str, port: int) -> None:
    import errno
    import socket

    if port == 0:
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise CliError(f"cannot resolve host {host!r}: {e}") from None
    family, kind, proto, _, addr = infos[0]
    sock = socket.socket(family, kind, proto)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(addr)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(e, "winerror", None) in (
            10048,
            10013,
        ):
            raise CliError(
                f"port {port} on {host} is already in use; stop the other process or pass --port"
            ) from None
        raise CliError(f"cannot listen on {host}:{port}: {e}") from None
    finally:
        sock.close()


# -- commands -------------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    import logging

    from skinflint import report

    cfg = load_config(args)
    if args.host:
        cfg.host = args.host
    if args.port is not None:
        cfg.port = args.port
    level = logging.WARNING if args.quiet else logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level)
    check_port(cfg.host, cfg.port)
    warning = None
    if not cfg.is_loopback:
        warning = f"{cfg.host} is not a loopback address: anyone who can reach it can spend"
    url = base_url(cfg.host, cfg.port)
    print(
        report.banner(
            __version__,
            url,
            str(cfg.source) if cfg.source else None,
            len(cfg.budgets),
            str(cfg.db_path),
            env_lines(url, None, detect_shell()),
            warning,
        ),
        file=sys.stderr,
        flush=True,
    )
    from skinflint import proxy

    try:
        proxy.serve(cfg)
    except OSError as e:
        raise CliError(f"cannot listen on {cfg.host}:{cfg.port}: {e}") from None
    return 0


def _resolve_command(cmd: list[str]) -> list[str]:
    import shutil

    found = shutil.which(cmd[0])
    if found is None:
        raise CliError(f"command not found: {cmd[0]}", code=127)
    return [found, *cmd[1:]]


def child_env(environ: Mapping[str, str], base: str) -> dict[str, str]:
    """Environment for a `run` child: base URLs pointed at the proxy."""
    env = dict(environ)
    # uv's launcher sets PYTHONHOME for skinflint's own interpreter; a Python child of a
    # different version would load the wrong standard library.
    home = env.get("PYTHONHOME")
    if home and os.path.normcase(home) == os.path.normcase(sys.base_prefix):
        del env["PYTHONHOME"]
    env["ANTHROPIC_BASE_URL"] = base
    env["OPENAI_BASE_URL"] = f"{base}/v1"
    # Claude Code turns tool search off behind a custom ANTHROPIC_BASE_URL and then sends
    # every tool definition in full (~15k more prompt tokens per request); keep it on.
    env.setdefault("ENABLE_TOOL_SEARCH", "true")
    return env


def run_child(cmd: list[str], env: dict[str, str]) -> int:
    """Run `cmd` with inherited stdio; the terminal's Ctrl+C reaches it directly."""
    import signal
    import subprocess

    proc = subprocess.Popen(_resolve_command(cmd), env=env)
    forwarded = [s for s in ("SIGTERM", "SIGHUP") if hasattr(signal, s) and os.name != "nt"]
    old: dict[int, Any] = {}

    def forward(signum, _frame):
        import contextlib

        with contextlib.suppress(OSError):
            proc.send_signal(signum)

    try:
        old[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
        for name in forwarded:
            sig = getattr(signal, name)
            old[sig] = signal.signal(sig, forward)
    except ValueError:
        pass  # not the main thread
    try:
        while True:
            try:
                code = proc.wait()
                break
            except KeyboardInterrupt:
                continue
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return 128 - code if code < 0 else code


def cmd_run(args: argparse.Namespace) -> int:
    import dataclasses
    import logging
    from datetime import datetime

    from skinflint import report
    from skinflint.model import BudgetRule, Per, State, Window
    from skinflint.store import Store

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise CliError("run: give a command after --, e.g. skinflint run -- claude", code=2)
    cfg = load_config(args)
    scope = valid_scope(args.scope or f"run-{datetime.now():%Y%m%d-%H%M%S}")
    rules = list(cfg.budgets)
    if args.cap is not None:
        if args.cap < 0:
            raise CliError("--cap must be non-negative", code=2)
        rules = [r for r in rules if r.name != "run cap"]
        rules.append(
            BudgetRule(
                name="run cap",
                usd=args.cap,
                per=Per.SCOPE,
                window=Window.TOTAL,
                scope=scope,
                hint="Raise --cap to allow more.",
            )
        )
    host = cfg.host if cfg.is_loopback else "127.0.0.1"
    run_cfg = dataclasses.replace(cfg, host=host, port=0, budgets=rules)
    setup_logging(logging.INFO if args.verbose else logging.WARNING)
    _resolve_command(cmd)
    from skinflint import proxy

    start = time.time()
    with proxy.embedded(run_cfg) as base:
        base = base.rstrip("/")
        code = run_child(cmd, child_env(os.environ, f"{base}/s/{scope}"))
    try:
        with Store.open_readonly(cfg.db_path) as store:
            rows = store.records(since=start, scope=scope)
    except Exception as e:  # noqa: BLE001
        print(f"skinflint: could not read the ledger: {e}", file=sys.stderr)
        return code
    done = [r for r in rows if r.state != State.BLOCKED]
    print(
        report.run_summary(
            scope,
            len(done),
            report.sum_usage(r.usage for r in done),
            sum(r.cost_usd for r in done),
            any(r.cost_estimated for r in done),
            len(rows) - len(done),
            args.cap,
        ),
        file=sys.stderr,
    )
    return code


def cmd_env(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    scope = valid_scope(args.scope) if args.scope else None
    for line in env_lines(base_url(cfg.host, cfg.port), scope, args.shell or detect_shell()):
        print(line)
    return 0


def _report_key(record, by: str) -> str | None:
    from datetime import datetime

    if by == "day":
        return datetime.fromtimestamp(record.ts).date().isoformat()
    value = getattr(record, by)
    return None if value is None else str(value)


def cmd_report(args: argparse.Namespace) -> int:
    from skinflint import report

    cfg = load_config(args)
    now = time.time()
    since = parse_time(args.since, now)
    until = parse_time(args.until, now)
    store = open_ledger(cfg, args.json)
    if store is None:
        return 0
    with store:
        session = resolve_session(store, args.session) if args.session else None
        aggs = store.aggregate(args.by, since, until, args.scope, session)
        flags: dict[str | None, list[bool]] = {}
        for r in store.records(since, until, args.scope, session):
            f = flags.setdefault(_report_key(r, args.by), [False, False])
            f[0] = f[0] or r.cost_estimated
            f[1] = f[1] or r.plan
    rows = [
        report.ReportRow(
            a.key,
            a.requests,
            a.blocked,
            a.errors,
            a.usage,
            a.cost_usd,
            *flags.get(a.key, [False, False]),
        )
        for a in aggs
    ]
    filters = {k: v for k, v in (("scope", args.scope), ("session", session)) if v}
    rep = report.Report(args.by, since, until, rows, report.total_row(rows), filters)
    if args.json:
        dump(rep.to_dict())
    else:
        print(report.render_report(rep))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from skinflint import report

    cfg = load_config(args)
    since = parse_time(args.since)
    store = open_ledger(cfg, args.json)
    if store is None:
        return 0
    with store:
        sessions = store.sessions(since)[: max(args.limit, 0)]
    if args.json:
        dump([s.to_dict() for s in sessions])
    else:
        print(report.render_sessions(sessions))
    return 0


def _pricing(cfg):
    from skinflint.pricing import Pricing

    return Pricing.load(cfg.prices, unknown_model=cfg.unknown_model)


def _record_dict(rec) -> dict[str, Any]:
    from dataclasses import asdict

    d = asdict(rec)
    d["usage"]["prompt_tokens"] = rec.usage.prompt_tokens
    return d


def cmd_profile(args: argparse.Namespace) -> int:
    from skinflint import profile, report

    cfg = load_config(args)
    if args.session and args.id:
        raise CliError("give a request id or --session, not both", code=2)
    store = open_ledger(cfg, args.json)
    if store is None:
        return 0
    pricing = _pricing(cfg)

    def price_for(r):
        return pricing.lookup(r.provider, r.model)[0]

    with store:
        if args.session:
            sid = resolve_session(store, args.session)
            items = [(r, store.segments(r.id)) for r in store.records(session=sid)]
            prof = profile.session_profile(items, price_for)
            if args.json:
                dump({"session": sid, "profile": prof.to_dict()})
            else:
                print(report.render_session_profile(sid, prof))
            return 0
        rec = resolve_record(store, args.id or "last")
        segs = store.segments(rec.id)
    if not segs:
        raise CliError(f"request #{rec.id} has no stored profile (blocked, unmetered or pruned)")
    prof = profile.request_profile(segs, rec.usage, price_for(rec))
    if args.json:
        dump({"record": _record_dict(rec), "profile": prof.to_dict()})
    else:
        print(report.render_request_profile(rec, prof))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from skinflint import profile, report

    cfg = load_config(args)
    store = open_ledger(cfg)
    if store is None:
        return 0
    with store:
        a = resolve_record(store, args.a)
        b = resolve_record(store, args.b)
        sa, sb = store.segments(a.id), store.segments(b.id)
    for rec, segs in ((a, sa), (b, sb)):
        if segs is None:
            raise CliError(f"request #{rec.id} has no stored profile")
    d = profile.diff(sa, sb)
    if args.json:
        dump({"a": a.id, "b": b.id, "diff": d.to_dict()})
    else:
        print(report.render_diff(a, b, d))
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    from skinflint import cachedoctor, report

    cfg = load_config(args)
    store = open_ledger(cfg, args.json)
    if store is None:
        return 0
    pricing = _pricing(cfg)
    with store:
        if args.json and not store.sessions():
            dump({"session": None, "events": [], "summary": cachedoctor.summarize([]).to_dict()})
            return 0
        sid = resolve_session(store, args.session or "last")
        items = [(r, store.segments(r.id)) for r in store.records(session=sid)]
    events = cachedoctor.analyze(items, lambda r: pricing.lookup(r.provider, r.model)[0])
    summary = cachedoctor.summarize(events)
    if args.json:
        dump(
            {
                "session": sid,
                "events": [e.to_dict() for e in events],
                "summary": summary.to_dict(),
            }
        )
    else:
        print(report.render_cache(sid, events, summary))
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    from skinflint import report
    from skinflint.budget import Budget

    cfg = load_config(args)
    if not cfg.budgets:
        if args.json:
            dump([])
        else:
            where = cfg.source or "no config file"
            print(f"no budgets configured ({where}); see `skinflint init`")
        return 0
    store = open_ledger(cfg, args.json)
    if store is None:
        return 0
    with store:
        session = resolve_session(store, args.session) if args.session else None
        statuses = Budget(cfg.budgets, store, None).status(scope=args.scope, session=session)
    if args.json:
        dump([s.to_dict() for s in statuses])
    else:
        print(report.render_budget(statuses))
    return 0


def resolve_price(pricing, query: str, provider=None):
    """(provider, model id, how it matched, price) for a model name."""
    from skinflint.model import Provider

    if provider is not None:
        order = [Provider(provider)]
    elif "claude" in query.lower():
        order = [Provider.ANTHROPIC, Provider.OPENAI]
    else:
        order = [Provider.OPENAI, Provider.ANTHROPIC]
    fallback = None
    for p in order:
        price, exact = pricing.lookup(p, query)
        if price is None:
            continue
        model_id = next(m for pp, m, pr in pricing.models() if pp == p and pr is price)
        if exact:
            if model_id == query:
                how = "exact"
            elif query in pricing.aliases(p):
                how = f"alias of {model_id}"
            else:
                how = f"normalised to {model_id}"
            return p, model_id, how, price
        name = query.lower().rsplit("/", 1)[-1]
        if name.startswith(model_id):
            return p, model_id, f"longest known prefix {model_id}, estimated", price
        if fallback is None:
            how = f"unknown model: priced as {model_id}, the {p} flagship, estimated"
            fallback = (p, model_id, how, price)
    if fallback is None:
        raise CliError(f'no price for {query!r} (limits.unknown_model = "block")')
    return fallback


def cmd_prices(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from skinflint import report

    cfg = load_config(args)
    pricing = _pricing(cfg)
    if args.model:
        p, model_id, how, price = resolve_price(pricing, args.model, args.provider)
        if args.json:
            dump(
                {
                    "query": args.model,
                    "provider": str(p),
                    "model": model_id,
                    "match": how,
                    "as_of": pricing.as_of,
                    "price": asdict(price),
                }
            )
        else:
            print(report.render_price(args.model, p, model_id, how, price))
        return 0
    models = [m for m in pricing.models() if args.provider in (None, str(m[0]))]
    if args.json:
        dump(
            {
                "as_of": pricing.as_of,
                "models": [
                    {"provider": str(p), "model": m, **asdict(price)} for p, m, price in models
                ],
            }
        )
    else:
        print(report.render_prices(models, pricing.as_of))
    return 0


def cmd_statusline(args: argparse.Namespace) -> int:
    from skinflint import statusline

    return statusline.main()


def cmd_init(args: argparse.Namespace) -> int:
    from pathlib import Path

    from skinflint import config

    path = Path(args.config) if args.config else config.default_config_path()
    if path.exists() and not args.force:
        raise CliError(f"{path} already exists; pass --force to overwrite it")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config.DEFAULT_CONFIG, encoding="utf-8")
    except OSError as e:
        raise CliError(f"cannot write {path}: {e}") from None
    print(path)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from skinflint import report
    from skinflint.store import Store, StoreError

    cfg = load_config(args)
    days = cfg.keep_profiles_days if args.profiles is None else args.profiles
    if days < 0 or (args.records is not None and args.records < 0):
        raise CliError("days must be non-negative", code=2)
    if not cfg.db_path.exists():
        print(report.NO_DATA)
        return 0
    now = time.time()
    profiles_before = now - days * 86400 if days else None
    records_before = now - args.records * 86400 if args.records else None
    try:
        with Store(cfg.db_path) as store:
            profiles, records = store.prune(profiles_before, records_before)
    except StoreError as e:
        raise CliError(str(e)) from None
    keep = f"older than {days}d" if days else "none: kept forever"
    rec_note = f"older than {args.records}d" if args.records else "kept"
    print(f"removed {profiles} profiles ({keep}), {records} records ({rec_note})")
    return 0


# -- parser ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    import argparse

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", help="config file (default: $SKINFLINT_HOME)")
    js = argparse.ArgumentParser(add_help=False)
    js.add_argument("--json", action="store_true", help="machine-readable output")

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Local proxy for the Anthropic and OpenAI APIs: spend caps, a request "
        "ledger, context breakdown and prompt-cache diagnosis.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("serve", parents=[common], help="run the proxy")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    g = p.add_mutually_exclusive_group()
    g.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    g.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "run",
        parents=[common],
        help="run a command through a private proxy",
        usage=f"{PROG} run [--scope NAME] [--cap USD] [--config P] [-v] -- CMD [ARGS...]",
    )
    p.add_argument("--scope", help="scope label (default: run-YYYYMMDD-HHMMSS)")
    p.add_argument("--cap", type=float, metavar="USD", help="spend cap for this run")
    p.add_argument("-v", "--verbose", action="store_true", help="log every request")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="command to run")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("env", parents=[common], help="print lines that point agents at the proxy")
    p.add_argument("--scope")
    p.add_argument("--shell", choices=SHELLS)
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("report", parents=[common, js], help="spend and tokens, grouped")
    p.add_argument("--since", default="24h", help="30m, 24h, 7d, 2w, YYYY-MM-DD or all")
    p.add_argument("--until")
    p.add_argument("--by", choices=BY, default="model")
    p.add_argument("--scope")
    p.add_argument("--session", metavar="ID")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("sessions", parents=[common, js], help="recent agent sessions")
    p.add_argument("--since", default="7d")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("profile", parents=[common, js], help="where prompt tokens go")
    p.add_argument("id", nargs="?", metavar="ID|last", help="request id (default: last)")
    p.add_argument("--session", metavar="ID|last")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("diff", parents=[common, js], help="compare two requests' context")
    p.add_argument("a", metavar="A")
    p.add_argument("b", metavar="B")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("cache", parents=[common, js], help="prompt-cache hits and misses")
    p.add_argument("--session", metavar="ID|last")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("budget", parents=[common, js], help="spend against each budget")
    p.add_argument("--scope")
    p.add_argument("--session", metavar="ID|last")
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("prices", parents=[common, js], help="model prices")
    p.add_argument("model", nargs="?", metavar="MODEL")
    p.add_argument("--provider", choices=("anthropic", "openai"))
    p.set_defaults(func=cmd_prices)

    p = sub.add_parser("statusline", help="one line for Claude Code's statusLine")
    p.set_defaults(func=cmd_statusline)

    p = sub.add_parser("init", parents=[common], help="write a starter config file")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("prune", parents=[common], help="delete old profiles or records")
    p.add_argument("--profiles", type=int, metavar="DAYS")
    p.add_argument("--records", type=int, metavar="DAYS")
    p.set_defaults(func=cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv == ["statusline"]:
        from skinflint import statusline

        return statusline.main()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except CliError as e:
        print(f"{PROG}: error: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except Exception as e:  # noqa: BLE001
        if os.environ.get("SKINFLINT_DEBUG"):
            raise
        print(f"{PROG}: error: {e}", file=sys.stderr)
        return 1
