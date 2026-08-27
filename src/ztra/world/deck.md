# src/ztra/world/deck.py

The schema and queries for `Deck.yaml`: what sits in which slot, the linker table (vial → rack +
well, the only way the robot can reach a vial — see ARCHITECTURE.md §4.1), and tip-rack occupancy.

The interesting logic is tip allocation, because it's the one place the deck model does more than
hold data:

- `take_tip` walks racks in id order, columns then rows, and marks the first free tip used — used by
  single-channel steps.
- `take_column` does the equivalent for an 8-channel pipette: it only ever hands out a whole free
  column (skipping a column with even one tip missing), because a multi-channel head picks up 8 tips
  at once or none.

Both mutate `TipRack.used` in place and return `(rack_id, well)` (or `None` if nothing fits) rather
than raising — callers (lowering, mainly) decide whether "no tip available" is a hard compile error
or something to try a different rack for.

`Module` models the OT-2 magnetic module: a plate sits "on" it rather than in a slot of its own, and
`engaged`/`height_mm` track whether it's currently pulling beads to the well wall — protocol steps
`engage_magnet`/`disengage_magnet` (see `ztra.protocol`) flip this state during compilation.
