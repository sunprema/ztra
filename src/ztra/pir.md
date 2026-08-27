# pir.py

Defines PIR-H, the vendor-neutral form the compiler, store, and diff engine all reason about.
It's deliberately small: by the time PIR-H exists, loops have already been unrolled (by
`compiler.py`'s `_Unroller`), so there's no loop construct here at all — just three op shapes.

`Transform` covers everything that changes the world's state (thaw, transfer, mix, delay,
tip pick/drop, rack replenish, magnet engage/disengage) via one `TransformKind` enum rather
than a class per operation; the fields that don't apply to a given kind are simply left `None`
(e.g. `height_mm` only means something for `magnet`). `ObserveOp` is a sensor reading request.
`Branch` is the one *structural* op — the only reason it needs to exist at all is that a flat
op list can't express "do this if the reading says X, otherwise do that"; everything else could
in principle be a flat sequence.

Every op carries an `Origin` (which AST step it came from, which loop iterations it was inside,
and what `$variable` bindings were active) purely so that later stages — compiler errors, the
lowered PIR-L, the world diff — can always point back at the original protocol line, even after
unrolling has destroyed the loop structure.

`count()` exists because a flat `len(ops)` undercounts once branches are involved — it recurses
into both arms of every `Branch` to get the true total op count across all paths.
