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

1. **Per-well parameter tables (S).** The two CSV recipes exist to give each well its own volume.
   `for_wells` substitutes only the well name; extend the loop to bind per-well values (well → volume, and
   later well → source). Kills the entire CSV/`is_simulating()` machinery: the table lives in the
   versioned protocol, and the compiler checks every row.
2. **Reservoirs and a waste container (M).** Wash protocols draw from troughs
   (`nest_1_reservoir_195ml`, 12-channel reservoirs) and dump supernatant into a liquid waste. The world
   model insists plates are 8×12 and has no reservoir or waste concept. New labware kinds plus linker/
   lowering addressing; prerequisite for the wash workflow.
3. **A timed `delay` step (S).** Incubations ("3 minutes on the magnet") are load-bearing steps with no
   ztra representation. Lowers to `ctx.delay`; the cost model already folds time.
4. **Tip economy (M).** Recipes reuse tips deliberately (`return_tip()`, per-well tips kept across wash
   rounds) and replenish racks mid-run (`pause` + `reset_tipracks()`). ztra is fresh-tip-only, so a real
   wash protocol would burn racks the deck cannot hold. Needs a tip-reuse strategy in lowering, and a
   `replenish_tips` step — a *human mutation of the world mid-protocol*, made explicit so the compiler can
   verify everything after it, rather than an untracked pause.
5. **Position and flow control, with labware geometry (M).** Supernatant removal aspirates offset
   *sideways* from the bead pellet (`bottom().move(Point(x=side))`) at reduced flow rate, dispenses at
   `well.top(-3)`, uses 10 µL air gaps and `blow_out()`. PIR-L ops carry only (labware, well, volume);
   the catalog lacks well diameter/depth. Adds optional op parameters and geometry fields.
6. **The magnetic module (M).** `magdeck.engage(height)` / `disengage()` plus a module occupying a slot
   under a plate. First module in the world model and PIR-L; with 3 and 5 it makes bead cleanup —
   arguably the most automated workflow in the field (NGS prep, extractions) — expressible.
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
