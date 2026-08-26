# Opentrons Python API — what ztra relies on

Reference notes from the Opentrons Python Protocol API docs (https://docs.opentrons.com/python-api/,
read 2026-08-25, API 2.29) and from poking at the installed packages. Only the facts that shape ztra's
world model, lowering, backend and runtime. Each has a "so in ztra" line.

## Protocol file shape

- A protocol is a Python file with `metadata = {...}`, `requirements = {"robotType": "OT-2"|"Flex", "apiLevel": "2.x"}`
  and `def run(ctx)`. For Flex `requirements` is mandatory and `apiLevel` lives there; for OT-2 it is optional.
- Current max apiLevel is **2.29**. **OT-2 runs 2.0–2.28, Flex runs 2.15–2.29.**
- The `opentrons` PyPI package ≥ 9 refuses OT-2 protocols; OT-2 needs `opentrons < 9`.
- *So in ztra:* the backend emits both dicts; `Hardware.robot.api_level` is validated against the robot's range
  (`W_API_LEVEL`); one vendor venv per robot generation.

## Deck and slots

- Flex slots are `A1`–`D4`; column 4 is a gripper-only staging area. OT-2 slots are `1`–`11` plus the fixed
  trash at `12`. Since 2.15 either naming is accepted on either robot, with a fixed correspondence
  (Flex `D1` = OT-2 `1`, … `A3` = trash).
- OT-2: fixed trash, no code. Flex ≥ 2.16: `ctx.load_trash_bin("A3")` (any of A1–D1, A3–D3) or
  `ctx.load_waste_chute()` (D3 only). Flex 2.15 had an implicit trash in A3.
- *So in ztra:* `Deck.slots` uses the robot's native names; the validator enforces exactly one `trash: true`
  (at `12` on OT-2); the backend calls `load_trash_bin` on Flex only, and the validator requires Flex apiLevel ≥ 2.16.

## Labware

- `ctx.load_labware(load_name, location)`; wells via `plate["A1"]`, `rows()`, `columns()`.
- Definitions live in the `opentrons_shared_data` package (`labware.load_definition(name, 1)`): `wells[A1].totalLiquidVolume`,
  `depth`, `dimensions.zDimension`, `ordering` (columns of wells), `parameters.isTiprack`, `tipLength`, `tipOverlap`.
- *So in ztra:* the catalog keys in `Hardware.labware` are Opentrons load names; `scripts/import_labware.py`
  (run inside a vendor venv) prints catalog entries from the shared definitions. `height_mm` = `zDimension`.

## Pipettes

| OT-2 | range (µL) | Flex | range (µL) |
|---|---|---|---|
| `p20_single_gen2` / `p20_multi_gen2` | 1–20 | `flex_1channel_50` / `flex_8channel_50` | 1–50 |
| `p300_single_gen2` / `p300_multi_gen2` | 20–300 | `flex_1channel_1000` / `flex_8channel_1000` | 5–1000 |
| `p1000_single_gen2` | 100–1000 | `flex_96channel_200` / `flex_96channel_1000` | 1–200 / 5–1000 |

- `ctx.load_instrument(name, mount, tip_racks=[...])`. The 96-channel takes both mounts.
- Flex pipettes only accept tips with capacity ≤ their own (50 µL pipette: 20/50 µL tips; 1000 µL: 50/200/1000).
- Readable in a protocol: `min_volume`, `max_volume`, `channels`, `has_tip`, `current_volume`, `flow_rate`.
- *So in ztra:* `KNOWN_PIPETTES` in `hardware.py` mirrors this table; the validator warns on unknown names or
  mismatched ranges/channels, errors on a pipette from the other robot, and warns on oversized tips for Flex.
  Multi-channel pipettes are not modelled yet.

## Tips

- `pick_up_tip()` with no argument uses the API's own tracker: A1, B1, … down each column across the listed racks.
  A returned tip still counts as used. `pick_up_tip(rack["C1"])` targets a specific tip. `starting_tip`, `reset_tipracks()`.
- `drop_tip()` goes to the default trash (position varied automatically to avoid piling).
- *So in ztra:* lowering picks tips itself in the same order and always emits explicit wells, so every segment
  (a separate vendor run) starts from the right tip regardless of the vendor tracker's state.

## Liquid handling

- `aspirate(volume, location, rate=1.0)`, `dispense(volume, location, rate=1.0, push_out=None)`,
  `mix(repetitions, volume, location)`, `blow_out()`, `touch_tip()`, `air_gap()`.
- Since 2.17 a dispense may not exceed what was aspirated — the simulator enforces it.
- Default aspirate/dispense height is 1 mm above the well bottom (`well_bottom_clearance`).
- Since 2.14: `ctx.define_liquid(name, description, display_color)` and `well.load_liquid(liquid, volume)` /
  `labware.load_empty(wells)` describe the starting deck for the app. **Before 2.20, `description` and
  `display_color` must be passed explicitly (even as `None`).** Since 2.22 the engine tracks
  `well.current_liquid_volume` / `current_liquid_height`.
- Since 2.20: `detect_liquid_presence()`, `require_liquid_presence()`, `measure_liquid_height()` (pressure-based
  sensing in the pipette) and `well.meniscus()` positioning.
- *So in ztra:* the backend declares starting liquids from the world model (so the app shows the initial deck
  and the simulator can check volumes); one `aspirate`+`dispense` per cycle with explicit volumes.
  Liquid presence detection is a candidate implementation of `OBSERVE` on Flex — an on-robot sensor that
  needs no external telemetry. Not used yet.

## Pauses, comments, resuming

- `ctx.pause(msg)` stops until a person resumes on the touchscreen/app; `ctx.delay(seconds=)` is timed;
  `ctx.comment(msg)` writes to the run log; `ctx.is_simulating()`.
- The protocol API has **no way to read an external value mid-run** and no programmatic resume. Runtime
  parameters (2.18+, `add_parameters` + `ctx.params.x`) are fixed at run setup and cannot change mid-run.
- *So in ztra:* a `branch` cannot live inside one vendor run → lowering produces segments, one vendor run each.
  The runtime resumes/starts runs through the robot's HTTP API (`robot-server`, port 31950, OpenAPI at
  `/openapi.json` on the robot). **To verify on hardware:** creating a run with `runTimeParameterValues`, and
  `POST /runs/{id}/actions` with `play`/`pause`/`stop`. The docs pages for this 404'd at the time of writing.

## Simulation

- `opentrons_simulate file.py` (options: `-l` log level, `-L` custom labware dir, `-o runlog|nothing`); exit code 0 on success.
- `opentrons.simulate.simulate(file) -> (run_log, bundle)`; each run-log entry has `payload["text"]` (the human line)
  plus command-specific keys. `get_protocol_api("2.16", robot_type=...)` gives a context for interactive use.
- *So in ztra:* the lowering tests run every generated segment through the simulator when `ZTRA_OT_SIM_OT2` /
  `ZTRA_OT_SIM_FLEX` are set. The structured run log is a candidate second source of "predicted" observations
  (e.g. vendor-estimated durations) later.

## Liquid tracking in the simulator — probed 2026-08-26

Checked by running code in both packages (opentrons 8.8.2 for OT-2, 9.1.1 for Flex), not from the docs.

| capability | result |
|---|---|
| `Well.current_liquid_volume()` | works from **apiLevel 2.21** on both packages; errors below that. Our example worlds use 2.16. |
| tracking sources | exact: a tube loaded with 1000 µL reads 900 after one 100 µL transfer |
| tracking destinations | only for wells the engine was told about. An untouched well is "unknown" (`LiquidHeightUnknownError`); after `labware.load_empty([...])` it tracks correctly |
| dispensing more than aspirated | refused (`InvalidDispenseVolumeError`) |
| aspirating more than a tracked well holds | **allowed** — the tube went to −50 µL. The engine tracks; it does not check |
| running a ztra segment file inside `opentrons.simulate.get_protocol_api()` | works; every *source* volume matched ztra's prediction exactly. Destinations were missing only because the backend does not yet emit `load_empty` for them |

*So in ztra:* the compiler's `E_VOLUME` / `E_CONSUMED` / `E_STATE` / `E_HAZARD` / tip checks are additive, not
redundant — the vendor engine has none of them. Over-dispense is the one check both have.

**Built 2026-08-26: the `OpentronsSimDriver`** (`ztra run --driver otsim`, `drivers/otsim.py`). Same `Driver`
interface as the fake: each segment is re-emitted from the driver's current world (so later segments carry the
right starting volumes), run inside the vendor engine (subprocess into the vendor venv, JSON back), and the
engine's tracked volumes are compared against ztra's own replay at every pause and at the end — a mismatch or a
vendor refusal aborts the run as a `DriverFault` (`D_VENDOR_MISMATCH` / `D_VENDOR_REFUSED`). The two backend
changes went in with it: `load_empty` is emitted for every dispense/mix target from apiLevel ≥ 2.22, and the
driver requires ≥ 2.22 (`D_API_LEVEL` otherwise). The harness monkeypatches `ctx.pause` to snapshot volumes
mid-run, which the vendor engine tolerates. Value confirmed: a second, vendor-written model of what a healthy
run does; ideal pipettes, so it complements the `FakeDriver` (noise, faults) rather than replacing it.

## Motion and air gaps — probed 2026-08-26 (opentrons 8.8.2, apiLevel 2.22)

- `well.bottom(z)`, `well.top(z)` and `.move(types.Point(x, y, z))` position a command; `flow_rate.aspirate`
  / `.dispense` are absolute µL/s (p300 gen2 default 92.86) — the `rate=` argument on a command is a multiplier,
  so the backend sets and restores the absolute value instead.
- `air_gap(v)` after an aspirate: `current_volume` becomes liquid + air, the dispense must deliver both
  (≥ 2.17 refuses dispensing more than the tip holds), and **liquid tracking subtracts the air**: after
  `aspirate(100)`, `air_gap(10)`, `dispense(110)` the destination reads 100 µL. So the vendor-sim driver's
  volume comparison needs no correction.
- Well geometry comes from `opentrons_shared_data.labware.load_definition(name, 1)["wells"]["A1"]`:
  `depth`, and `diameter` (circular) or `xDimension`/`yDimension` (rectangular) — the catalog's
  `well_depth_mm` / `well_diameter_mm`.

## Magnetic module — probed 2026-08-26 (opentrons 8.8.2, apiLevel 2.22)

- `ctx.load_module("magnetic module gen2", "6")` (model `magneticModuleV2`), then `mag.load_labware(name)`;
  the plate is keyed by the module's slot in `ctx.loaded_labwares`, so the vendor-sim driver's well manifest
  needs nothing special. Any plate loads (the flat Corning plate too).
- `engage(height_from_base=h)`: the engine adds a 2.5 mm offset and refuses hardware heights outside 0–25
  (`EngageHeightOutOfRangeError`), so `h` is 0..22.5. `disengage()`. In simulation `status` does not change.
- The engine has no idea what beads are: liquid tracking sees totals only. ztra's `magnetic: true` reagents
  are a refinement on top; the two agree on totals, which is what the vendor-sim cross-check compares.
- Flex has no magnetic module; its **magnetic block** is passive (the gripper moves the plate) and is not modelled.
- Geometry of `nest_96_wellplate_100ul_pcr_full_skirt`: 100 µL, depth 14.78, diameter 5.34, height 15.7,
  default engage height 20 (`parameters.magneticModuleEngageHeight`).

## What real protocols need

The vendor's community Cookbook was reviewed 2026-08-26 as evidence of what working protocols actually
do; findings and the resulting gap list live in [COOKBOOK_FINDINGS.md](COOKBOOK_FINDINGS.md).

## Not yet used, worth knowing

- Modules (temperature, heater-shaker, thermocycler, magnetic block, plate reader 2.21, Flex stacker 2.25): a real
  `thaw` could be a temperature module instead of a pause.
- Liquid classes (2.24+): `transfer_with_liquid_class()` adjusts air gap/blowout/speeds per liquid — relevant when
  the mixture model gets concentrations and viscosity.
- Complex commands `transfer()/distribute()/consolidate()` with `new_tip=always|once|never` — we emit building
  blocks instead so PIR-L stays explicit.
- Camera capture (2.27), command groups (2.29).
