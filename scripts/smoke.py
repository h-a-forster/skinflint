"""End-to-end smoke test of an installed skinflint against a fake Anthropic upstream.

Starts a fake upstream (aiohttp, streaming SSE with usage), runs `skinflint serve` as a
subprocess with a tiny spend cap, sends requests until the cap refuses one, then checks the
JSON output of report, sessions, profile, cache and budget. Runs on Linux, macOS and Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from aiohttp import web

MODEL = "smoke-model"
SESSION = "5m0ke000-0000-4000-8000-000000000001"
CAP = 0.02
# USD per MTok, set in the config so the numbers do not depend on the bundled price table.
RATES = {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5, "cache_read": 0.1}
UNCACHED, CACHED, OUTPUT = 200, 800, 500


def expected_cost(first: bool) -> float:
    cache = RATES["cache_write_5m"] if first else RATES["cache_read"]
    return (UNCACHED * RATES["input"] + CACHED * cache + OUTPUT * RATES["output"]) / 1e6


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class FakeUpstream:
    def __init__(self) -> None:
        self.calls = 0
        self.port = 0
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def messages(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        first = self.calls == 0
        self.calls += 1
        usage = {
            "input_tokens": UNCACHED,
            "cache_creation_input_tokens": CACHED if first else 0,
            "cache_read_input_tokens": 0 if first else CACHED,
            "cache_creation": {
                "ephemeral_5m_input_tokens": CACHED if first else 0,
                "ephemeral_1h_input_tokens": 0,
            },
            "output_tokens": 1,
        }
        resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        message = {
            "id": f"msg_smoke{self.calls}",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "usage": usage,
        }
        await resp.write(sse("message_start", {"type": "message_start", "message": message}))
        block = {"type": "text", "text": ""}
        await resp.write(
            sse(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": block},
            )
        )
        delta = {"type": "text_delta", "text": "hi"}
        await resp.write(
            sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        )
        await resp.write(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        final = {**usage, "output_tokens": OUTPUT}
        await resp.write(
            sse(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": final},
            )
        )
        await resp.write(sse("message_stop", {"type": "message_stop"}))
        await resp.write_eof()
        return resp

    async def _main(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/messages", self.messages)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = runner.addresses[0][1]
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._ready.set()
        await self._stop.wait()
        await runner.cleanup()

    def _run(self) -> None:
        asyncio.run(self._main())

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("fake upstream did not start")

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def skinflint_cmd() -> list[str]:
    exe = shutil.which("skinflint")
    return [exe] if exe else [sys.executable, "-m", "skinflint"]


def cli(env: dict[str, str], *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        [*skinflint_cmd(), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"skinflint {' '.join(args)} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout


def cli_json(env: dict[str, str], *args: str):
    return json.loads(cli(env, *args, "--json"))


def post(base: str, n: int) -> tuple[int, dict[str, str], bytes]:
    body = {
        "model": MODEL,
        "max_tokens": 64,
        "stream": True,
        "system": [
            {
                "type": "text",
                "text": "You are a smoke test. " * 40,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file. " * 20,
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        "messages": [{"role": "user", "content": f"request {n}"}],
    }
    req = urllib.request.Request(
        base + "/v1/messages",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": "sk-ant-smoke",
            "x-claude-code-session-id": SESSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def wait_healthy(base: str, proc: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"skinflint serve exited early with {proc.returncode}")
        try:
            with urllib.request.urlopen(base + "/_skinflint/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("skinflint serve did not become healthy")


def close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def run(tmp: Path) -> None:
    upstream = FakeUpstream()
    upstream.start()
    port = free_port()
    config = tmp / "smoke.toml"
    config.write_text(
        f"""
[server]
host = "127.0.0.1"
port = {port}

[upstream]
anthropic = "http://127.0.0.1:{upstream.port}"

[storage]
path = "smoke.db"

[[budget]]
name = "tiny"
usd = {CAP}
window = "total"

[prices."{MODEL}"]
provider = "anthropic"
input = {RATES["input"]}
output = {RATES["output"]}
cache_write_5m = {RATES["cache_write_5m"]}
cache_read = {RATES["cache_read"]}
""",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.setdefault("SKINFLINT_HOME", str(tmp / "home"))
    env["SKINFLINT_CONFIG"] = str(config)
    env["PYTHONIOENCODING"] = "utf-8"
    base = f"http://127.0.0.1:{port}"
    log = open(tmp / "serve.log", "w+", encoding="utf-8")  # noqa: SIM115
    server = subprocess.Popen(
        [*skinflint_cmd(), "serve"], env=env, stdout=log, stderr=subprocess.STDOUT
    )
    try:
        wait_healthy(base, server)
        ok = 0
        for n in range(20):
            status, headers, body = post(base, n)
            if status == 200:
                assert b"message_stop" in body, body[:200]
                ok += 1
                time.sleep(0.3)  # let the proxy settle the row before the next budget check
                continue
            assert status == 402, (status, body[:300])
            lower = {k.lower(): v for k, v in headers.items()}
            assert lower.get("x-should-retry") == "false", headers
            err = json.loads(body)
            assert err["error"]["type"] == "billing_error", err
            assert "tiny" in err["error"]["message"], err
            break
        else:
            raise AssertionError("the cap never blocked a request")
        assert ok >= 2, f"only {ok} requests got through before the cap"
        assert upstream.calls == ok, (upstream.calls, ok)
        cost = expected_cost(True) + (ok - 1) * expected_cost(False)
        print(f"smoke: {ok} requests passed, then 402 (expected cost ${cost:.6f})")

        deadline = time.time() + 10
        while True:
            rep = cli_json(env, "report", "--since", "all")
            if rep["total"]["requests"] == ok or time.time() > deadline:
                break
            time.sleep(0.2)
        total = rep["total"]
        assert total["requests"] == ok, total
        assert total["blocked"] == 1, total
        assert total["prompt_tokens"] == ok * (UNCACHED + CACHED), total
        assert total["cache_read"] == (ok - 1) * CACHED, total
        assert total["output_tokens"] == ok * OUTPUT, total
        assert close(total["cost_usd"], cost), (total["cost_usd"], cost)
        assert [r["key"] for r in rep["rows"]] == [MODEL], rep["rows"]

        sessions = cli_json(env, "sessions", "--since", "all")
        assert len(sessions) == 1 and sessions[0]["id"] == SESSION, sessions
        assert sessions[0]["requests"] == ok and sessions[0]["blocked"] == 1, sessions[0]

        prof = cli_json(env, "profile", "last")
        assert prof["record"]["model"] == MODEL, prof["record"]
        assert prof["profile"]["total_tokens"] == UNCACHED + CACHED, prof["profile"]
        groups = {g["group"] for g in prof["profile"]["groups"]}
        assert groups, prof["profile"]

        sprof = cli_json(env, "profile", "--session", "last")
        assert sprof["session"] == SESSION and sprof["profile"]["requests"] == ok, sprof

        cache = cli_json(env, "cache")
        assert cache["session"] == SESSION, cache
        assert len(cache["events"]) == ok, cache["events"]
        assert cache["summary"]["requests"] == ok, cache["summary"]
        assert cache["events"][-1]["cache_read"] == CACHED, cache["events"][-1]

        budget = cli_json(env, "budget")
        assert len(budget) == 1 and budget[0]["rule"]["name"] == "tiny", budget
        assert close(budget[0]["spend"]["usd"], cost), (budget[0]["spend"], cost)
        assert budget[0]["remaining"]["usd"] == max(CAP - cost, 0), budget[0]

        line = cli(env, "statusline", stdin=json.dumps({"session_id": SESSION}))
        assert line.startswith("skinflint $"), line
        for text_cmd in (["report"], ["sessions"], ["profile"], ["cache"], ["budget"]):
            out = cli(env, *text_cmd)
            assert out.strip(), text_cmd
        print("smoke: report, sessions, profile, cache, budget and statusline OK")
        print(line.strip().encode("ascii", "replace").decode())
    except BaseException:
        log.flush()
        log.seek(0)
        sys.stderr.write("--- skinflint serve output ---\n" + log.read())
        raise
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(10)
        log.close()
        upstream.stop()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="skinflint-smoke-"))
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("smoke: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
