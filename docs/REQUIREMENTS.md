This `REQUIREMENTS.md` defines the technical and operational standards for **ztra**. It ensures that the system provides a robust "Physical Operating System" where agents can design, simulate, and execute physical work with deterministic safety.

> Revised 2026-08-25 to reconcile with [ARCHITECTURE.md](ARCHITECTURE.md) §7 after the `prototype/unknowns` round
> ([PROTOTYPE_FINDINGS.md](PROTOTYPE_FINDINGS.md)). Existing IDs are stable; amended text is marked **(amended)**,
> new requirements are marked **(new)**, and each carries the experiment tag **[U*]** that motivated it.

---

# REQUIREMENTS.md: ztra (Physical Operating System)

## 1. Project Purpose & Scope

**ztra** is a software-hardware harness that enables autonomous agents to operate in the physical world. The primary scope of version 0.1 is **Automated Liquid Handling (96-well plates)**. The system must provide a version-controlled "Source of Truth" for physical states, a compiler to validate physical intent, and a telemetry-backed execution loop.

---

## 2. Functional Requirements

### 2.1 Physical State Management (the Store — "Vertical Git")

- **FR-1.1: Versioned State.** The system shall maintain a version-controlled repository of the environment (Inventory, Deck, Hardware). **(amended)** The repository stores both *events* (protocol steps and PIR, for provenance and rebase) and *snapshots* (content-hashed world states, for O(1) checkout). **[U4]**
- **FR-1.2: Branching.** Users/Agents shall be able to create branches to test hypothetical experimental designs without affecting the `main` physical inventory record. **(amended)** Branches hold *Intent* commits only. `main` is the only branch that may hold *Observation* commits, and is the only branch that reflects reality. **There is no merge operation.** A branch is executed only as a fast-forward of `main`; a branch whose base is stale must be rebased, i.e. recompiled against the current `main`. **[U4]**
- **FR-1.3: Linear Resource Tracking.** The system must treat physical objects as non-clonable entities. If a resource is "consumed" in a branch, it must be marked as unavailable for future operations in that lineage. **(amended)** This applies to consumables of the hardware as well as reagents — in particular **pipette tips**, whose racks and occupancy are part of the world model. **[U3]**
- **FR-1.4: Provenance Logging.** Every state transition must be recorded with a cryptographic hash, linking the `Command` to the `Observed Telemetry`. **(amended)** Commits are of two kinds: `Intent` (steps + PIR + *predicted* world) and `Observation` (telemetry + *observed* world). After execution, `main` adopts the **observed** world, never the predicted one. **[U4]**

### 2.2 The ztra Compiler & Linter

- **FR-2.1: Physical State Checking.** **(amended, formerly "Physical Type Checking")** The compiler shall enforce state preconditions on every operation (e.g. aspiration requires a `Thawed` reagent; a `consumed` vial cannot be reused). These guarantees are delivered by **abstract interpretation** of the protocol against a cloned world model — the compiler executes the protocol symbolically and checks invariants at each step. No syntactic or generic type system is required or implied. **[U1]**
- **FR-2.2: Volumetric Validation.** The system shall throw a `CompileError` if a protocol requests more volume than is physically present in a source well or vial, or more than the destination's capacity (CON-2).
- **FR-2.3: Constraint Enforcement.** The linter must validate protocols against hardware limits (e.g., pipette volume range, incubator temperature ranges). **(amended)** Includes **tip-rack exhaustion** and **static deck checks** (slot occupancy, labware height vs. pipette clearance, tip length vs. labware depth). Robot-arm reach and trajectory checks are deferred to FR-3.3. **[U3, U7]**
- **FR-2.4: Agent-Readable Tracebacks.** Error messages must be structured for LLM consumption. **(amended)** The error object shall contain at least: `code` (stable identifier, e.g. `E_VOLUME`, `E_OVERFLOW`, `E_STATE`, `E_CONSUMED`, `E_HAZARD`, `E_PIPETTE_RANGE`, `E_DECK`, `E_TIPS`), `step_path` (path through the protocol AST), `iterations` (one unrolled loop pass per enclosing `repeat`), `branch_path` (branch decisions leading to the error), `physical_law`, `resource`, `coordinate`, `expected`, `actual`, `hint`, and `chain_of_thought` (NFR-5.1). **[U1]**
- **FR-2.5: Protocol as Data.** **(new)** A protocol is a structured document (YAML/JSON) conforming to the Protocol AST schema, not a bespoke textual language. The v0.1 step set is `thaw`, `transfer`, `mix`, `repeat`, `for_wells`, `observe`, `if_observed` (reference: [PROTOCOL.md](PROTOCOL.md)). **[U2]**
- **FR-2.6: Protocol Totality.** **(new)** The protocol language shall be total and bounded so that compilation can fully unroll it: loop counts are static; there is no unbounded iteration; conditional branching is permitted only on the result of an `Observe` step. The compiler shall be **path-sensitive**: at each conditional it forks the world state and checks the remainder of the protocol on every path, producing one predicted outcome per path tagged with its branch conditions; a step invalid on any path is a `CompileError` naming that path. The number of paths is capped (`E_TOO_MANY_PATHS`). *(Supersedes the pessimistic-join rule from the prototype, which cannot be sound for both under- and over-volume checks at once.)* **[U1]**
- **FR-2.7: Observation Scheduling.** **(new)** The compiler shall insert `Observe` checkpoints into the PIR according to a **verification budget** supplied at compile time (e.g. "weigh the plate after every N transfers"). The budget trades run time for the ability to localize deviations in the World Diff (FR-4.3). **[U5]**

### 2.3 Digital Twin Simulation

- **FR-3.1: Deterministic Simulation.** The system shall provide a virtual sandbox that predicts the final state of an experiment based on the PIR. **(amended)** The simulator is the **same engine** as the compiler, extended with a noise model (FR-3.2) and the sensor model (CON-5). In addition to the predicted world, it shall output the **expected reading at every scheduled `Observe`**, which is the reference the Diff Engine compares against. **[U1, U5]**
- **FR-3.2: Probabilistic Noise Injection.** The simulator must allow for "Reality Stress Testing," injecting seeded variance (e.g., ±2% volume drift, per-operation jitter) to evaluate protocol robustness across many seeds.
- **FR-3.3: Collision Avoidance.** **(amended, scope reduced for v0.1)** For v0.1 the system shall perform **static deck checks** only (see FR-2.3), relying on the vendor firmware's own bounds enforcement on a fixed slot grid. Trajectory-level kinematic collision detection is **deferred** until a non-slotted robot (e.g. Hamilton, articulated arm) is in scope. **[U7]**

### 2.4 Execution & Telemetry

- **FR-4.1: Hardware Abstraction (PIR).** The system shall emit vendor-neutral PIR commands that backends translate for specific robots. **(amended)** PIR has **two levels**: **PIR-H** (abstract: `MOVE`, `TRANSFORM`, `OBSERVE`) used by the compiler, store and diff engine; and **PIR-L** (domain-level for liquid handling: `PickUpTip`, `Aspirate`, `Dispense`, `Mix`, `DropTip`, `Observe`, with resolved labware/well) consumed by the runtime and backends. The deterministic **lowering** H→L allocates tips and trash, resolves entities through the deck linker table (CON-6), and splits transfers exceeding pipette range. Lowering runs before commit so the store records exactly what will be dispatched. **[U3]**
- **FR-4.2: Real-time Telemetry.** The system shall ingest live sensor data (scales, cameras, thermometers) during execution. **(amended)** Telemetry is owned by a **separate Telemetry Service** process built from one **sensor adapter** per instrument. An `OBSERVE` in PIR-L is **not a robot command**: on Opentrons it lowers to a `pause`, and the reading is supplied by the Telemetry Service. **[U3]**
- **FR-4.3: The World Diff.** Post-execution, the system must generate a "Diff Report" comparing the _Predicted State_ (Simulation) vs. the _Observed State_ (Reality). **(amended)** The report is only as fine-grained as the observation schedule (FR-2.7). Each observed entity shall carry `predicted`, `observed`, `delta`, `sigma`, and a verdict of `VERIFIED_WITHIN_SENSOR_NOISE`, `DEVIATED`, or `UNOBSERVED`. The run-level summary shall classify deviations as `systematic` (aggregate off, no observed entity off — a calibration signal) or `localized` (a specific entity off), and shall state explicitly whether the deviation **can be localized** given the sensors used. **[U5]**

---

## 3. Non-Functional Requirements

### 3.1 Safety & Security

- **NFR-3.1: Fail-Safe Interlocks.** Any physical command that deviates from the "Safe Operating Envelope" (defined in `Hardware.yaml`) must trigger an immediate hardware E-Stop. Interlocks live in the runtime / driver / Telemetry Service layer, not in the compiler. **(amended)** Implemented as `EStop` in the Telemetry Service (a reading outside the envelope aborts the run and records what completed); wired to a hardware stop when a real driver exists.
- **NFR-3.2: Immutability of History.** Once a physical action is "committed" to the repo, its telemetry and outcome cannot be edited or deleted. **(amended)** Each commit hash is computed over `parent ‖ commit contents`; chain verification shall detect any alteration of an earlier commit. **[U4]**

### 3.2 Reliability & Determinism

- **NFR-4.1: Deterministic Logic.** The core must ensure that identical inputs to the compiler always result in identical PIR outputs **and identical predicted-world hashes**. No wall-clock time or unordered collections in the compile path. **[U1]**
- **NFR-4.2: Low-Latency Telemetry.** The Telemetry Service must process sensor feedback within <50 ms to allow for real-time error correction during robot moves. **(amended)** This path never crosses the core's CLI boundary (IF-2.3). **[U6]**

### 3.3 Agent-Native Design (Explainability)

- **NFR-5.1: High-Fidelity Feedback.** The system must provide "Chain of Thought" logging for every compiler decision so an agent can understand _why_ a protocol failed the build. **(amended)** Delivered as the `chain_of_thought` array in every `CompileError` (FR-2.4) and available on success as a compile trace. **[U1]**
- **NFR-5.2: Cost Modeling.** The system shall provide an estimated "Economic Cost" (time, reagent, tips, wear) for a protocol before execution, computed during the same compile walk.

---

## 4. External Interface Requirements

### 4.1 Hardware Interfaces

- **IF-1.1:** Support for **OT-2 and Flex (Opentrons)** via the Python Protocol API. **(amended)** The `opentrons` package ≥ 9.0 refuses OT-2 protocols; OT-2 and Flex drivers therefore run in **separate Python environments** (`opentrons<9` and `opentrons>=9`). Every backend's generated protocol shall be validated in CI with the vendor's `opentrons_simulate`. **[U3]**
- **IF-1.2:** Support for **SiLA2** (Standard in Lab Automation) compatible instruments. Mapping onto PIR-L / `OBSERVE` is an open question (ARCHITECTURE §8).
- **IF-1.3:** Integration with MQTT/WebSockets for IoT sensor meshes, via the Telemetry Service.

### 4.2 Agent Interfaces

- **IF-2.1:** **MCP Server Integration.** **ztra** must expose its CLI and Compiler as Model Context Protocol (MCP) tools for agents like Claude. **(amended)** The MCP server (`ztra-mcp`) wraps the core in-process and returns the same JSON shapes and error codes as the CLI (IF-2.3); refusals are values, not exceptions. **[U6]**
- **IF-2.2:** REST/gRPC API for high-frequency world model queries (post-v0.1).
- **IF-2.3: CLI / JSON Boundary.** **(new)** The core exposes its operations (compile, lower, simulate, diff, store operations) as a CLI taking and returning JSON on stdin/stdout. The compiler is stateless per call; vendor drivers (in their own venvs) and the MCP server communicate with the core only through this boundary. **[U6]**

---

## 5. Domain Constraints (96-Well Liquid Handling)

- **CON-1:** The system shall assume a standard SBS-format 96-well plate grid (8x12).
- **CON-2:** Maximum volume per well is determined by the specific `Labware Definition` in the world model.
- **CON-3:** All reagents must be associated with a `Material Safety Data Sheet (MSDS)` hazard class in the world model; the compiler rejects incompatible classes meeting in one vessel, evaluated on the **accumulated** contents of the destination, not on syntax. **[U1]**
- **CON-4: Tip Racks.** **(new)** The world model shall include tip racks, their labware definition, and per-tip occupancy. A protocol that would exhaust available tips is a `CompileError` (`E_TIPS`). **[U3]**
- **CON-5: Sensor Model.** **(new)** `Hardware.yaml` shall declare, for every sensor, which entities it can observe and its noise σ (e.g. plate scale: whole-plate mass, σ ≈ 0.5 mg; camera: wells in a given column, σ ≈ 5 µL). The simulator and Diff Engine shall use this model; the actual values are to be measured on the owned instruments (ARCHITECTURE §8). **[U5]**
- **CON-6: Deck Linker Table.** **(new)** `Deck.yaml` shall map every named entity (vial, plate, tip rack) to `(labware, slot, well)`. Lowering to PIR-L fails for any entity absent from this table. **[U3]**

---

## 6. Deferred / Out of Scope for v0.1

| Item | Status | Reason |
|---|---|---|
| Trajectory-level kinematic collision detection (original FR-3.3) | Deferred | Slot-grid robots plus vendor firmware bounds make static deck checks sufficient. **[U7]** |
| Branch merging | Rejected | Reality is linear; execute = fast-forward, conflicts resolved by rebase/recompile. **[U4]** |
| Arithmetic on observed values, unbounded loops | Deferred | Breaks compile-time totality (FR-2.6). **[U1]** |
| Per-well volume diffs without a covering sensor | Not promised | Reported as `UNOBSERVED` instead. **[U5]** |
| Second implementation language for the core | Rejected 2026-08-25 | No compelling need for v0.1 (see ARCHITECTURE §6); Python with pydantic + mypy strict. Revisit only for a measured hot path behind the CLI boundary. |
| Agent orchestration framework (LangGraph or similar) | Deferred (re-confirmed by U8) | An agent drove the full loop through the MCP tools with one-shot recoveries from every error and no need for an orchestrator. Revisit only at step 7, when there is something to dispatch, and only if a small loop over the `mcp` client cannot provide durable resume / an approval gate. |
| Mixture / concentration semantics inside a well | Resolved 2026-08-26 | Wells track exact composition by volume; labelled stock concentrations dilute by volume fraction (WORLD_MODEL.md). Reaction chemistry and unit conversion stay out of scope. |
