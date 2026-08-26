# Simulator, Observation Budget & World Diff (v1)

Three pieces that turn "the compiler says it is valid" into "here is what the sensors should read, here is
what they did read, and here is what that means".

```
ztra simulate <world> <protocol> [--budget ...] [--seeds N --drift F --jitter UL --failure-rate P]
ztra diff     <world> <protocol> <telemetry.yaml> [--budget ...] [--outcome N] [--out <dir>]
ztra store commit  <branch> <protocol> --budget "sensor=scale_1,every=3"
ztra store execute <branch> --telemetry <telemetry.yaml>
```

## 1. Sensor model → readings

`Hardware.sensors` says what each sensor can see and how noisy it is. From any world the simulator can
compute the exact reading a perfect sensor would give:

| kind | `values` | how |
|---|---|---|
| `plate_mass` | `{mass_mg}` | Σ volume × reagent density over the plate's wells (or the vials in a tube rack). Tare not included. |
| `well_volume` | `{A1: …, B1: …}` | one entry per well the sensor lists, plus every well in its `columns` |
| `temperature` | `{}` | not modelled yet |

## 2. Observation budget (FR-2.7)

`--budget "sensor=scale_1,every=3,end=true,prefix=auto"` makes the compiler add `observe` steps: one after
every 3rd transfer/mix (inside branch arms too) and one at the very end, labelled `auto_1`, `auto_2`, …
Each costs the sensor's `read_time_s`; each buys a point at which a deviation can be pinned down. The budget is
stored with the intent so a rebase schedules identically.

## 3. Simulator

`simulate(world, pir, noise, seeds)` runs every path once with no noise — that is **the prediction**, and it
matches the compiler's world exactly — and then `seeds` times with noise:

| noise | meaning |
|---|---|
| `pipette_accuracy` | **what a healthy robot does**: each pipette's `accuracy` from `Hardware.yaml` — one systematic bias drawn per run (`systematic_pct`), plus scatter per dispense (`random_pct` of the volume + `random_ul`) |
| `dispense_drift` | stress test: fraction lost on every dispense (0.03 = 3 % short); the source still loses the full volume |
| `jitter_ul` | stress test: extra random error per dispense, standard deviation in µL |
| `failure_rate` | stress test: chance a transfer delivers nothing (clogged tip, missed well) |

`Noise.normal()` is `pipette_accuracy` alone. The store computes every intent's expected readings with it over
30 seeded runs, so each reading's spread reflects the hardware's own tolerance. The CLI's `simulate` and the
MCP tool default to it too (`--ideal-pipettes` switches it off).

Runs are seeded, so the same inputs give the same numbers. Per path the result holds each reading's `nominal`
values, their `mean`/`std` across seeds, and counts of `failed_transfers`, `shortfalls` (source ran dry) and
`overflows` (destination full; excess lost). This is FR-3.2's stress test.

## 4. Telemetry

What the lab sends back, keyed by the protocol's observe labels ([example](../examples/telemetry/demo_short_fill.yaml)):

```yaml
readings:
  - { label: after_fill, sensor: scale_1,  values: { mass_mg: 211.6 } }
  - { label: auto_2,     sensor: camera_1, values: { A1: 101, B1: 48 } }
```

## 5. World diff

For the outcome that happened — worked out from the readings the protocol branched on, or given with
`--outcome` — every expected reading gets a verdict per metric:

- `VERIFIED_WITHIN_SENSOR_NOISE` — |observed − predicted| ≤ 3σ, where σ combines the sensor's noise with the
  spread of a healthy run (`sqrt(sigma² + std²)`). **This is the tolerance model** (found in U8): a scale is far
  more precise than a pipette, so with sensor σ alone every real run read `DEVIATED`. With the pipettes'
  accuracy folded in, a 2 % run is `VERIFIED` and a missing 180 µL transfer is still `DEVIATED`.
- `DEVIATED` — beyond 3σ.
- `UNOBSERVED` — no reading arrived for that label/metric.

Then a run-level call:

| classification | meaning |
|---|---|
| `ok` | something was measured, nothing deviated |
| `localized` | a specific well read off — a protocol or hardware failure you can point at |
| `systematic` | only aggregates (mass) deviated — calibration drift, or a failure in a well nobody looked at |
| `unobserved` | nothing was measured |

`can_localize` says whether any per-well sensor covered the run at all; `unaccounted` lists aggregate
deviations nobody could place; `notes` explain readings that were ignored or unexpected.

### The observed world

The diff also produces the best estimate of the world after the run, which is what `store execute` records:
start from the predicted final world; for every well that read `DEVIATED`, shift its final volume by the
same delta (steps after the reading are assumed accurate; contents scale proportionally). Aggregate
deviations cannot be placed and leave the world unchanged — they are reported, not hidden.

## 6. Not in v1

- Temperature sensors, timestamps in telemetry, sensors on tube racks other than mass.
- Noise on aspiration (only dispense is modelled) and on sensors themselves (their σ is used for judging, not simulated).
- A reading that reveals liquid in a well predicted empty: contents unknown, so the estimate leaves it empty and says so.
