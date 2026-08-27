---
path: "src/ztra/backend/opentrons.py"
summary: "Turns one PIR-L segment into a runnable Opentrons Protocol API v2 Python file."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: true
---

> [!WARNING]
> **Nexus desync** — explainer says OT-2/Flex trash handling "comes from Hardware.yaml, not from branching backend code" and that every op gets a preceding comment; the code branches directly on `hw.robot.model` for trash and the robot string, and a comment is only emitted when an op's `origin` differs from the previous op's, not for every op.

Turns one PIR-L segment into a runnable Opentrons Protocol API v2 file. This is the only place that speaks the vendor's syntax; everything upstream (compiler, lowering) stays vendor-neutral.

`emit_segment` writes straight-line Python: load labware/modules/pipettes from the world model, declare starting liquids so the Opentrons app can show initial deck state, then translate each lowered op (`Aspirate`, `Dispense`, `PickUpTip`, …) to the matching pipette call. OT-2 and Flex differ only in a few details — trash handling, robot/API strings — and those come from `Hardware.yaml`, not from branching backend code.

Two things are worth knowing if you're debugging generated code:

- `_liquids` and `_empties` exist only because the vendor simulator tracks liquid volume per well from API 2.22 onward, and it needs to be told what's already there *and* which destination wells are about to receive something, or its own tracking disagrees with ours (see `drivers/otsim.py`, which cross-checks exactly this).
- `_with_rate` wraps a single aspirate/dispense/mix in a save-and-restore of the pipette's flow rate, because the vendor API has no "just this once" rate override — you have to mutate the pipette's rate, act, then set it back.

Every op is preceded by a comment naming the protocol step it came from (`op.origin.step_path`), so a person reading the generated file can trace a vendor command back to a line in the original protocol.
