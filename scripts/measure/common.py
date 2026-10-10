"""Shared helpers for the live measurements in measurements/: run the proxy, run nested
`claude -p` through it, export the ledger, and enforce a hard spend limit.

Every nested call's cost goes into measurements/<date>/spend.jsonl. A run is refused when the
recorded spend plus its expected cost would pass STOP_AT. Calls killed before they report a
cost are charged at the proxy ledger's figure for their session.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATE = os.environ.get("MEASURE_DATE", "2026-10-10")
OUT = ROOT / "measurements" / DATE
HARD_LIMIT = 30.0
STOP_AT = float(os.environ.get("MEASURE_STOP_AT", "28.0"))  # headroom for in-flight overshoot
SKINFLINT = os.environ.get("SKINFLINT_BIN", "skinflint")
SCRATCH = Path(os.environ.get("MEASURE_SCRATCH", "/tmp/skinflint-measure"))


class OverBudget(RuntimeError):
    pass


def spent() -> float:
    path = OUT / "spend.jsonl"
    if not path.exists():
        return 0.0
    return sum(json.loads(line)["usd"] for line in path.read_text().splitlines() if line)


def charge(experiment: str, usd: float, source: str, **extra: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "experiment": experiment, "usd": usd, "source": source, **extra}
    with (OUT / "spend.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")


def guard(expected: float) -> None:
    total = spent()
    if total + expected > STOP_AT:
        raise OverBudget(f"spent ${total:.4f}; ${expected:.2f} more would pass ${STOP_AT:.2f}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Proxy:
    """`skinflint serve` in its own process with its own ledger and optional config."""

    def __init__(self, home: Path, config: str | None = None, port: int | None = None):
        self.home = home
        self.port = port or free_port()
        home.mkdir(parents=True, exist_ok=True)
        if config is not None:
            (home / "config.toml").write_text(config)
        env = {**os.environ, "SKINFLINT_HOME": str(home)}
        self.log = (home / "serve.log").open("a")
        self.proc = subprocess.Popen(
            [SKINFLINT, "serve", "--port", str(self.port)],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"proxy did not start; see {home / 'serve.log'}")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def db(self) -> Path:
        return self.home / "skinflint.db"

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()

    def __enter__(self) -> Proxy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def rows(db: Path, where: str = "1", args: tuple = ()) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(f"SELECT * FROM requests WHERE {where} ORDER BY id", args)
        return [dict(r) for r in cur]
    finally:
        con.close()


def cli_json(home: Path, *args: str) -> object:
    env = {**os.environ, "SKINFLINT_HOME": str(home)}
    out = subprocess.run([SKINFLINT, *args, "--json"], env=env, capture_output=True, text=True)
    return json.loads(out.stdout) if out.stdout.strip() else None


def claude_env(base_url: str, **extra: str) -> dict[str, str]:
    """A minimal environment: PATH and HOME only, so nothing from this shell leaks in."""
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "ANTHROPIC_BASE_URL": base_url}
    env.update(extra)
    return env


def claude(
    binary: str,
    args: list[str],
    env: dict[str, str],
    cwd: Path,
    experiment: str,
    expected: float,
    timeout: float = 600,
    tag: str = "",
) -> dict:
    """Run `claude -p ... --output-format json`, charge its total_cost_usd, return the result."""
    guard(expected)
    started = time.time()
    proc = subprocess.run(
        [binary, "-p", *args, "--output-format", "json"],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = {"parse_error": True, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
    usd = float(result.get("total_cost_usd") or 0.0)
    charge(experiment, usd, "total_cost_usd", tag=tag, session=result.get("session_id"))
    result["_wall_s"] = round(time.time() - started, 2)
    result["_returncode"] = proc.returncode
    return result


def write_json(name: str, obj: object) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(obj, indent=1, default=str) + "\n")
    return path


def log(msg: str) -> None:
    print(
        f"[{time.strftime('%H:%M:%S')}] {msg} (spent ${spent():.4f})", file=sys.stderr, flush=True
    )
