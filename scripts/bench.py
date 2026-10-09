"""Measure the latency skinflint adds, against the fake upstream from tests/fakes.py.

Runs the fake upstream and the proxy in their own processes, then sends the same requests
directly and through the proxy: time to first byte and total time for non-streamed and
streamed Anthropic requests (N each), then throughput with concurrent streams. Prints a
markdown table. Localhost only.

    uv run python scripts/bench.py [-n 200] [--concurrency 32]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fakes import FakeUpstream  # noqa: E402

from skinflint.config import Config  # noqa: E402
from skinflint.model import Provider  # noqa: E402

HEADERS = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
SMALL = {
    "model": "claude-haiku-4-5",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "hi"}],
}
FIXTURE = ROOT / "tests" / "fixtures" / "claude_code" / "first_turn.request.json"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve_fake(port: int, delay: float) -> None:
    fake = FakeUpstream()
    fake.record = False
    fake.delay = delay
    web.run_app(fake.app(), host="127.0.0.1", port=port, access_log=None, print=None)


def serve_proxy(port: int, upstream: str, db: str) -> None:
    from skinflint.proxy import serve

    upstreams = {Provider.ANTHROPIC: upstream, Provider.OPENAI: upstream}
    serve(Config(port=port, db_path=Path(db), upstreams=upstreams))


def spawn(mode: str, *args: str) -> tuple[subprocess.Popen, str]:
    """Run this script in a server mode in a child process; return it and its base URL."""
    port = free_port()
    proc = subprocess.Popen([sys.executable, __file__, mode, str(port), *args])
    deadline = time.time() + 20
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, f"http://127.0.0.1:{port}"
        except OSError:
            if time.time() > deadline or proc.poll() is not None:
                proc.kill()
                raise RuntimeError(f"{mode} did not start") from None
            time.sleep(0.05)


async def one(session: aiohttp.ClientSession, url: str, body: bytes) -> tuple[float, float]:
    """(ttfb, total) in seconds. TTFB is the first body chunk."""
    t0 = time.perf_counter()
    async with session.post(url + "/v1/messages", data=body, headers=HEADERS) as resp:
        first = None
        async for _ in resp.content.iter_any():
            if first is None:
                first = time.perf_counter()
        end = time.perf_counter()
        assert resp.status == 200, resp.status
    return (first or end) - t0, end - t0


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


async def latency(base: dict[str, str], body: bytes, n: int) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = {}
    async with aiohttp.ClientSession() as session:
        for name, url in base.items():
            for _ in range(10):  # warm up connections and caches
                await one(session, url, body)
            ttfb, total = [], []
            for _ in range(n):
                a, b = await one(session, url, body)
                ttfb.append(a)
                total.append(b)
            out[name] = {"ttfb": ttfb, "total": total}
    return out


async def throughput(url: str, body: bytes, concurrency: int, per_worker: int) -> float:
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:

        async def worker() -> None:
            for _ in range(per_worker):
                await one(session, url, body)

        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        return concurrency * per_worker / (time.perf_counter() - t0)


def servers(tmp: str, name: str, delay: float, procs: list[subprocess.Popen]) -> dict[str, str]:
    fake, fake_url = spawn("--serve-fake", str(delay))
    procs.append(fake)
    proxy, proxy_url = spawn("--serve-proxy", fake_url, str(Path(tmp) / f"{name}.db"))
    procs.append(proxy)
    return {"direct": fake_url, "proxy": proxy_url}


def stop(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        proc.terminate()
        proc.wait()
    procs.clear()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--serve-fake":
        return serve_fake(int(sys.argv[2]), float(sys.argv[3]))
    if len(sys.argv) > 1 and sys.argv[1] == "--serve-proxy":
        return serve_proxy(int(sys.argv[2]), sys.argv[3], sys.argv[4])
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-n", type=int, default=200, help="requests per measurement")
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--per-worker", type=int, default=10)
    args = ap.parse_args()

    big = json.loads(FIXTURE.read_text(encoding="utf-8"))
    size = FIXTURE.stat().st_size // 1024
    cases = [
        ("small, JSON", {**SMALL, "stream": False}),
        ("small, SSE", {**SMALL, "stream": True}),
        (f"Claude Code {size} KB, JSON", {**big, "stream": False}),
        (f"Claude Code {size} KB, SSE", {**big, "stream": True}),
    ]
    rows = []
    procs: list[subprocess.Popen] = []
    # Windows may hold the ledger open briefly after the proxy process exits.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        try:
            base = servers(tmp, "latency", 0.0, procs)
            for label, payload in cases:
                res = asyncio.run(latency(base, json.dumps(payload).encode(), args.n))
                for metric in ("ttfb", "total"):
                    d, p = res["direct"][metric], res["proxy"][metric]
                    overhead = [(pct(p, q) - pct(d, q)) * 1000 for q in (50, 95, 99)]
                    rows.append((label, metric, statistics.median(d) * 1000, *overhead))
            stop(procs)
            base = servers(tmp, "throughput", 0.001, procs)
            body = json.dumps({**SMALL, "stream": True}).encode()
            rps = {
                name: asyncio.run(throughput(url, body, args.concurrency, args.per_worker))
                for name, url in base.items()
            }
        finally:
            stop(procs)

    print(f"\nskinflint proxy overhead (N={args.n}, localhost, fake upstream)\n")
    print("| request | metric | direct p50 ms | overhead p50 ms | p95 ms | p99 ms |")
    print("|---|---|---:|---:|---:|---:|")
    for label, metric, direct, o50, o95, o99 in rows:
        print(f"| {label} | {metric} | {direct:.2f} | {o50:+.2f} | {o95:+.2f} | {o99:+.2f} |")
    total = args.concurrency * args.per_worker
    print(
        f"\nThroughput, {args.concurrency} concurrent SSE streams ({total} requests, "
        f"8 events each, 1 ms apart):"
    )
    print("\n| path | requests/s |\n|---|---:|")
    for name, value in rps.items():
        print(f"| {name} | {value:.0f} |")
    print(f"\nproxy/direct: {rps['proxy'] / rps['direct']:.0%}")


if __name__ == "__main__":
    main()
