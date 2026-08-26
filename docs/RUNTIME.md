# Runtime, Drivers & Telemetry (v1, fake hardware)

The runtime is what turns an intent in the store into a run and a recorded observation. Everything that
needs hardware sits behind two small interfaces, and a fake implementation of each lets the whole loop run
today.

```
ztra run <branch> --yes [--seed N] [--accurate] [--fault SEG:OP:clog|door_open] [-m msg] [-v]
MCP: run_intent(repo, branch, approve=true, seed, faults, message)
```

## 1. The interfaces

**`Driver`** — `run_segment(world, index, segment, source, hooks)`. Runs one lowered segment to the end and
returns its run log. Calls `hooks.on_observe(op, i)` when the robot stops for a reading, `hooks.on_pause(op, i)`
when it stops for a person, `hooks.on_op_done(i)` after every op. Raises `DriverFault` to stop the run.

**`SensorAdapter`** — `read(sensor_id) -> {metric: value}`. One per instrument. The **`TelemetryService`** owns
the adapters, stamps and collects readings by label, and applies the **interlock**: a reading outside
`Hardware.safe_envelope` raises `EStop` (today: temperature sensors vs `temperature_c`).

A real Opentrons driver would upload the segment file, start the run over the robot's HTTP API, and resume
it after each reading; a real scale adapter would talk serial. Neither exists yet.

## 2. The fake lab

`FakeDriver` keeps a hidden copy of the world — the truth — and applies each PIR-L op to it with the pipettes'
accuracy spec (one bias per run, scatter per dispense), producing an Opentrons-style run log. `SimulatedSensor`
reads that hidden world and adds the sensor's σ. `FixedSensor` always answers the same thing (for tests, or to
pretend an instrument misbehaves). Faults: `clog` (a dispense delivers nothing), `door_open` (the run stops).

## 3. What the runtime guarantees

1. **Nothing is dispatched without approval.** An approver callback sees the intent and the vendor files first
   (`--yes` / `approve=true` on the fake). A "no" leaves the store untouched.
2. **At most once.** The branch must be a fast-forward of `main` and its head must be an unexecuted intent
   (the store's rules). On dispatch a journal `.ztra/runs/<intent>.json` is written; if a run crashes between
   dispatch and record, the next attempt is refused with `S_RUN_IN_PROGRESS` until a person clears it.
3. **Branches are decided from the readings.** At a `decide`, the runtime evaluates the condition on the labelled
   reading and continues with the chosen child segment.
4. **What happened is recorded, finished or not.** A completed run goes through `store.execute` with the
   telemetry (diff, observed world). An aborted run (`DriverFault`, `EStop`) is recorded with
   `status: aborted`, the reason, the readings so far, and a world computed from **the protocol steps whose every
   robot op completed** — a transfer interrupted between aspirate and dispense is not counted. The intent counts
   as executed either way; retrying means committing a new intent (which recompiles against the new `main`).

## 4. Not in v1

- Real drivers and adapters (the Opentrons HTTP run-control API is unverified; see OPENTRONS_NOTES.md).
- An `OpentronsSimDriver` that runs segments inside the vendor's own simulator and reads its tracked volumes —
  probed and found feasible at apiLevel ≥ 2.22; recorded in OPENTRONS_NOTES.md, not built.
- Pause handling for a person (`on_pause` is a no-op in the fake).
- The interlock only knows temperature; door, pressure and collision signals arrive with real hardware.
- The <50 ms telemetry latency budget is meaningless on a fake and is not measured.
