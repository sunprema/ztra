# Prototype Findings (branch `prototype/unknowns`)

**Status:** findings from throwaway prototype code (branch `prototype/unknowns`, since removed); only the conclusions here were kept.
For U3, point `ZTRA_OT_SIM_FLEX` / `ZTRA_OT_SIM_OT2` at an `opentrons_simulate` binary (see §U3).

Goal: before building v0.1, resolve the parts of REQUIREMENTS/ARCHITECTURE whose *design* is unclear
(not merely unbuilt). Each section: the question, what we built, what happened, the decision it implies.

---

## Triage of the requirements

| Bucket | Items | Action |
|---|---|---|
| Commodity — design is obvious | YAML config, seeded noise (FR-3.2), JSON error shape, hash chain (FR-1.4), MCP/REST plumbing (IF-2.x), cost model (NFR-5.2) | Build when needed, no prototype |
| Needs hardware | <50 ms telemetry loop (NFR-4.2), E-stop interlocks (NFR-3.1), SiLA2 (IF-1.2), camera/scale ingest (FR-4.2) | Design interfaces now, test on hardware later |
| **Genuinely unclear — prototyped** | U1 compiler vs simulator, U2 protocol shape, U3 PIR expressiveness, U4 vertical-git semantics, U5 world diff under sparse observation | See below |
| Decision by analysis | U6 core↔driver boundary, U7 kinematic collision detection | See below |

---

## U1 + U2 — Is the "compiler" a type system, or symbolic execution?

**Question.** FR-2.1 talks about `Thawed<Reagent>` vs `Frozen<Reagent>` and ARCH §2 about a "linear type system".
Do we need a real type checker over a language, or is it enough to *execute the protocol abstractly against
a cloned World* and check invariants at every step?

**What we built.** A protocol AST as plain data (`Thaw`, `Transfer`, `Repeat{times}`, `IfObserved`) and a
compiler that walks it against a `World` clone, producing PIR + a structured `CompileError` + a chain-of-thought trace.

**What happened.**

| Protocol | Result | Why it matters |
|---|---|---|
| A: loop of 4×60 µL from a 200 µL vial | `E_VOLUME` at `step_path [1,0]`, `iteration 4`, actual `20 uL` | Static checking works **because the loop bound is static** — we unroll |
| B: aspirate from Frozen vial | `E_STATE` expected `Thawed` actual `Frozen`, hint "insert a Thaw step" | "Stateful types" = predicates on world state, no generics needed |
| C: acid → well, water → well, base → well | `E_HAZARD` at step 2, "naoh into well containing hcl" | Hazard is only visible from *accumulated state*, never from syntax — a syntactic type checker cannot catch this |
| D: `IfObserved` branch, then reuse the vial | `E_CONSUMED` after a *pessimistic join* of both arms | Data-dependent control flow forces the compiler to fork the world and check every arm |
| E: valid protocol, compiled twice | identical PIR, identical world hash `6b6742d64afea445` | NFR-4.1 determinism falls out for free with ordered maps + no floats in hashes of ordering |

**Decisions.**
1. **The compiler *is* an abstract interpreter, not a type system.** "Linear types" and "stateful types" become
   `consumed`/`state` fields on world entities plus per-op preconditions. Drop the generics vocabulary from the
   docs; keep the *guarantees*.
2. **Protocol language must be total and bounded**: static loop counts, no unbounded `while`. Data-dependent
   branches are allowed only on `OBSERVE` results, and the compiler must check *all* arms and join pessimistically
   (min volume, OR of consumed). This is the single most important language constraint discovered.
3. **Protocol = data (JSON/YAML AST), not a DSL.** Agents produce structured output natively; a bespoke syntax
   only adds a parser to debug. A `.ztra` file can simply be YAML of this AST. A DSL can be layered later.
4. **The error shape works:** `code`, `step_path`, `iteration`, `physical_law`, `resource`, `coordinate`,
   `expected`, `actual`, `hint`, `chain_of_thought`. This satisfies FR-2.4 + NFR-5.1 as-is.
5. Compiler and simulator share the same engine. "Simulate" = compile + noise + telemetry model (see U5).
   Don't build two.

---

## U3 — Can `MOVE/TRANSFORM/OBSERVE` drive a real liquid handler?

**Question.** ARCH §4.3 defines a 3-op PIR. Is it sufficient input for a driver?

**What we built.** Lowered the same abstract PIR two ways into Opentrons Python (API 2.16) and fed both to
Opentrons' own `opentrons_simulate`, for both Flex (opentrons 9.1.1) and OT-2 (opentrons 8.8.2).

**What happened.**

| Lowering | Flex (9.1.1) | OT-2 (8.8.2) |
|---|---|---|
| naive: `TRANSFORM` → aspirate+dispense | **REJECTED** `TipNotAttachedError` | **REJECTED** `TipNotAttachedError` |
| with tip allocation (pick_up → aspirate → dispense → drop) | **ACCEPTED**, 9 run-log lines | **ACCEPTED**, 9 run-log lines |

Side finding: **the `opentrons` PyPI package ≥ 9.0 refuses OT-2 protocols outright** ("designed for an OT-2
robot… download the Opentrons-OT2 app"). OT-2 and Flex need *different* driver dependencies, not just different
labware names. Also, Flex at API ≥ 2.16 requires an explicit `load_trash_bin`.

**Decisions.**
1. **Two-level PIR.**
   - **PIR-H** (abstract, the 3 ops in the docs) — what the compiler checks and the diff engine reasons about.
   - **PIR-L** (domain-level: `PickUpTip`, `Aspirate`, `Dispense`, `DropTip`, `Mix`, `Observe`) — what drivers consume.
   The H→L lowering is where hardware-specific resources (tips, trash, deck slots) get allocated.
2. **Tips are a linear resource** just like reagents. The world model must carry tip racks and their occupancy,
   or the compiler cannot promise a protocol is runnable.
3. **The world model needs a "linker" table** `entity → (labware, slot, well)`. `V_water` means nothing to a
   driver. `Hardware.yaml` must include deck layout, not just capability ranges.
4. `OBSERVE` has **no native implementation on Opentrons** — it lowers to `ctx.pause(...)` and an external
   sensor. Telemetry (FR-4.2) is a separate side-channel, not part of the robot protocol.
5. Keep the OT-2 and Flex drivers as separate Python environments (8.x vs 9.x).

---

## U4 — What do commit / branch / merge mean physically?

**Question.** FR-1.2 wants branches for hypotheses; FR-1.3 wants linear resources. What is a commit, and what
happens when two branches consume the same vial?

**What we built.** An append-only, hash-chained DAG with two commit kinds — `Intent` (compiled steps +
predicted world snapshot) and `Observation` (telemetry + observed world snapshot) — with `branch`,
`commit_intent`, `execute`, `rebase`, `verify_chain`.

**What happened.**
```
main: V_hcl = 200 uL
hyp-A intent (150 uL)  OK   → predicts 50 uL
hyp-B intent (120 uL)  OK   → predicts 80 uL          ← both compile; consumption is virtual on branches
execute hyp-A          OK   → main = 53 uL (observed; reality dispensed short)
execute hyp-B          Err  "not a fast-forward of main; rebase first"
rebase hyp-B           Err  E_VOLUME: cannot aspirate more than is present (V_hcl)
tamper with an Observation commit → verify_chain = false
```

**Decisions.**
1. **Only `main` is real.** Branches hold `Intent` commits only; `Observation` commits exist only on `main`.
2. **Execute = fast-forward.** A branch may be executed only if its base is the current `main` head. Otherwise
   it must be **rebased = recompiled against the new physical reality**. Linear-resource conflicts then surface as
   ordinary `CompileError`s. **There is no 3-way merge, and there should not be one.**
3. Store **both** events (steps/PIR, for provenance and rebase) and **snapshots** (world hash → world, for O(1)
   checkout). Hash chain over `parent + kind` gives NFR-3.2 immutability cheaply.
4. Real git *could* store the snapshots/YAML, but git's merge semantics are wrong for this domain. Recommend a
   small purpose-built store (SQLite or content-addressed files) with git-like *vocabulary* only.
5. The observed world after execution (53 µL) diverges from the predicted one (50 µL) → every execution ends
   with a World Diff and `main` adopts the *observed* state, never the predicted one.

---

## U5 — What can a World Diff say when you can't see per-well volume?

**Question.** FR-4.3 wants predicted-vs-observed. A liquid handler doesn't expose well volumes; realistic
sensors are a plate scale (σ ≈ 0.5 mg) and maybe a camera on some wells (σ ≈ 5 µL).

**What happened** (6 × 50 µL transfers, camera on column 1 only):

| Scenario | Plate mass | Per-well | Localizable? |
|---|---|---|---|
| S1: 3% systematic under-dispense | Δ −7.9 mg → DEVIATED | all within camera noise | **No** — total is off, no well is |
| S2: clogged tip on well A2 (not camera-observed) | Δ −28.2 mg → DEVIATED | A2 UNOBSERVED | **No** |
| S3: clogged tip on well A1 (camera-observed) | Δ −28.2 mg → DEVIATED | A1 Δ −18.5 → DEVIATED | **Yes** |
| S4: compiler inserts `OBSERVE(mass)` after every transfer | — | Δ −30.2 after transfer #4 | **Yes, at +1 weighing per transfer** |

**Decisions.**
1. **The diff is only as fine as the observations.** Every well in a diff report needs a verdict of
   `VERIFIED_WITHIN_SENSOR_NOISE` / `DEVIATED` / `UNOBSERVED` plus the sensor σ. A bare "predicted vs observed"
   table is misleading.
2. **The compiler must schedule observations** ("verification budget"): the agent (or a policy) chooses how many
   `OBSERVE` checkpoints to insert, trading run time for localizability. This is a first-class compiler pass, not
   a runtime afterthought.
3. The diff engine needs the **sensor model** (which entities each sensor observes, and its σ) as part of
   `Hardware.yaml`.
4. Low-noise systematic drift (S1) is detectable in aggregate but never per-well with a camera; that is a
   calibration signal, not a protocol failure — the diff report should classify deviations as
   `systematic` vs `localized`.

---

## U6 — Core ↔ driver boundary (decision by analysis)

Options: in-process import, gRPC, or CLI + JSON on stdin/stdout.

**Decision: CLI + JSON (and JSON-RPC over stdio for MCP).** The compiler is deterministic and stateless per
call (world in, PIR/errors out), so a process boundary costs nothing, keeps the core free of the drivers'
aggressively pinned vendor dependencies, and makes the MCP server (IF-2.1) a thin wrapper. Revisit an
in-process binding only if a hot loop needs it; the <50 ms telemetry path (NFR-4.2) is a separate service
anyway and never crosses this boundary.

## U7 — Kinematic collision detection (decision by analysis)

FR-3.3 as written (trajectory-level collision checking) is a robotics project on its own. For v0.1 on
Opentrons, the vendor firmware already refuses out-of-bounds moves and the deck is a fixed slot grid.
**Decision: reduce FR-3.3 for v0.1 to static deck checks** — slot occupancy, labware height vs. pipette
clearance, tip length vs. labware depth. Re-open true kinematics only when a non-slotted robot (Hamilton, arm)
is in scope.

---

## Recommended build order for v0.1 (derived from the above)

1. **World model schema** — reagents (with MSDS class), vials, plates, **tip racks**, deck layout/linker table,
   **sensor model**. YAML on disk.
2. **Protocol AST + abstract-interpreting compiler** (U1) → PIR-H + structured errors. Bounded language only.
3. **PIR-H → PIR-L lowering** (tip allocation, slots) + **Opentrons emitter** validated by `opentrons_simulate`
   in CI (both 8.x and 9.x).
4. **Store** — intent/observation DAG with fast-forward-only execution and rebase (U4).
5. **Simulator + diff engine** — same engine as the compiler, plus noise + sensor model + observation
   scheduling (U5).
6. MCP server over the CLI (U6).
7. Hardware: telemetry side-channel, E-stop, then real robot runs.

## What is still unknown after this round

- Real sensor σ values and latency for the scale/camera you actually own (drives U5's thresholds).
- Whether agents cope with the pessimistic-join rule for `IfObserved`, or need a "declare expected branch" hint.
- Mixture semantics inside a well (we used "dominant reagent"); concentrations need a proper model.
- How SiLA2 instruments map onto PIR-L / OBSERVE.

---

## U8 — An agent driving the loop through the MCP tools (2026-08-26)

**Question.** Now that the core is Python and has an MCP server, does the agent loop need an orchestrator
(LangGraph or similar)? Four candidate failure modes: (1) bad retries on `E_*` errors, (2) long runs and
process death, (3) an enforced approval gate, (4) none — it just works.

**What we did.** Claude Code, with only the `ztra` MCP tools (no file access), was asked to dilute the enzyme
1:10 into column 2 of `examples/world`, checking mass every 3 transfers, and commit it on a branch. Caveat:
the agent was the model that wrote the system, so it knew the error codes.

**What happened** (14 tool calls, no code changes):

| step | result | recovery |
|---|---|---|
| naive 10 µL enzyme + 90 µL water × 8 wells | `E_PIPETTE_RANGE` (p300 is 20–300) | scale to 20 + 180 |
| same, no thaw | `E_STATE` V_enzyme frozen, hint "insert a thaw step" | add `thaw` |
| 8 wells | `E_VOLUME` at step 11: **water** runs out (1440 > 1000) — a mistake the agent actually made | cut to 5 wells |
| compile / simulate / branch / commit | 10 transfers, 10 tips, 4 auto observes; expected 430/650/1030/1050 mg | — |
| execute with bench telemetry ~2 % light | 4× `DEVIATED`, `systematic`, `can_localize: false`; shortfall reported as `unaccounted`, world **not** silently adjusted | — |
| plan run 2 against `main` | "one more well" → `E_VOLUME` **from the observed state** (water 100 µL left); "mix the five" → ok | — |

**Findings.**
1. (1) did not occur: three errors, three one-shot fixes from `hint`/`expected` alone. (2) and (3) do not exist as
   problems until step 7 gives us something to dispatch. (4) holds for this loop.
2. **Tolerance bug in the diff.** Expected readings carried only the sensor's σ (0.5 mg), so a perfectly normal
   2 % run read `DEVIATED` at every checkpoint. The drift is the *pipette's*, so that is where the tolerance now
   lives: `Pipette.accuracy` (systematic % per run, random % + µL per dispense), simulated over 30 seeded runs by
   the store, folded into the diff's σ. A 2 % run is now `ok`; a missing 180 µL transfer is still `DEVIATED`.
3. The language has no per-well iteration; "column 2" is hand-unrolled. Open item.
4. Stock budgeting is only discovered by compiling; a pre-flight demand-vs-stock summary would save a round trip. Open item.

**Decisions.** No orchestration framework. The MCP server plus the store's rules are the orchestration
surface. Revisit at step 7 only if a small loop over the `mcp` client cannot provide durable resume or an
approval gate before dispatch. Tolerance model fixed the same day.
