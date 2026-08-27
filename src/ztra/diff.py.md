---
path: "src/ztra/diff.py"
summary: "The diff engine: compares simulated predictions against real telemetry and classifies any deviation."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: true
---

> [!WARNING]
> **Nexus desync** — explainer says "no deviations means `ok`"; the code only classifies as `ok` when there are zero deviations *and* at least one verified reading — if nothing was observed at all, it classifies as `unobserved` instead.

The diff engine turns "what the lab reported" and "what the simulator expected" into a verdict, per reading, and a best guess at what the world actually looks like now. It is deliberately as honest as the sensors allow: a well nothing observed doesn't get a silent pass, it gets `UNOBSERVED`, and the report says outright whether a deviation could even in principle be pinned to a specific well (`can_localize`) versus only sensed in aggregate.

`resolve_outcome()` handles the case where the protocol branched: it checks each predicted outcome's branch conditions against the labelled telemetry readings and returns the one that's consistent, or raises `D_UNRESOLVED` if the readings don't decide it — the caller must supply the reading the protocol actually branched on.

The core of `diff()` compares each expected reading (with its combined sensor + simulation-spread sigma) against the matching telemetry reading, if any: within `THRESHOLD_SIGMAS` (3σ) of prediction is `VERIFIED_WITHIN_SENSOR_NOISE`, further off is `DEVIATED`, missing is `UNOBSERVED`. The run-level `classification` then reads the entries as a whole: no deviations means `ok`; a per-well deviation means `localized` (something specific went wrong — a protocol or hardware failure); a deviation only visible in an aggregate metric like total mass, with every observed well fine, means `systematic` (calibration drift, or a failure that happened to land in a well nobody watched).

`_estimate_world()` is where the diff engine earns its place in the store: it starts from the predicted final world and, for each well that deviated, shifts that well's volume by the observed delta (assuming later steps ran as planned) — the closest approximation of reality available given a partial sensor budget. Aggregate-only deviations (a scale reading, not a per-well one) can't be placed anywhere, so they're reported separately as `unaccounted` rather than guessed at.
