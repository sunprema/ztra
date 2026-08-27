---
path: "src/ztra/compiler_errors.py"
summary: "CompileError: the structured, agent-readable exception every compiler and lowering failure raises."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: false
---

# compiler_errors.py

`CompileError` is the one exception type every compiler and lowering failure raises, and it's
shaped for a machine reader (an LLM agent), not just a human. Rather than a free-text message,
it carries structured fields: a stable `code` (e.g. `E_VOLUME`, `E_HAZARD`, `E_TIPS`), the
`physical_law` that was violated in plain language, the `resource` involved, what was
`expected` versus what was `actual`, and a `hint` suggesting a fix.

Two fields exist specifically because the compiler is path-sensitive and executes inside
unrolled loops: `step_path` + `iterations` pinpoint exactly which AST step and which loop
iteration(s) produced the error (from the op's `Origin`), and `branch_path` records which
`if_observed` branches were assumed true/false to reach this point, so an agent debugging a
branch-specific failure knows which hypothetical path it's looking at. `chain_of_thought` is
the trace of everything the checker did on that path up to the failure — effectively a replay
an agent can read instead of re-deriving the world state itself.

`to_dict()` is what actually reaches the agent (via the CLI/MCP JSON output), and it omits the
`iterations` and `branch_path` keys entirely when they're empty rather than sending empty lists.
