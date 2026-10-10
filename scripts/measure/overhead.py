"""Fixed context overhead of Claude Code: the prompt it sends for a one-word answer.

Matrix: Claude Code versions x MCP servers (none / four local stdio servers) x tool search
(Claude Code's default behind a custom ANTHROPIC_BASE_URL / ENABLE_TOOL_SEARCH=true).
Each cell runs `claude -p "Reply with exactly: hi"` in an empty directory with a minimal
environment, through a fresh proxy ledger, and records the largest request's profile.

    MEASURE_SCRATCH=... SKINFLINT_BIN=... CC_ROOT=... MCP_ROOT=... \
        python scripts/measure/overhead.py [--repeats 1] [--models claude-haiku-4-5]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from common import SCRATCH, Proxy, claude, claude_env, cli_json, log, rows, write_json

CC_ROOT = Path(os.environ.get("CC_ROOT", SCRATCH / "cc"))
MCP_ROOT = Path(os.environ.get("MCP_ROOT", SCRATCH / "mcp"))
PROMPT = "Reply with exactly: hi"


def mcp_config(workdir: Path) -> Path:
    bins = MCP_ROOT / "node_modules" / ".bin"
    servers = {
        "filesystem": {"command": str(bins / "mcp-server-filesystem"), "args": [str(workdir)]},
        "memory": {"command": str(bins / "mcp-server-memory"), "args": []},
        "everything": {"command": str(bins / "mcp-server-everything"), "args": []},
        "sequential-thinking": {"command": str(bins / "mcp-server-sequential-thinking")},
    }
    path = workdir.parent / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}, indent=1))
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", default="2.1.250,2.1.287,2.1.296")
    ap.add_argument("--models", default="claude-haiku-4-5")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="overhead.json")
    a = ap.parse_args()
    results = []
    for version in a.versions.split(","):
        binary = str(CC_ROOT / version / "node_modules" / ".bin" / "claude")
        for model in a.models.split(","):
            for mcp in ("none", "4 servers"):
                for tool_search in ("default", "true"):
                    for rep in range(a.repeats):
                        cell = f"{version}-{model}-{mcp.replace(' ', '')}-ts{tool_search}-{rep}"
                        base = SCRATCH / "overhead" / cell
                        shutil.rmtree(base, ignore_errors=True)
                        work = base / "work"
                        work.mkdir(parents=True)
                        args = [PROMPT, "--model", model, "--strict-mcp-config"]
                        if mcp != "none":
                            args += ["--mcp-config", str(mcp_config(work))]
                        with Proxy(base / "sf") as proxy:
                            extra = (
                                {} if tool_search == "default" else {"ENABLE_TOOL_SEARCH": "true"}
                            )
                            res = claude(
                                binary,
                                args,
                                claude_env(proxy.url, **extra),
                                work,
                                "overhead",
                                expected=0.25,
                                timeout=300,
                                tag=cell,
                            )
                            ledger = rows(proxy.db)
                            main_row = max(
                                (r for r in ledger if r["endpoint"] == "messages"),
                                key=lambda r: (
                                    r["input_tokens"]
                                    + r["cache_write_5m"]
                                    + r["cache_write_1h"]
                                    + r["cache_read"]
                                ),
                                default=None,
                            )
                            profile = (
                                cli_json(proxy.home, "profile", str(main_row["id"]))
                                if main_row
                                else None
                            )
                        results.append(
                            {
                                "cell": cell,
                                "version": version,
                                "model": model,
                                "mcp": mcp,
                                "tool_search": tool_search,
                                "repeat": rep,
                                "claude_result": {
                                    k: res.get(k)
                                    for k in (
                                        "total_cost_usd",
                                        "usage",
                                        "modelUsage",
                                        "session_id",
                                        "is_error",
                                        "result",
                                        "_wall_s",
                                        "_returncode",
                                        "parse_error",
                                        "stderr",
                                    )
                                },
                                "ledger": ledger,
                                "main_profile": profile,
                            }
                        )
                        prompt = (profile or {}).get("profile", {}).get("total_tokens")
                        log(f"overhead {cell}: prompt {prompt} cost {res.get('total_cost_usd')}")
                        write_json(a.out, results)


if __name__ == "__main__":
    main()
