# src/ztra/world/summary.py

Renders a `World` down to a compact dict meant for an LLM agent to read directly — the "what's on the
bench right now" view referenced throughout ARCHITECTURE.md as the agent-facing picture of state.

It's a thin fold over the other world-model modules rather than new logic: it calls `validate()` for
errors/warnings, `describe_mixture`/`total_ul` for well contents, and `Deck` queries for tip counts
and slot placement. The one piece of judgment is what to *omit* — empty wells are dropped, and
`sensors`/`hazards` are collapsed to one line each — so the output stays small enough for an agent's
context window while still being enough to plan a next protocol.
