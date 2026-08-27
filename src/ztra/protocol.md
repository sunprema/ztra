# src/ztra/protocol.py

The Protocol AST — the schema for the experiment an agent writes. Per ARCHITECTURE.md §4.2, this is
data, not a DSL: agents already emit structured output natively, so a bespoke parser would add a
failure surface without adding safety.

The one rule every step type is built to satisfy is **total and bounded**: `Repeat` and `ForWells`/
`ForEach` take fixed counts or fixed lists (no `while`), and the only branching construct,
`IfObserved`, can only test a reading taken earlier on the same path (`observation` names an earlier
`Observe`'s `label`). That's what lets the compiler unroll and check the whole protocol ahead of time
instead of discovering problems at run time — see `E_LOOP_BOUND` / `E_TOO_MANY_PATHS` in
ARCHITECTURE.md §4.3.

`Loc` is the "where" vocabulary: `VialLoc` (a source tube), `WellLoc` (one well), or `ColumnLoc` (a
whole column, for 8-channel steps — every well in the column is still checked individually, but the
tip pickup and robot action are one operation). `PlaceLoc` narrows that to the two kinds a `transfer`
can actually move liquid into/out of (you can't transfer into "a column").

`WithTip` is the exception to "fresh tip per step": one tip covers its whole body, and the compiler
enforces it only ever draws from one location — this is what makes the dedicated-tip-per-well wash
pattern (bead washes, etc.) expressible without either wasting tips or risking cross-contamination.
`ReplenishTips` is the explicit "a human swapped the rack" step, so the compiler can verify what comes
after it assumes a fresh rack rather than silently trusting tip counts it can't see.

The `Step` union is a pydantic discriminated union keyed on `op`; the `model_rebuild()` calls after it
exist because several step types (`WithTip`, `Repeat`, `ForWells`, `ForEach`, `IfObserved`) reference
`Step`/themselves recursively in their `body`/`then`/`otherwise` fields, and pydantic needs the full
`Step` type to exist before those forward references can resolve.
