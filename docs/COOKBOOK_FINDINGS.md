# Findings from the Opentrons Cookbook

Reviewed 2026-08-26: [Opentrons/Protocols/Cookbook.md](https://github.com/Opentrons/Protocols/blob/develop/Cookbook.md),
the vendor team's collection of copy-paste patterns that, in their words, "get coded into almost every
protocol". Thirteen recipes. Read here as evidence: what do working protocols actually need, which of it
does ztra already make unnecessary, and which of it can ztra not yet express.

## What the cookbook confirms about ztra's thesis

The most-featured recipes are state tracking, reimplemented by hand inside each protocol file:

| Cookbook recipe | What it does | In ztra |
|---|---|---|
| Liquid level tracking (3 of the 13 recipes) | Subclasses the vendor `Well` class to bolt on `current_volume` and a height estimate, using private internals (`well._impl`) that broke at API 2.13 and had to be rewritten (`well._core`) | Volumes are the world model; heights become derivable when labware geometry lands (gap 5) |
| Track data across protocol runs | Mutable CSV/JSON on the robot's filesystem (`/data/csv/tiptracking.csv`), with `is_simulating()` branches feeding fake data to the simulator | Cross-run state is the store: tip occupancy and volumes persist through observation commits, hash-chained instead of editable |
| Tip tracking with refills | A hand-maintained tip ledger serialized to JSON, and a custom `_pick_up()` everyone must remember to call | Tip racks and `used` are world state; lowering allocates explicit tips; exhaustion is `E_TIPS` at compile time |

The vendor's own team distributes workarounds for the absence of a versioned source of truth. That is the
problem ztra exists to solve, and these recipes become obsolete under it.

## Gaps: what working protocols do that ztra cannot express yet

In **implementation order** — dependency- and risk-aware, smallest useful step first, ending at the
workflow that needs them all. Sizes: S = a day or less, M = days, L = a week-scale change.

1. **Per-well parameter tables (S) — done 2026-08-26.** The two CSV recipes exist to give each well its
   own volume. Now `for_each` (PROTOCOL.md): a list of items, `$row.well` / `$row.volume_ul` /
   `$row.source` in the body, every row compiled. Kills the entire CSV/`is_simulating()` machinery: the
   table lives in the versioned protocol. Example: `examples/protocols/volume_gradient.yaml`.
2. **Reservoirs and a waste container (M) — done 2026-08-26.** Wash protocols draw from troughs
   (`nest_1_reservoir_195ml`, 12-channel reservoirs) and dump supernatant into a liquid waste. Now a
   labware kind `reservoir` (any grid) usable under `Inventory.plates` and addressed like a plate, and
   `waste: true` on a reservoir: it receives anything (hazard rules still apply) and `E_WASTE_SOURCE`
   refuses to draw from it. The example world and `ztra init` carry a waste; the example world also has a
   12-channel trough of wash buffer.
3. **A timed `delay` step (S) — done 2026-08-26.** Incubations ("3 minutes on the magnet") are load-bearing
   steps. `delay {seconds, minutes}` is costed, lowers to `ctx.delay`, and is accepted by the vendor engine.
4. **Tip economy (M) — done 2026-08-26.** Recipes reuse tips deliberately (`return_tip()`, per-well tips
   kept across wash rounds) and replenish racks mid-run (`pause` + `reset_tipracks()`). Now `with_tip`
   (one tip for a block; a named tip returns to its position and comes out again later) with the
   one-source rule `E_TIP_CONTAMINATION`, and `replenish_tips` — the human's rack swap as an explicit,
   compiler-verified step that lowers to a pause plus `reset_tipracks()`. The vendor engine accepts both
   the return-and-re-pick and the swap (PROTOCOL.md).
5. **Position and flow control, with labware geometry (M) — done 2026-08-26.** Supernatant removal
   aspirates offset *sideways* from the bead pellet at reduced flow rate, dispenses at `well.top(-3)`, uses
   10 µL air gaps and `blow_out()`. Now optional `aspirate:` / `dispense:` / `position:` motions on
   transfer and mix (`at`, `offset_mm`, `side_mm`, `rate_ul_s`, `blow_out`) plus `air_gap_ul`, checked
   against `well_depth_mm` / `well_diameter_mm` in the catalog (`E_POSITION`) and the safe envelope
   (`E_FLOW_RATE`). The vendor engine subtracts the air gap when tracking liquid, so the vendor-sim
   cross-check stays exact (OPENTRONS_NOTES.md).
6. **The magnetic module (M) — done 2026-08-26.** `magdeck.engage(height)` / `disengage()` plus a
   module occupying a slot under a plate. Now `Deck.modules` (kind, slot, the plate it holds, engaged
   state), `engage_magnet` / `disengage_magnet` steps, and `magnetic: true` reagents that stay put while
   the magnet is up — so a supernatant removal predicts "80 µL of water to waste, 20 µL of beads stay",
   and asking for more than the supernatant is an `E_VOLUME` that says how much the magnet holds. With 3
   and 5, one full wash round compiles, lowers and runs on the vendor engine (tests/test_magnet.py).
7. **Multi-channel pipettes (L).** Every serious recipe uses `p300_multi_gen2`. Eight tips per pickup,
   row-of-rack addressing, column-wise transfers: this reaches deepest into lowering, tip accounting and
   the compiler, which is why it goes last, on top of the settled foundations.

**Acceptance test for the lot:** the cookbook's wash-step + supernatant-removal recipes, written as a ztra
protocol, compiled with a budget, run on the vendor-sim driver. When that works, ztra expresses the
cookbook's hardest real workflow end to end.

## Confirmed non-goals

- The "fewer than 8 tips" recipe edits motor current through private internals
  (`ctx._implementation._hw_manager...`); the rail-lights recipe runs a background thread inside the
  protocol. Generated code stays on the documented public API; operator attention and hardware tweaks
  belong to the driver/runtime layer.
- The cookbook's height-tracking *math* (compensation coefficients for viscous liquids) is a calibration
  concern; if it returns, it belongs in the world model as liquid properties, not in protocol code.
