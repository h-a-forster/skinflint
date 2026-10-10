"""Cache breakers: what one change between two turns of a Claude Code session costs, and
whether skinflint's local verdict matches the API's own cache diagnostics.

Each trial runs turn 1 with `claude -p`, then turn 2 with `--resume` and one change, both
through the same fresh proxy. The proxy adds `diagnostics.previous_message_id`, so the API
reports its own cache-miss reason next to skinflint's verdict.

    MEASURE_SCRATCH=... SKINFLINT_BIN=... python scripts/measure/cachebreak.py \
        [--breakers control,tools,...] [--repeats 3]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from common import SCRATCH, Proxy, claude, claude_env, cli_json, log, rows, write_json

MODEL = "claude-haiku-4-5"
TTL_MODEL = "claude-sonnet-4-6"  # used by nothing else here, so no other run keeps its cache warm
IDLE_S = 330  # past the 5-minute TTL
PROMPT1 = "Reply with exactly: one"
PROMPT2 = "Reply with exactly: two"


def mcp_config(path: Path) -> Path:
    bins = SCRATCH / "mcp" / "node_modules" / ".bin"
    cfg = {"mcpServers": {"memory": {"command": str(bins / "mcp-server-memory"), "args": []}}}
    path.write_text(json.dumps(cfg))
    return path


def git(work: Path, *args: str) -> None:
    ident = ["-c", "user.name=m", "-c", "user.email=m@example.com"]
    subprocess.run(["git", *ident, *args], cwd=work, check=True)


def plan(breaker: str, base: Path, nonce: str) -> tuple[list[str], list[str], dict, dict]:
    """(turn-1 args, turn-2 args, turn-1 env, turn-2 env) for one breaker."""
    common = ["--strict-mcp-config"]
    sp = ["--append-system-prompt", f"Trial {nonce}."]
    a1 = [*common, *sp, "--model", MODEL]
    a2 = list(a1)
    e1: dict[str, str] = {}
    e2: dict[str, str] = {}
    if breaker == "tools":
        a2 += ["--disallowedTools", "NotebookEdit"]
    elif breaker == "system":
        a2 = [*common, "--append-system-prompt", f"Trial {nonce}. Codename beta.", "--model", MODEL]
    elif breaker == "model":
        a2 = [*common, *sp, "--model", "claude-haiku-5-5"]
    elif breaker == "mcp":
        a2 += ["--mcp-config", str(mcp_config(base / "mcp.json"))]
    elif breaker == "system_full":
        a2 = [
            *common,
            "--system-prompt",
            f"You are a terse assistant. Trial {nonce}.",
            "--model",
            MODEL,
        ]
    elif breaker in ("claude_md", "git"):
        pass  # files change between turns, see run()
    elif breaker == "ttl":
        a1 = [*common, *sp, "--model", TTL_MODEL]
        a2 = list(a1)
        e1 = e2 = {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}
    elif breaker != "control":
        raise ValueError(breaker)
    return a1, a2, e1, e2


def run(breaker: str, rep: int, binary: str, out: list) -> None:
    cell = f"{breaker}-{rep}"
    base = SCRATCH / "cachebreak" / cell
    shutil.rmtree(base, ignore_errors=True)
    work = base / "work"
    work.mkdir(parents=True)
    nonce = uuid.uuid4().hex[:8]
    a1, a2, e1, e2 = plan(breaker, base, nonce)
    if breaker == "claude_md":
        (work / "CLAUDE.md").write_text("Use British spelling.\n")
    if breaker == "git":
        git(work, "init", "-q")
        (work / "a.txt").write_text("a\n")
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", "first")
    with Proxy(base / "sf") as proxy:
        r1 = claude(
            binary,
            [PROMPT1, *a1],
            claude_env(proxy.url, **e1),
            work,
            "cachebreak",
            expected=0.3,
            timeout=300,
            tag=f"{cell}/1",
        )
        sid = r1.get("session_id")
        if breaker == "claude_md":
            (work / "CLAUDE.md").write_text("Use British spelling. Prefer short sentences.\n")
        if breaker == "git":
            (work / "b.txt").write_text("b\n")
            git(work, "add", ".")
            git(work, "commit", "-q", "-m", "second")
        if breaker == "ttl":
            time.sleep(IDLE_S)
        r2 = claude(
            binary,
            [PROMPT2, "--resume", str(sid), *a2],
            claude_env(proxy.url, **e2),
            work,
            "cachebreak",
            expected=0.3,
            timeout=300,
            tag=f"{cell}/2",
        )
        ledger = rows(proxy.db)
        cache = cli_json(proxy.home, "cache", "--session", str(r2.get("session_id") or sid))
    keep = ("total_cost_usd", "usage", "modelUsage", "session_id", "is_error", "result", "_wall_s")
    out.append(
        {
            "cell": cell,
            "breaker": breaker,
            "repeat": rep,
            "nonce": nonce,
            "turn1": {k: r1.get(k) for k in keep},
            "turn2": {k: r2.get(k) for k in keep},
            "ledger": ledger,
            "cache": cache,
        }
    )
    log(f"cachebreak {cell}: turn2 ${r2.get('total_cost_usd')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--breakers", default="control,tools,system,model,mcp,claude_md,ttl")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--version", default="2.1.296")
    ap.add_argument("--out", default="cachebreak.json")
    a = ap.parse_args()
    binary = str(SCRATCH / "cc" / a.version / "node_modules" / ".bin" / "claude")
    out: list = []
    breakers = a.breakers.split(",")
    # The TTL trials idle for minutes; run them alongside the rest.
    threads = [
        threading.Thread(target=lambda r=r: run("ttl", r, binary, out))
        for r in range(a.repeats)
        if "ttl" in breakers
    ]
    for t in threads:
        t.start()
    for breaker in (b for b in breakers if b != "ttl"):
        for rep in range(a.repeats):
            run(breaker, rep, binary, out)
            write_json(a.out, sorted(out, key=lambda o: o["cell"]))
    for t in threads:
        t.join()
    write_json(a.out, sorted(out, key=lambda o: o["cell"]))


if __name__ == "__main__":
    main()
