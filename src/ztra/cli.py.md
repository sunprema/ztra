---
path: "src/ztra/cli.py"
summary: "The ztra command-line entrypoint: a JSON-only argparse wrapper that mirrors the MCP server's tools."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: true
---

> [!WARNING]
> **Nexus desync** — explainer says exit code 1 means only a `CompileError`, `StoreError`, or failed run; the code also returns 1 for `cmd_init`'s `ScaffoldError`, for `cmd_world validate` whenever validation issues exist (no exception involved), and for `cmd_preflight` whenever the preflight report is infeasible.

The `ztra` command-line entrypoint — a thin argparse wrapper that always prints one JSON object to stdout and never a human-formatted message, so its output is as scriptable/agent-readable as the MCP tools in `mcp_server.py`. In fact the two are close mirrors of each other: `cmd_compile`/`cmd_simulate`/`cmd_diff`/etc. call exactly the same core functions (`compile`, `simulate`, `diff`, `lower`, the `Store`) that the MCP tools call — the CLI is what a person runs by hand, the MCP server is what an agent calls directly, both fronting the same core.

Exit codes carry meaning: `1` means the core itself refused (a `CompileError`, `StoreError`, or a failed run) — a real answer, just not a passing one. `2` means an input file couldn't even be loaded (bad YAML, missing world). This distinction lets a calling script or agent tell "the protocol is impossible" apart from "you gave me a bad path."

`cmd_run` is the one command that actually dispatches a (simulated) physical run — it only proceeds with `--yes`, mirroring the runtime's separate approve gate, and defaults to the fake driver; `--driver otsim` swaps in the vendor-simulator cross-check from `drivers/otsim.py` instead (and refuses `--fault` there, since fault injection is fake-driver only).
