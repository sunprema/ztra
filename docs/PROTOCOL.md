# Protocol Language & Compiler (v1)

A protocol is **data** (FR-2.5): a YAML/JSON document of the AST below. The compiler is an **abstract
interpreter** (FR-2.1): it unrolls the protocol into PIR-H and then *executes* it against a clone of the
world model, checking every physical precondition at every step. Example: [`examples/protocols/demo.yaml`](../examples/protocols/demo.yaml).

```
ztra compile <world_dir> <protocol.yaml> [--no-worlds]
  → { ok: true,  pir: [...], outcomes: [ { conditions, world_hash, cost, trace, world } ] }
  → { ok: false, error: { code, step_path, iterations, branch_path, physical_law, resource,
                          coordinate, expected, actual, hint, chain_of_thought } }   exit 1
```

---

## 1. AST

```yaml
version: 1
name: optional_name
steps:
  - op: thaw
    vial: V_enzyme                          # frozen → thawed, freeze_thaw_cycles += 1

  - op: transfer
    from: { vial: V_water }                 # a Loc: { vial: id } or { plate: id, well: A1 }; reservoirs are addressed as plates
    to:   { plate: P1, well: B1 }
    volume_ul: 50                           # one fresh tip per transfer
    aspirate: { at: bottom, offset_mm: 0.5, side_mm: -1, rate_ul_s: 20 }   # optional: where in the well, how fast
    dispense: { at: top, offset_mm: -3, blow_out: true }                    # optional; `at` bottom|top, offset up (+) / down (−)
    air_gap_ul: 10                          # optional: air drawn after the liquid so nothing drips

  - op: mix
    at: { plate: P1, well: B1 }
    volume_ul: 100
    repetitions: 5                          # default 3; one fresh tip
    position: { at: bottom, offset_mm: 2, rate_ul_s: 150 }   # optional, same shape as aspirate/dispense

  - op: delay                               # wait: an incubation, beads settling
    minutes: 3                              # seconds and minutes add up; must be > 0
    seconds: 0

  - op: with_tip                            # one tip for the whole body instead of one per step
    name: t_A1                              # optional: a named tip returns to its rack position and can
    body: [ ...steps... ]                   #   be picked up again later by the same name

  - op: replenish_tips                      # a person swaps in a fresh rack; the robot pauses for it
    rack: TIPS1                             # every position is free again from here on

  - op: engage_magnet                       # raise the magnet under a plate: beads pellet, and drawing
    module: MAG1                            #   from that plate now takes the supernatant and leaves them
    height_mm: 6.5                          # above the labware base; 0..22.5 on the GEN2 module
  - op: disengage_magnet
    module: MAG1

  - op: repeat                              # static bound, fully unrolled
    times: 3
    body: [ ...steps... ]

  - op: for_wells                           # once per well; $w stands for the well inside
    wells: [A2..E2, H2]                     # names, or same-column / same-row ranges
    as: w                                   # default: well  (so $well)
    body:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: $w }, volume_ul: 180 }

  - op: for_each                            # once per row of a table; $row.<column> stands for a value
    as: row                                 # default: item
    items:                                  # any columns you like; every row needs the ones the body uses
      - { well: A3, volume_ul: 20 }
      - { well: B3, volume_ul: 40 }
    body:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: $row.well }, volume_ul: $row.volume_ul }

  - op: observe                             # take a reading; the label names it
    sensor: scale_1                         # must exist in Hardware.sensors
    label: after_fill

  - op: if_observed                         # branch on an earlier observation
    observation: after_fill
    condition: { metric: mass_mg, cmp: ge, value: 215 }   # cmp: gt | ge | lt | le
    then: [ ...steps... ]
    otherwise: [ ...steps... ]              # optional
```

Unknown fields and unknown `op`s are rejected at load.

### Language rules (FR-2.6 — total and bounded)

- `repeat.times` is a literal ≥ 1; `for_wells.wells` and `for_each.items` are explicit lists. There is no `while`, no recursion, no arithmetic.
- `$name` (from `for_wells`) may appear in `well:` fields; `$name.column` (from `for_each`) may appear in
  `well:`, `vial:` and `volume_ul:` fields, and must hold a name or a number respectively. Variables must be
  bound by an enclosing loop; nested loops need different names.
- `if_observed` may only test an `observe` that appears **earlier on the same path**. A label taken inside
  one arm of a branch is not visible after the branch.
- Branching multiplies the number of paths the compiler must check; more than **64 paths** (`MAX_PATHS`)
  is `E_TOO_MANY_PATHS`.
- Vials hold a single reagent; mixtures live in plate wells (`E_MIXTURE_IN_VIAL`).
- **Motion.** Left out, the vendor defaults apply (1 mm above the well bottom, the pipette's own speed). Given,
  a position must stay inside the well — bounded by `well_depth_mm` / `well_diameter_mm` from the labware
  catalog when they are known (`E_POSITION`) — and a flow rate inside `safe_envelope.max_flow_rate_ul_s`
  (`E_FLOW_RATE`). An air gap rides in the tip with the liquid, so it takes room: transfers split into more
  cycles, and a gap as big as the pipette is `E_PIPETTE_RANGE`.
- **Tips.** Outside a `with_tip`, every transfer and mix takes a fresh tip. Inside one, the first step picks a
  tip up and the rest reuse it; a tip may only ever draw from **one location** (`E_TIP_CONTAMINATION`) and fits
  one pipette (`E_TIP_PIPETTE`). A *named* tip goes back to its rack position at the end of the block and is
  picked up again by a later block with the same name — still bound to its one source. Blocks do not nest and
  racks are not swapped inside them (`E_TIP_SCOPE`).

## 2. PIR-H

The unrolled, checkable form (ARCHITECTURE §4.4). Each op carries its `origin` — the AST `step_path` and one
iteration number per enclosing `repeat` — so errors, costs and diffs can point back at the protocol.

| Op | Fields | From |
|---|---|---|
| `transform` | `kind: thaw \| transfer \| mix`, `inputs: [{loc, volume_ul}]`, `outputs: [...]`, `repetitions?` | `thaw`, `transfer`, `mix` |
| `observe` | `sensor`, `entity` (what it observes), `label` | `observe` |
| `branch` | `observation`, `condition`, `then: [PIR-H]`, `otherwise: [PIR-H]` | `if_observed` |

`branch` is a **structural** op added to the three data ops of the original design: a flat instruction list
cannot express a data-dependent choice, and lowering needs to know where the robot pauses for a decision.
`move` exists in the design but is not emitted by v0.1 liquid handling.

## 3. Checking semantics

The checker runs PIR-H against a clone of the world. At a `branch` it **forks the world** and continues the
*rest of the protocol* separately on each arm — path-sensitive, not a pessimistic join — so a step after a
branch is checked against exactly the state each path produces. Prediction is therefore a **set of
outcomes**, one per path, each carrying:

- `conditions` — the branch decisions that lead here (`after_fill: mass_mg >= 215 => true`)
- `world` and `world_hash` — the predicted world model on that path
- `cost` — `thaws, transfers, aspirations, mixes, delays, tips_used, tip_racks_replaced, module_actions, observations, reagent_consumed_ul{}, estimated_time_s` (`tips_used` counts fresh tips: a shared or reused tip counts once)
- `trace` — the chain of thought (NFR-5.1)

An unconditional protocol has exactly one outcome. All outcomes are checked; the first violation on any path
aborts compilation with an error whose `branch_path` says which path.

Per-step transitions and checks:

| Step | Preconditions (error) | Transition |
|---|---|---|
| `thaw` | vial exists (`E_UNKNOWN_ENTITY`) | state → thawed; cycles += 1 |
| `for_wells` | wells well-formed (`E_WELL_RANGE`), non-empty, variable not already bound | body unrolled once per well with `$name` substituted |
| `for_each` | items non-empty (`E_LOOP_BOUND`), variable not already bound; every `$name.column` used exists (`E_UNBOUND_VARIABLE`) and has the right kind of value (`E_VARIABLE_TYPE`) | body unrolled once per item with `$name.column` substituted; the values land in each op's `origin.bindings` |
| `transfer` | volume > 0 and ≥ smallest pipette min (`E_PIPETTE_RANGE`); source exists, not consumed (`E_CONSUMED`), thawed if a vial (`E_STATE`), holds ≥ volume (`E_VOLUME`); destination exists (`E_UNKNOWN_ENTITY`, `E_COORDINATE`), has capacity (`E_OVERFLOW`), no incompatible hazard classes meet (`E_HAZARD`), vial destination holds the same reagent (`E_MIXTURE_IN_VIAL`); a free compatible tip exists on the deck (`E_TIPS`) | liquid moves (mixtures move proportionally); a source vial reaching 0 becomes `consumed`; one tip is marked used; stock consumption is costed |
| `mix` | volume within one pipette's range (no splitting), the well holds ≥ volume, a free tip | one tip used; contents unchanged |
| `delay` | duration > 0 (`E_DELAY`) | nothing moves; cost += the wait |
| `with_tip` | not nested (`E_TIP_SCOPE`); inside: one pipette (`E_TIP_PIPETTE`), one source (`E_TIP_CONTAMINATION`) | one tip for the body, taken at the first step; named tips are remembered (rack, well, source) and not taken again |
| `replenish_tips` | rack exists (`E_UNKNOWN_ENTITY`), not inside a `with_tip` | every position of the rack becomes free; named tips that lived there are forgotten; cost += 1 rack |
| `engage_magnet` / `disengage_magnet` | module exists (`E_UNKNOWN_ENTITY`); height within 0..22.5 mm (`E_MAGNET_HEIGHT`) | the module's `engaged` state and height change; while engaged, `magnetic` reagents in the plate it holds are not drawn by transfers or mixes (an `E_VOLUME` on such a well says how much is held by the magnet) |
| `observe` | sensor exists (`E_UNKNOWN_SENSOR`) | none; cost += `read_time_s` |
| `if_observed` | label observed earlier on this path (`E_UNKNOWN_OBSERVATION`) | fork |

Volumes above every pipette's maximum are accepted for `transfer` and costed as ⌈v / max⌉ aspiration
cycles; lowering (step 3) performs the split. Tips are allocated column-major (A1, B1, … H1, A2, …) from
placed racks compatible with the chosen pipette, in rack-id order.

Before anything else the world must validate with zero errors (`E_WORLD_INVALID`) and the protocol version
must match (`E_PROTOCOL_VERSION`).

## 4. Error codes

| Code | Physical law |
|---|---|
| `E_WORLD_INVALID` | the world model must validate before compiling |
| `E_PROTOCOL_VERSION` | protocol schema version mismatch |
| `E_LOOP_BOUND` | `repeat.times` must be ≥ 1; `for_wells.wells` / `for_each.items` must not be empty |
| `E_WELL_RANGE` | a `for_wells` item is not a well name or a same-row / same-column range |
| `E_UNBOUND_VARIABLE` | `$name` / `$name.column` used outside a loop that binds it, or naming a column the item lacks |
| `E_VARIABLE_TYPE` | a variable holds a name where a volume is needed, or a number where a well/vial name is needed |
| `E_VARIABLE_SHADOWED` | nested loops reusing a variable name |
| `E_UNKNOWN_SENSOR` | `observe` must name a sensor in `Hardware.sensors` |
| `E_UNKNOWN_OBSERVATION` | `if_observed` must test an earlier observation on the same path |
| `E_TOO_MANY_PATHS` | > `MAX_PATHS` branch paths |
| `E_UNKNOWN_ENTITY` | vial / plate does not exist |
| `E_COORDINATE` | well is not on the plate's labware |
| `E_PIPETTE_RANGE` | volume not servable by any pipette |
| `E_CONSUMED` | consumed linear resource reused |
| `E_STATE` | aspiration from a frozen vial |
| `E_VOLUME` | aspirating more than present |
| `E_OVERFLOW` | destination over labware capacity |
| `E_HAZARD` | incompatible MSDS classes meeting in one vessel |
| `E_MIXTURE_IN_VIAL` | a second reagent into a vial |
| `E_WASTE_SOURCE` | aspirating or mixing in a waste reservoir; what went in is gone |
| `E_DELAY` | a delay must last a positive time |
| `E_POSITION` | an aspirate/dispense/mix position would leave the well (offset beyond its depth, sideways beyond its radius, wrong sign) |
| `E_FLOW_RATE` | a flow rate is not positive or exceeds `safe_envelope.max_flow_rate_ul_s` |
| `E_MAGNET_HEIGHT` | an engage height outside the module's 0..22.5 mm |
| `E_TIP_SCOPE` | `with_tip` blocks do not nest; no rack swap inside one |
| `E_TIP_PIPETTE` | every step under one `with_tip` must use the same pipette |
| `E_TIP_CONTAMINATION` | a shared or named tip may only ever draw from one location |
| `E_TIPS` | no free compatible tip on the deck |

Every error carries `step_path` (AST path), `iterations` (one per enclosing `repeat`/`for_wells`/`for_each`), `bindings` on each PIR op's origin (what each variable stood for), `branch_path`,
`physical_law`, `resource`, `coordinate`, `expected`, `actual`, `hint`, and `chain_of_thought` (FR-2.4).

## 5. Pre-flight

`ztra preflight <world> <protocol> [--budget …]` (MCP: `preflight_protocol`) walks every path to the end and reports
the whole budget at once (`feasible: true|false` plus the figures) — stock per vial and per reagent (with other vials of the same reagent named), tips per
pipette against free tips on the deck, wells whose peak volume exceeds the labware, and frozen vials used without
a thaw — worst case across branch paths. Resource-related compile errors (`E_VOLUME`, `E_TIPS`, `E_OVERFLOW`,
`E_CONSUMED`, `E_STATE`) carry the same `preflight` summary, so an error at step 11 also says "short by 440 µL overall".

## 6. Cost model (NFR-5.2)

`estimated_time_s` = aspiration cycles × 12 s + tip changes × 4 s + mix repetitions × 2 s + Σ delays + Σ sensor
`read_time_s`. Constants live in `CostModel` and are placeholders until measured on the robot. Thaw time is
not modelled.

## 7. Not in v1

- Static deck clearance checks (`E_DECK`) — with lowering, step 3.
- Evaluating conditions — the compiler never decides a branch; the runtime does, from telemetry.

Implementation: `src/ztra/protocol.py`, `src/ztra/pir.py`, `src/ztra/compiler.py`.
