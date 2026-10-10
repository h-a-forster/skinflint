"""Cap stress: N parallel `claude -p` agents share one $1 session cap; some are killed
mid-stream. Compares the proxy ledger with the costs Claude Code reports.

All agents send `x-skinflint-session: <round>` (via ANTHROPIC_CUSTOM_HEADERS), so the
per-session budget covers all of them. Output is stream-json with partial messages, which
names every message id, so ledger rows map back to agents. An agent marked for abort gets
SIGKILL after it has seen `--kill-after` content deltas of one streamed message.

    MEASURE_SCRATCH=... SKINFLINT_BIN=... python scripts/measure/capstress.py \
        --round r1 --agents 6 --kill 0 [--model claude-opus-5-5]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from common import ROOT, SCRATCH, Proxy, charge, claude_env, guard, log, rows, write_json

CONFIG = """
[[budget]]
name = "session-cap"
usd = {cap}
per = "session"
window = "total"
"""
TASK = (
    "Read every .py file under src/ one at a time with the Read tool. After reading each "
    "file, write a 200-word summary of it to notes/<file name>.md with the Write tool before "
    "reading the next file. Do not stop until every file has a summary."
)


def agent(
    i: int, binary: str, env: dict, work: Path, model: str, kill_after: int | None, out: dict
) -> None:
    started = time.time()
    proc = subprocess.Popen(
        [
            binary,
            "-p",
            TASK,
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--strict-mcp-config",
            "--allowedTools",
            "Read,Write,Edit,Glob,Grep",
            "--permission-mode",
            "acceptEdits",
        ],
        env=env,
        cwd=work,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    events, msg_ids, deltas, killed_at = [], [], 0, None
    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        events.append(ev)
        inner = ev.get("event", {}) if ev.get("type") == "stream_event" else {}
        kind = inner.get("type")
        if kind == "message_start":
            msg_ids.append(inner.get("message", {}).get("id"))
            deltas = 0
        elif kind == "content_block_delta":
            deltas += 1
            # Kill on the second message or later, so some cost has already settled.
            if kill_after is not None and len(msg_ids) >= 2 and deltas >= kill_after:
                killed_at = time.time() - started
                proc.send_signal(signal.SIGKILL)
                break
    proc.wait()
    stderr = proc.stderr.read() if proc.stderr else ""
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    out[i] = {
        "agent": i,
        "killed": killed_at is not None,
        "killed_at_s": killed_at,
        "returncode": proc.returncode,
        "wall_s": round(time.time() - started, 2),
        "msg_ids": msg_ids,
        "result": result,
        "stderr": stderr[-3000:],
        "n_events": len(events),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", required=True)
    ap.add_argument("--agents", type=int, default=6)
    ap.add_argument("--kill", type=int, default=0, help="how many agents to kill mid-stream")
    ap.add_argument("--kill-after", type=int, default=20, help="content deltas before SIGKILL")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--version", default="2.1.296")
    ap.add_argument("--cap", type=float, default=1.0, help="session cap in USD")
    ap.add_argument("--reserve", default="estimate", choices=["estimate", "worst_case"])
    a = ap.parse_args()
    # Worst case for a round: the cap plus every agent's in-flight request.
    guard(a.cap + a.agents * 0.6)
    binary = str(SCRATCH / "cc" / a.version / "node_modules" / ".bin" / "claude")
    base = SCRATCH / "capstress" / a.round
    shutil.rmtree(base, ignore_errors=True)
    env_extra = {"ANTHROPIC_CUSTOM_HEADERS": f"x-skinflint-session: {a.round}"}
    out: dict[int, dict] = {}
    config = f'[limits]\nreserve = "{a.reserve}"\n{CONFIG.format(cap=a.cap)}'
    with Proxy(base / "sf", config) as proxy:
        threads = []
        for i in range(a.agents):
            work = base / f"agent{i}"
            shutil.copytree(
                ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__")
            )
            (work / "notes").mkdir()
            kill_after = a.kill_after if i < a.kill else None
            t = threading.Thread(
                target=agent,
                args=(
                    i,
                    binary,
                    claude_env(proxy.url, **env_extra),
                    work,
                    a.model,
                    kill_after,
                    out,
                ),
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        time.sleep(2)  # let the proxy settle killed streams
        ledger = rows(proxy.db)
    reported = sum(float((o["result"] or {}).get("total_cost_usd") or 0) for o in out.values())
    ledger_ok = sum(r["cost_usd"] for r in ledger)
    for o in out.values():
        ids = set(filter(None, o["msg_ids"]))
        mine = [r for r in ledger if r["upstream_id"] in ids]
        o["ledger_usd"] = sum(r["cost_usd"] for r in mine)
        o["ledger_rows"] = [r["id"] for r in mine]
        usd = float((o["result"] or {}).get("total_cost_usd") or 0)
        # A killed agent reports nothing: charge the ledger's figure for its messages.
        charge(
            "capstress",
            usd if o["result"] else o["ledger_usd"],
            "total_cost_usd" if o["result"] else "ledger (killed)",
            tag=f"{a.round}/{o['agent']}",
        )
    unmatched = [
        r
        for r in ledger
        if r["upstream_id"] and not any(r["upstream_id"] in o["msg_ids"] for o in out.values())
    ]
    if unmatched:
        charge(
            "capstress",
            sum(r["cost_usd"] for r in unmatched),
            "ledger (unmatched rows)",
            tag=a.round,
        )
    summary = {
        "round": a.round,
        "agents": a.agents,
        "killed": a.kill,
        "model": a.model,
        "version": a.version,
        "cap_usd": a.cap,
        "reserve": a.reserve,
        "ledger_total_usd": ledger_ok,
        "reported_total_usd": reported,
        "blocked_rows": sum(1 for r in ledger if r["state"] == "blocked"),
        "estimated_rows": sum(1 for r in ledger if r["cost_estimated"]),
        "unmatched_rows": [r["id"] for r in unmatched],
    }
    write_json(f"capstress-{a.round}.json", {"summary": summary, "agents": out, "ledger": ledger})
    log(f"capstress {a.round}: {json.dumps(summary)}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
