"""Summarise the raw measurements in measurements/<date>/ as markdown tables.

For the cache breakers it also replays skinflint's cache doctor on a copy of each trial's
ledger with the API diagnostics removed, so the local verdict can be compared with the API's.
That step needs the trial ledgers under $MEASURE_SCRATCH/cachebreak/.

    MEASURE_SCRATCH=... SKINFLINT_BIN=... python scripts/measure/analyse.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import statistics

from common import OUT, SCRATCH, cli_json, spent, write_json

HAIKU45_1H_WRITE = 2.0  # $/MTok, from skinflint's price table


def overhead() -> list[str]:
    lines = [
        "| Version | Model | MCP servers | Tool search | Prompt tokens | Built-in tools "
        "| MCP tools | System prompt | Cold cost |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    cells = json.loads((OUT / "overhead.json").read_text())
    models = OUT / "overhead-models.json"
    if models.exists():
        cells += json.loads(models.read_text())
    seen = set()
    for c in cells:
        key = (c["version"], c["model"], c["mcp"], c["tool_search"])
        if key in seen:
            continue
        seen.add(key)
        p = (c["main_profile"] or {}).get("profile", {})
        groups = {g["group"]: g["tokens"] for g in p.get("groups", [])}
        mcp = sum(v for k, v in groups.items() if k.startswith("tools: mcp"))
        total = p.get("total_tokens", 0)
        reps = [
            (x["main_profile"] or {}).get("profile", {}).get("total_tokens")
            for x in cells
            if (x["version"], x["model"], x["mcp"], x["tool_search"]) == key
        ]
        spread = "" if len(set(reps)) == 1 else f" ({min(reps):,}-{max(reps):,})"
        cold = f"${total * HAIKU45_1H_WRITE / 1e6:.4f}" if c["model"] == "claude-haiku-4-5" else "-"
        ts = "on (`ENABLE_TOOL_SEARCH=true`)" if c["tool_search"] == "true" else "off (default)"
        lines.append(
            f"| {c['version']} | {c['model']} | {c['mcp']} | {ts} | {total:,}{spread} "
            f"| {groups.get('tools: built-in', 0):,} | {mcp:,} | "
            f"{groups.get('system prompt', 0):,} | {cold} |"
        )
    return lines


def capstress() -> list[str]:
    lines = [
        "| Round | Model | Reserve | Cap | Agents | Killed (aborted rows) | Blocked | Ledger total "
        "| Over cap | Completed agents: reported | ledger | Killed agents: ledger |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for path in sorted(OUT.glob("capstress-r*.json"), key=lambda p: int(p.stem[11:])):
        d = json.loads(path.read_text())
        s = d["summary"]
        agents = d["agents"].values()
        done = [a for a in agents if a["result"]]
        killed = [a for a in agents if not a["result"]]
        rep = sum(float(a["result"].get("total_cost_usd") or 0) for a in done)
        led = sum(a["ledger_usd"] for a in done)
        kled = sum(a["ledger_usd"] for a in killed)
        aborted = sum(1 for r in d["ledger"] if r["state"] == "aborted")
        over = s["ledger_total_usd"] - s["cap_usd"]
        lines.append(
            f"| {s['round']} | {s['model'].removeprefix('claude-')} "
            f"| {s.get('reserve', 'estimate')} | ${s['cap_usd']:g} "
            f"| {s['agents']} | {len(killed)} ({aborted}) | {s['blocked_rows']} "
            f"| ${s['ledger_total_usd']:.4f} | {'+' if over > 0 else ''}{over / s['cap_usd']:.1%} "
            f"| ${rep:.4f} | ${led:.4f} | ${kled:.4f} |"
        )
    return lines


def aborted() -> list[str]:
    lines = [
        "| Round | Row | Model | Streamed chars | Output tokens (estimated) | Prompt tokens "
        "| Cost (estimated) |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for path in sorted(OUT.glob("capstress-r*.json"), key=lambda p: int(p.stem[11:])):
        d = json.loads(path.read_text())
        for r in d["ledger"]:
            if r["state"] != "aborted":
                continue
            chars = (r["error"] or "").split("estimated from ")[-1].split(" streamed")[0]
            prompt = r["input_tokens"] + r["cache_write_5m"] + r["cache_write_1h"] + r["cache_read"]
            lines.append(
                f"| {d['summary']['round']} | {r['id']} | {r['model']} | {chars} "
                f"| {r['output_tokens']:,} | {prompt:,} | ${r['cost_usd']:.4f} |"
            )
    return lines


def local_verdicts(cell: str, session: str) -> dict | None:
    """skinflint's own cache verdicts for a trial, with the API's diagnostics stripped."""
    src = SCRATCH / "cachebreak" / cell / "sf"
    if not (src / "skinflint.db").exists():
        return None
    dst = SCRATCH / "cachebreak-local" / cell
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("serve.log", "config.toml"))
    con = sqlite3.connect(dst / "skinflint.db")
    con.execute("UPDATE requests SET cache_miss_reason = NULL, cache_missed_tokens = NULL")
    con.commit()
    con.close()
    out = cli_json(dst, "cache", "--session", session)
    return out if isinstance(out, dict) else None


def cachebreak() -> list[str]:
    trials = json.loads((OUT / "cachebreak.json").read_text())
    extra = OUT / "cachebreak-extra.json"
    if extra.exists():
        trials += json.loads(extra.read_text())
    local = {}
    for t in trials:
        sid = t["turn2"].get("session_id") or t["turn1"].get("session_id")
        local[t["cell"]] = local_verdicts(t["cell"], sid) if sid else None
    write_json("cachebreak-local.json", local)
    by: dict[str, list[dict]] = {}
    for t in trials:
        by.setdefault(t["breaker"], []).append(t)
    lines = [
        "| Breaker | Turn-2 cost (median) | Cache read on turn 2 | Tokens re-written | "
        "API reason | skinflint verdict (with API) | skinflint verdict (local only) |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for breaker, ts in by.items():
        costs, reads, writes, api, joint, alone = [], [], [], [], [], []
        for t in ts:
            # On --resume Claude Code reports the session's cumulative cost.
            costs.append(
                float(t["turn2"].get("total_cost_usd") or 0)
                - float(t["turn1"].get("total_cost_usd") or 0)
            )
            ev = (t["cache"] or {}).get("events", [])
            main = [e for e in ev if e["predecessor_id"] is not None] or ev[-1:]
            e = max(main, key=lambda e: e["prompt_tokens"]) if main else None
            if e:
                reads.append(e["cache_read"])
                writes.append(e["cache_write"])
                api.append(e["api_reason"] or "none")
                joint.append(f"{e['verdict']}: {e['cause']}" if e["cause"] else e["verdict"])
            lev = (local[t["cell"]] or {}).get("events", [])
            le = next((x for x in lev if e and x["record_id"] == e["record_id"]), None)
            if le:
                alone.append(f"{le['verdict']}: {le['cause']}" if le["cause"] else le["verdict"])

        def uniq(xs: list[str]) -> str:
            return "; ".join(
                f"{x} (x{xs.count(x)})" if xs.count(x) > 1 else x for x in dict.fromkeys(xs)
            )

        lines.append(
            f"| {breaker} | ${statistics.median(costs):.4f} "
            f"| {', '.join(f'{x:,}' for x in reads)} | {', '.join(f'{x:,}' for x in writes)} "
            f"| {uniq(api)} | {uniq(joint)} | {uniq(alone)} |"
        )
    return lines


def reconcile() -> list[str]:
    """Claude Code's total_cost_usd against the ledger, per completed run."""
    pairs = []
    for name in ("overhead.json", "overhead-models.json"):
        for c in json.loads((OUT / name).read_text()):
            rep = c["claude_result"]["total_cost_usd"] or 0
            pairs.append((rep, sum(r["cost_usd"] for r in c["ledger"])))
    for path in sorted(OUT.glob("capstress-r*.json"), key=lambda p: int(p.stem[11:])):
        for a in json.loads(path.read_text())["agents"].values():
            if a["result"]:
                pairs.append((float(a["result"]["total_cost_usd"] or 0), a["ledger_usd"]))
    for name in ("cachebreak.json", "cachebreak-extra.json"):
        for t in json.loads((OUT / name).read_text()):
            # Cumulative on --resume: turn 2's figure covers the whole session.
            rep = t["turn2"]["total_cost_usd"] or 0
            pairs.append((rep, sum(r["cost_usd"] for r in t["ledger"])))
    worst = max(abs(led - rep) for rep, led in pairs)
    return [
        f"- Runs compared: {len(pairs)}",
        f"- Claude Code total: ${sum(p[0] for p in pairs):.6f}",
        f"- Ledger total: ${sum(p[1] for p in pairs):.6f}",
        f"- Largest per-run difference: ${worst:.2e}",
    ]


def main() -> None:
    parts = [
        f"# Measurements {OUT.name}",
        "",
        f"Nested spend recorded: ${spent():.4f} (counts turn 1 of resumed sessions twice)",
        "",
    ]
    parts += ["## Ledger against Claude Code's total_cost_usd", "", *reconcile(), ""]
    parts += ["## Fixed context overhead", "", *overhead(), ""]
    parts += ["## Cap stress (shared session cap)", "", *capstress(), ""]
    parts += ["## Streams cut off by a killed client", "", *aborted(), ""]
    if (OUT / "cachebreak.json").exists():
        parts += ["## Cache breakers", "", *cachebreak(), ""]
    (OUT / "summary.md").write_text("\n".join(parts))
    print("\n".join(parts))


if __name__ == "__main__":
    main()
