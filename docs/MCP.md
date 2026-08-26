# MCP Server (v1)

ztra as tools an agent can call. The server wraps the core in-process (same venv, no shelling out) and
speaks MCP over stdio. Every tool returns a JSON object; refusals are `{ok: false, error: {code, hint, …}}`
with the same codes the CLI uses, never exceptions — so the agent can read the hint and try again.

```
.venv/bin/ztra-mcp            # stdio server
```

Claude Code finds it through the repo's `.mcp.json`. Any other MCP client: run the command above and speak
stdio. Install the extra with `uv pip install -e ".[mcp]"`.

## Tools

| tool | what it does |
|---|---|
| `reference(topic)` | the docs: protocol, world, lowering, store, simulation, opentrons, architecture (also exposed as `ztra://docs/<topic>` resources) |
| `world_summary(world_dir)` | vials and volumes, filled wells, free tips, sensors, hazards, validation problems |
| `world_validate(world_dir)` | the full issue list |
| `preflight_protocol(world_dir, protocol_yaml \| protocol_path, budget?)` | the whole resource budget vs. stock, worst case across paths |
| `compile_protocol(world_dir, protocol_yaml \| protocol_path, budget?)` | predicted outcomes (conditions, world hash, cost, trace) or the structured compile error (resource errors carry the pre-flight summary) |
| `simulate_protocol(…, seeds, dispense_drift, jitter_ul, failure_rate)` | expected readings per observe with spread; failure counts |
| `lower_protocol(…)` | PIR-L segments and the vendor Python files |
| `diff_run(…, telemetry_yaml \| telemetry_path, outcome?, write_observed_world_to?)` | verdicts, classification, observed-world estimate |
| `store_init / store_branch / store_log / store_show / store_world / store_checkout` | history and branches |
| `store_commit(repo, branch, protocol…, message?, budget?)` | compile + lower + simulate, record an intent |
| `store_files(repo, hash, out_dir?)` | vendor files for an intent |
| `store_execute(repo, branch, telemetry…, observed_world_dir?, outcome?, message?)` | record a real run; main adopts the observed world |
| `store_rebase / store_verify` | recompile a stale branch; check every hash |

The server's `instructions` tell the agent the loop: summary → write protocol → compile until it passes →
simulate → commit on a branch → a person runs the files → execute with telemetry → read the diff.

## Not in v1

- The server never dispatches to a robot. `store_files` hands out vendor files; a person (later: the runtime)
  runs them and brings telemetry back.
- No authentication or remote transport; stdio only, on the same machine as the world files.
- Tools take directory paths, so the agent and the server share a filesystem.
