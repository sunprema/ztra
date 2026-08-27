# src/ztra/world/hardware.py

The schema for `Hardware.yaml`: robot model, pipettes, the labware catalog, the sensor model, and
safety limits (§4.1 of ARCHITECTURE.md).

`KNOWN_PIPETTES` is a small reference table of real Opentrons pipette names and their documented
ranges/channels — it exists purely so `validate.py` can warn when a world's declared pipette doesn't
match what the vendor actually ships (typo'd name, wrong range copied from an old robot). It is not
load-bearing for compilation; an unknown pipette name still works, it just loses that cross-check.

`RobotModel.valid_slot` and `fixed_trash_slot` encode the OT-2 vs. Flex difference at the source:
OT-2 has numbered slots 1–12 with trash bolted to slot 12, Flex has lettered/numbered A1–D4 with a
trash bin you place yourself. Everything else that cares about slot validity calls through here
rather than re-encoding the robot's geometry.

`Hardware.pipette_for` is the sizing logic lowering depends on: given a volume, find the smallest
pipette (of the requested channel count) that can do it in one draw; if none is big enough and
splitting is allowed, fall back to the largest pipette run over multiple cycles, rounding up so the
last cycle isn't left over-full. `reserve_ul` reserves headroom in the tip (an air gap) — the same
"minus reserve" bound protects both the single-draw and the split path.

`Accuracy` holds the per-pipette error model (systematic bias, random scatter, a volume-independent
scatter floor) — these feed the simulator's noise model (§4.6) and the sensor-based diff engine, not
the compiler itself. Defaults are deliberately loose vendor-published figures until a real robot's
numbers replace them (an open question in ARCHITECTURE.md §8).
