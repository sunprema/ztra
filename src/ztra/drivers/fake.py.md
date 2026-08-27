---
path: "src/ztra/drivers/fake.py"
summary: "FakeDriver: a pretend lab that applies lowered ops to its own physical world, with pipette sloppiness and fault injection."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: false
---

A pretend lab: the only thing that stands in for hardware today, since no real robot exists yet. `FakeDriver` keeps its own `physical` copy of the world — the ground truth the rest of ztra never sees directly, only through sensors — and applies each lowered op to it, complete with realistic pipette sloppiness (a per-pipette systematic bias drawn once at construction, plus per-dispense random noise) unless `accurate=True` asks for ideal pipettes instead.

Two things make this more than a toy:

- **Fault injection.** `faults` maps `(segment, op index) → "clog" | "door_open"`, so tests and demos can force a specific op to fail — a clog delivers nothing, a door-open aborts the run with a `DriverFault` — to exercise the runtime's and diff engine's failure paths without needing a real accident.
- **Multi-channel support.** `_channel_wells` figures out which physical wells an N-channel pickup/aspirate/dispense actually touches: a single well, a column (walking down from the given well), or the same trough well repeated for every channel, depending on the labware's row count.

`Pause` handling has two special cases worth knowing: a message starting with `"Thaw "` actually thaws the named vial (so `ztra run` can simulate the thaw step's effect), and a paused-for-tip-replenishment rack has its `used` list cleared, standing in for a person swapping in a fresh rack.
