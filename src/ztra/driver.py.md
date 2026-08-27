---
path: "src/ztra/driver.py"
summary: "Defines the Driver/Hooks contract between the runtime and whatever actually moves the robot."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: true
---

> [!WARNING]
> **Nexus desync** — explainer says "this file has no logic of its own"; `DriverFault.__init__` and `DriverFault.to_dict()` are concrete executable methods (setting fields, building a dict), not just protocol stubs.

Defines the boundary between the runtime and whatever actually moves the robot: a `Driver` runs one segment and calls back into `Hooks` whenever it hits an `OBSERVE` or a `Pause`, or finishes an op. `DriverFault` is the one way a driver can say "the run cannot continue" — door opened, motor stalled, vendor software refused — carrying a stable code and the op index it happened at, so the runtime can record exactly where things stopped.

This file has no logic of its own; it's the `Protocol`-typed contract that `drivers/fake.py` (a pretend lab) and `drivers/otsim.py` (the real vendor simulator) both implement, and the only shape `Runtime` (runtime.py) is allowed to depend on. That's what lets the same runtime code run against a fake lab today and real hardware later without changing.
