# Measurements 2026-10-10

Nested spend recorded: $14.0428 (counts turn 1 of resumed sessions twice)

## Ledger against Claude Code's total_cost_usd

- Runs compared: 111
- Claude Code total: $10.107486
- Ledger total: $10.107486
- Largest per-run difference: $2.22e-16

## Fixed context overhead

| Version | Model | MCP servers | Tool search | Prompt tokens | Built-in tools | MCP tools | System prompt | Cold cost |
|---|---|---|---|---:|---:|---:|---:|---:|
| 2.1.250 | claude-haiku-4-5 | none | off (default) | 35,228 | 26,783 | 0 | 6,420 | $0.0705 |
| 2.1.250 | claude-haiku-4-5 | none | on (`ENABLE_TOOL_SEARCH=true`) | 20,455 | 11,933 | 0 | 6,385 | $0.0409 |
| 2.1.250 | claude-haiku-4-5 | 4 servers | off (default) | 42,272 | 27,327 | 6,102 | 6,410 | $0.0845 |
| 2.1.250 | claude-haiku-4-5 | 4 servers | on (`ENABLE_TOOL_SEARCH=true`) | 21,288 | 11,990 | 0 | 6,419 | $0.0426 |
| 2.1.287 | claude-haiku-4-5 | none | off (default) | 35,442 (35,442-35,611) | 26,742 | 0 | 6,290 | $0.0709 |
| 2.1.287 | claude-haiku-4-5 | none | on (`ENABLE_TOOL_SEARCH=true`) | 21,001 | 12,086 | 0 | 6,342 | $0.0420 |
| 2.1.287 | claude-haiku-4-5 | 4 servers | off (default) | 43,030 | 27,306 | 6,486 | 6,365 | $0.0861 |
| 2.1.287 | claude-haiku-4-5 | 4 servers | on (`ENABLE_TOOL_SEARCH=true`) | 21,847 | 12,147 | 0 | 6,374 | $0.0437 |
| 2.1.296 | claude-haiku-4-5 | none | off (default) | 35,995 | 27,014 | 0 | 6,367 | $0.0720 |
| 2.1.296 | claude-haiku-4-5 | none | on (`ENABLE_TOOL_SEARCH=true`) | 21,328 | 12,275 | 0 | 6,333 | $0.0427 |
| 2.1.296 | claude-haiku-4-5 | 4 servers | off (default) | 43,586 | 27,530 | 6,687 | 6,352 | $0.0872 |
| 2.1.296 | claude-haiku-4-5 | 4 servers | on (`ENABLE_TOOL_SEARCH=true`) | 22,174 | 12,336 | 0 | 6,365 | $0.0443 |
| 2.1.296 | claude-haiku-5-5 | none | off (default) | 32,397 | 26,290 | 0 | 5,727 | - |
| 2.1.296 | claude-haiku-5-5 | none | on (`ENABLE_TOOL_SEARCH=true`) | 17,338 | 10,971 | 0 | 5,979 | - |
| 2.1.296 | claude-haiku-5-5 | 4 servers | off (default) | 42,669 | 27,077 | 8,953 | 6,259 | - |
| 2.1.296 | claude-haiku-5-5 | 4 servers | on (`ENABLE_TOOL_SEARCH=true`) | 18,562 | 11,111 | 0 | 7,058 | - |
| 2.1.296 | claude-sonnet-5-5 | none | off (default) | 31,622 | 26,350 | 0 | 4,891 | - |
| 2.1.296 | claude-sonnet-5-5 | none | on (`ENABLE_TOOL_SEARCH=true`) | 16,563 | 11,031 | 0 | 5,141 | - |
| 2.1.296 | claude-sonnet-5-5 | 4 servers | off (default) | 41,894 | 27,120 | 8,972 | 5,421 | - |
| 2.1.296 | claude-sonnet-5-5 | 4 servers | on (`ENABLE_TOOL_SEARCH=true`) | 17,787 | 11,174 | 0 | 6,217 | - |

## Cap stress (shared session cap)

| Round | Model | Reserve | Cap | Agents | Killed (aborted rows) | Blocked | Ledger total | Over cap | Completed agents: reported | ledger | Killed agents: ledger |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| r1 | opus-5-5 | estimate | $1 | 4 | 0 (0) | 4 | $1.0838 | +8.4% | $1.0838 | $1.0838 | $0.0000 |
| r2 | opus-5-5 | estimate | $1 | 6 | 2 (2) | 4 | $0.9137 | -8.6% | $0.7835 | $0.7835 | $0.1302 |
| r3 | opus-5-5 | estimate | $1 | 8 | 2 (2) | 6 | $1.0147 | +1.5% | $0.8843 | $0.8843 | $0.1304 |
| r4 | opus-5-5 | estimate | $1 | 8 | 3 (2) | 5 | $0.8906 | -10.9% | $0.6907 | $0.6907 | $0.2000 |
| r5 | opus-5-5 | estimate | $1 | 8 | 3 (2) | 5 | $0.8975 | -10.3% | $0.6285 | $0.6285 | $0.2690 |
| r6 | opus-5-5 | estimate | $1 | 6 | 0 (0) | 6 | $0.9873 | -1.3% | $0.9873 | $0.9873 | $0.0000 |
| r7 | sonnet-5-5 | worst_case | $1 | 6 | 0 (0) | 6 | $0.0000 | -100.0% | $0.0000 | $0.0000 | $0.0000 |
| r8 | opus-5-5 | worst_case | $1 | 4 | 0 (0) | 4 | $0.0000 | -100.0% | $0.0000 | $0.0000 | $0.0000 |
| r9 | sonnet-5-5 | estimate | $1 | 8 | 1 (0) | 7 | $1.0896 | +9.0% | $0.9487 | $0.9487 | $0.1409 |
| r10 | sonnet-5-5 | worst_case | $5 | 6 | 1 (1) | 4 | $1.5643 | -68.7% | $1.5193 | $1.5193 | $0.0450 |

## Streams cut off by a killed client

| Round | Row | Model | Streamed chars | Output tokens (estimated) | Prompt tokens | Cost (estimated) |
|---|---:|---|---:|---:|---:|---:|
| r2 | 7 | claude-opus-5-5 | 294 | 98 | 33,136 | $0.0112 |
| r2 | 11 | claude-opus-5-5 | 291 | 97 | 33,136 | $0.0112 |
| r3 | 9 | claude-opus-5-5 | 306 | 102 | 33,136 | $0.0113 |
| r3 | 10 | claude-opus-5-5 | 301 | 101 | 33,136 | $0.0113 |
| r4 | 9 | claude-opus-5-5 | 308 | 103 | 33,136 | $0.0113 |
| r4 | 11 | claude-opus-5-5 | 326 | 109 | 33,136 | $0.0114 |
| r5 | 17 | claude-opus-5-5 | 1,724 | 575 | 33,466 | $0.0208 |
| r5 | 20 | claude-opus-5-5 | 1,703 | 568 | 33,495 | $0.0209 |
| r10 | 12 | claude-sonnet-5-5 | 1,554 | 518 | 33,916 | $0.0101 |

## Cache breakers

| Breaker | Turn-2 cost (median) | Cache read on turn 2 | Tokens re-written | API reason | skinflint verdict (with API) | skinflint verdict (local only) |
|---|---:|---:|---:|---|---|---|
| claude_md | $0.0042 | 36,099, 36,099, 36,100 | 194, 190, 180 | none (x3) | hit (x3) | hit (x3) |
| control | $0.0039 | 35,964, 35,965, 35,964 | 60, 81, 58 | none (x3) | hit (x3) | hit (x3) |
| mcp | $0.0223 | 0, 28,250, 28,250 | 37,880, 9,643, 9,627 | tools_changed (x3) | api_reported: tools_changed: ListMcpResourcesTool, ReadMcpResourceDirTool, ReadMcpResourceTool +9 more added; hit (x2) | prefix_changed: tools changed: ListMcpResourcesTool, ReadMcpResourceDirTool, ReadMcpResourceTool +9 more added; hit (x2) |
| model | $0.0029 | 0, 31,571, 31,571 | 44,384, 12,813, 12,813 | none (x3) | cold: first request of this agent; partial: no earlier request from this agent; read a cache shared with another (x2) | cold: first request of this agent; partial: no earlier request from this agent; read a cache shared with another (x2) |
| system | $0.0039 | 35,963, 35,964, 35,964 | 62, 55, 60 | none (x3) | hit (x3) | hit (x3) |
| tools | $0.0220 | 25,921, 25,921, 25,921 | 9,622, 9,622, 9,632 | tools_changed (x3) | api_reported: tools_changed: NotebookEdit removed (x3) | prefix_changed: tools changed: NotebookEdit removed (x3) |
| ttl | $0.0431 | 25,804, 25,804, 0 | 9,411, 9,410, 35,216 | none (x3) | expired: 5m33s since the previous request exceeds the 5m00s cache lifetime (x2); expired: 5m32s since the previous request exceeds the 5m00s cache lifetime | expired: 5m33s since the previous request exceeds the 5m00s cache lifetime (x2); expired: 5m32s since the previous request exceeds the 5m00s cache lifetime |
| git | $0.0039 | 36,044, 36,040, 36,042 | 65, 64, 58 | none (x3) | hit (x3) | hit (x3) |
| system_full | $0.0039 | 35,969, 35,969, 35,968 | 58, 53, 63 | none (x3) | hit (x3) | hit (x3) |
