# Lowering & Backends (v1)

Lowering turns PIR-H (what the compiler checked) into PIR-L (what a liquid handler does), and a backend
turns PIR-L into vendor code. Both are deterministic and run before anything is committed, so the store can
record exactly what will be sent to the robot.

```
ztra lower <world_dir> <protocol.yaml>              → { ok, program, files: { "segment_0.py": "...", ... } }
ztra lower <world_dir> <protocol.yaml> --out <dir>  → writes program.json + segment_N.py
```

## 1. What lowering does

| PIR-H | PIR-L |
|---|---|
| `transform thaw V` | `pause "Thaw V and resume"` |
| `transform transfer A → B, v µL` | `pick_up_tip`, then ⌈v / max⌉ × (`aspirate`, `dispense`) of equal volume, then `drop_tip` |
| `transform mix at W, v µL × n` | `pick_up_tip`, `mix`, `drop_tip` |
| `observe sensor` | `observe` (robot pauses; the telemetry service reads) |
| `transform delay` | `delay seconds` (the robot waits on its own) |
| `transform tip pick` / `drop` / `return` | opens a shared-tip scope (the tip is picked at the first step inside); `drop_tip` or `return_tip` at the end |
| `transform replenish` | `pause "Replace tip rack R…"` with `replenish_rack: R`; the backend adds `reset_tipracks()` for every pipette that uses the rack; allocation restarts at A1 |
| `transform magnet` | `magnet {module, engaged, height_mm}` → `MAG1.engage(height_from_base=…)` / `MAG1.disengage()`; the module is loaded with `ctx.load_module(model, slot)` and its plate with `MAG1.load_labware(...)` |
| `branch` | a segment boundary — see §2 |

Along the way it:

- **resolves addresses** — a vial becomes `(tube rack, position)` through `Deck.linker`; a plate well becomes
  `(plate, well)`. The labware must sit in a slot. Errors: `E_UNLINKED`, `E_NOT_ON_DECK`.
- **picks pipettes** the same way the compiler does (smallest that fits; largest with splitting for
  transfers), so costs and cycles agree.
- **picks tips** the same way the compiler does (column-major, racks in id order, placed racks only), so the
  tip wells in the vendor code match the predicted world's `tip_racks.used`. Error: `E_TIPS`.

PIR-L ops: `pick_up_tip`, `aspirate`, `dispense`, `mix`, `drop_tip`, `return_tip`, `pause`, `observe`, `delay`.
`pick_up_tip`, `return_tip`, `aspirate`, `dispense` and `mix` carry `channels` (1, or 8 for a column step: the
well named is the top of the column, or the one trough well every channel uses; channels 1–7 of a gang lower
to nothing, channel 0 carries the action). `aspirate`, `dispense` and `mix` carry the optional motion (`at`, `offset_mm`, `side_mm`, `rate_ul_s`);
`aspirate`/`dispense` carry `air_gap_ul` (the dispense delivers liquid + air), `dispense` carries `blow_out`.
The backend emits `well.bottom(z)` / `well.top(z)` with `.move(types.Point(x=side, …))`, an absolute
flow-rate set-and-restore around the command, `air_gap()` after the aspirate and `blow_out()` after the
dispense. Every op keeps
its `origin` (protocol step and loop iteration), and the backend writes it as a comment above the code.

## 2. Segments

A vendor protocol runs start to finish; it cannot ask a scale mid-run and change course. So a program
with `if_observed` is lowered to a **tree of segments**:

```
segment 0: ...ops...  ends: decide on 'after_fill' (mass_mg >= 215) → 1 if true, 2 if false
segment 1: ...then-arm + everything after the branch...  ends: halt
segment 2: ...else-arm + everything after the branch...  ends: halt
```

Segment 0 is the entry. The **runtime** (build-order step 7) runs a segment, takes the reading at its
`observe`, evaluates the `decide`, and runs the chosen child. Each segment starts with no tip attached and
ends with the tip dropped, so segments are independent vendor runs.

**Everything after a branch is copied into each arm.** That is deliberate: the two arms may use different
numbers of tips, so the continuation's tip wells differ per path. Copying keeps every segment's tip wells
exact. The tree has at most `MAX_PATHS` leaves (the compiler's cap), so this stays small.

`Program::walk(&[true, false, …])` follows the tree for a given list of decisions.

## 3. The Opentrons backend

One Python file per segment, Protocol API v2:

```python
# ztra segment 0 of 3. Generated; do not edit.
# ends: runtime decides on 'after_fill' (mass_mg >= 215) -> segment 1 if true, segment 2 if false

requirements = {"robotType": "OT-2", "apiLevel": "2.16"}


def run(ctx):
    P1 = ctx.load_labware("corning_96_wellplate_360ul_flat", "1")
    TR1 = ctx.load_labware("opentrons_24_tuberack_nest_1.5ml_snapcap", "2")
    TIPS1 = ctx.load_labware("opentrons_96_tiprack_300ul", "3")
    pip_p300_single_gen2 = ctx.load_instrument("p300_single_gen2", "right", tip_racks=[TIPS1])
    # protocol step [0]
    ctx.pause("Thaw V_enzyme and resume")
    # protocol step [1, 0] iteration [1]
    pip_p300_single_gen2.pick_up_tip(TIPS1["C1"])
    pip_p300_single_gen2.aspirate(50, TR1["A1"])
    pip_p300_single_gen2.dispense(50, P1["B1"])
    pip_p300_single_gen2.drop_tip()
    ...
```

The file also declares the starting liquids (`define_liquid` / `load_liquid`) from the world model so the app shows
the initial deck state. Everything vendor-specific comes from the world model: `robot.model` picks `OT-2` vs `Flex`, slot names come
from `Deck.slots`, labware names are the catalog keys (which are the Opentrons load names), pipettes and their
tip racks come from `Hardware.pipettes`. Flex additionally gets `ctx.load_trash_bin(<trash slot>)`; OT-2's
trash is fixed in slot 12 and needs no call.

`observe` and `thaw` become `ctx.pause(...)`. Opentrons has no readable sensors in the protocol API, and
resuming a paused run from outside is a runtime concern, not a backend one.

## 4. Checking the backend against the vendor simulator

The test `opentrons_simulate_accepts_every_segment` runs every generated file through Opentrons' own
simulator when these variables point at a binary:

```
ZTRA_OT_SIM_OT2=/path/to/venv-opentrons-8/bin/opentrons_simulate    # OT-2 needs opentrons < 9
ZTRA_OT_SIM_FLEX=/path/to/venv-opentrons-9/bin/opentrons_simulate   # Flex needs opentrons >= 9
.venv/bin/pytest tests/test_lower.py
```

Without them the test prints "skipping" and passes. Two environments are required because the
`opentrons` package ≥ 9 refuses OT-2 protocols outright.

## 5. Not in v1

- Static deck clearance checks (`E_DECK`) — the labware catalog has `height_mm` but no pipette/tip geometry yet.
- Multi-channel pipettes, air gaps, blow-out, touch-tip, flow rates — the backend emits defaults.
- SiLA2 backend.
- Tip reuse policies (every transfer takes a fresh tip).
