"""Baseline without the proxy: the prompt size Claude Code reports when it talks to
api.anthropic.com directly, for comparison with overhead.py (which goes through skinflint).

    MEASURE_SCRATCH=... python scripts/measure/direct.py
"""

from __future__ import annotations

import argparse
import os
import shutil

from common import SCRATCH, claude, log, write_json
from overhead import CC_ROOT, PROMPT, mcp_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", default="2.1.250,2.1.287,2.1.296")
    ap.add_argument("--model", default="claude-haiku-4-5")
    a = ap.parse_args()
    results = []
    for version in a.versions.split(","):
        binary = str(CC_ROOT / version / "node_modules" / ".bin" / "claude")
        for mcp in ("none", "4 servers"):
            for tool_search in ("default", "false"):
                cell = f"{version}-{a.model}-{mcp.replace(' ', '')}-ts{tool_search}-direct"
                base = SCRATCH / "direct" / cell
                shutil.rmtree(base, ignore_errors=True)
                work = base / "work"
                work.mkdir(parents=True)
                args = [PROMPT, "--model", a.model, "--strict-mcp-config"]
                if mcp != "none":
                    args += ["--mcp-config", str(mcp_config(work))]
                env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
                if tool_search == "false":
                    env["ENABLE_TOOL_SEARCH"] = "false"
                res = claude(
                    binary, args, env, work, "direct", expected=0.15, timeout=300, tag=cell
                )
                u = res.get("usage") or {}
                prompt = (
                    u.get("input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                )
                results.append(
                    {
                        "cell": cell,
                        "version": version,
                        "model": a.model,
                        "mcp": mcp,
                        "tool_search": tool_search,
                        "prompt_tokens_reported": prompt,
                        "claude_result": {
                            k: res.get(k)
                            for k in (
                                "total_cost_usd",
                                "usage",
                                "modelUsage",
                                "session_id",
                                "is_error",
                                "num_turns",
                            )
                        },
                    }
                )
                log(f"direct {cell}: prompt {prompt} (all API calls summed)")
                write_json("direct.json", results)


if __name__ == "__main__":
    main()
