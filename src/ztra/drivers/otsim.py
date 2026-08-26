"""A driver that runs every segment inside Opentrons' own simulator.

The vendor engine is a second, independently written model of what a healthy run
does: it validates each command and tracks liquid per well (API 2.22+). This
driver replays the segment on the accurate fake lab for reagent identities, runs
the same segment through the vendor engine in its own venv, and compares the two
at every pause and at the end. Any disagreement aborts the run — it means our
physics or lowering is wrong. Ideal pipettes, no noise, no faults: it complements
the FakeDriver rather than replacing it.

Point ZTRA_OT_SIM_OT2 / ZTRA_OT_SIM_FLEX at an `opentrons_simulate` binary in a
venv with the right vendor package; the driver runs that venv's python."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ztra.backend.opentrons import _api_at_least, emit_segment
from ztra.driver import DriverFault, Hooks
from ztra.drivers.fake import FakeDriver
from ztra.lower import ObserveL, Pause, Program, Segment
from ztra.world import World
from ztra.world.hardware import RobotModel
from ztra.world.inventory import total_ul

ENV_VARS = {RobotModel.ot2: "ZTRA_OT_SIM_OT2", RobotModel.flex: "ZTRA_OT_SIM_FLEX"}

# Runs inside the vendor venv. Executes the emitted segment file against a simulated
# protocol context, snapshotting every tracked well volume at each pause and at the
# end. Prints one JSON object on stdout; everything the vendor prints goes to stderr.
HARNESS = '''
import json, sys

spec = json.load(open(sys.argv[1]))
real_stdout = sys.stdout
sys.stdout = sys.stderr  # vendor chatter must not corrupt the JSON

out = {"snapshots": [], "final": None, "commands": [], "error": None}
try:
    from opentrons.simulate import get_protocol_api

    if spec["robot_type"] == "Flex":
        ctx = get_protocol_api(spec["api_level"], robot_type="Flex")
    else:
        ctx = get_protocol_api(spec["api_level"])

    def volumes():
        labware = {str(slot): lw for slot, lw in ctx.loaded_labwares.items()}
        result = {}
        for slot, wells in spec["wells"].items():
            lw = labware.get(slot)
            result[slot] = {}
            for well in wells:
                try:
                    result[slot][well] = float(lw[well].current_liquid_volume())
                except Exception:
                    result[slot][well] = None
        return result

    ns = {}
    exec(compile(open(spec["source"]).read(), spec["source"], "exec"), ns)
    orig_pause = ctx.pause

    def pause(msg=None):
        out["snapshots"].append(volumes())
        return orig_pause(msg)

    ctx.pause = pause
    ns["run"](ctx)
    out["final"] = volumes()
    try:
        out["commands"] = list(ctx.commands())
    except Exception:
        pass
except Exception as e:
    out["error"] = {"type": type(e).__name__, "message": str(e)}
json.dump(out, real_stdout)
'''


class OpentronsSimDriver:
    name = "otsim"

    def __init__(self, physical: World, python: str | None = None, tolerance_ul: float = 0.5) -> None:
        api = physical.hardware.robot.api_level or "2.16"
        if not _api_at_least(api, (2, 22)):
            raise DriverFault("D_API_LEVEL", f"the vendor engine tracks liquid from apiLevel 2.22; the world says {api}. Set Hardware.robot.api_level to \"2.22\" or higher")
        self.tolerance_ul = tolerance_ul
        self.python = python or self._find_python(physical.hardware.robot.model)
        self.inner = FakeDriver(physical, accurate=True)
        self.segments_run = self.inner.segments_run

    @property
    def physical(self) -> World:
        return self.inner.physical

    @staticmethod
    def _find_python(model: RobotModel) -> str:
        env = ENV_VARS[model]
        sim = os.environ.get(env)
        if not sim:
            raise DriverFault("D_NO_VENDOR_VENV", f"point {env} at an opentrons_simulate binary in a venv with the {model.value} package")
        python = Path(sim).parent / "python"
        if not python.exists():
            raise DriverFault("D_NO_VENDOR_VENV", f"{env} points at {sim} but there is no python next to it")
        return str(python)

    def run_segment(self, world: World, index: int, segment: Segment, source: str, hooks: Hooks) -> list[str]:
        # Re-emit from the current physical world, not the intent's base world: a later
        # segment must tell the vendor engine the volumes as they stand now.
        src = emit_segment(self.physical, Program(segments=[segment]), index, segment)
        vendor = self._run_vendor(src)
        if vendor["error"] is not None:
            e = vendor["error"]
            raise DriverFault("D_VENDOR_REFUSED", f"the vendor engine refused segment {index}: {e['type']}: {e['message']}")
        pauses = sum(1 for op in segment.ops if isinstance(op, (Pause, ObserveL)))
        if len(vendor["snapshots"]) != pauses:
            raise DriverFault("D_VENDOR_MISMATCH", f"segment {index}: expected {pauses} pause snapshot(s) from the vendor engine, got {len(vendor['snapshots'])}")
        sync = _CompareAtPauses(self, hooks, vendor["snapshots"], index)
        log = self.inner.run_segment(world, index, segment, source, sync)
        self._compare(vendor["final"], f"segment {index}, at the end")
        log.append(f"[otsim] vendor engine ran segment {index} ({len(vendor['commands'])} commands); volumes agree within {self.tolerance_ul} uL")
        return log

    def _run_vendor(self, source: str) -> dict[str, Any]:
        wells: dict[str, list[str]] = {}
        for slot, content in self.physical.deck.slots.items():
            if content.entity in self.physical.inventory.plates:
                plate = self.physical.inventory.plates[content.entity]
                d = self.physical.hardware.labware[plate.labware]
                wells[slot] = [f"{chr(ord('A') + r)}{c + 1}" for r in range(d.rows) for c in range(d.cols)]
            elif content.entity in self.physical.deck.tube_racks:
                rack = content.entity
                wells[slot] = sorted(link.well for link in self.physical.deck.linker.values() if link.rack == rack)
        robot = "Flex" if self.physical.hardware.robot.model is RobotModel.flex else "OT-2"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "segment.py").write_text(source)
            (Path(tmp) / "harness.py").write_text(HARNESS)
            spec = {"source": str(Path(tmp) / "segment.py"), "api_level": self.physical.hardware.robot.api_level, "robot_type": robot, "wells": wells}
            (Path(tmp) / "spec.json").write_text(json.dumps(spec))
            r = subprocess.run([self.python, str(Path(tmp) / "harness.py"), str(Path(tmp) / "spec.json")], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise DriverFault("D_VENDOR_CRASH", f"the vendor harness exited {r.returncode}: {r.stderr.strip()[-500:]}")
        try:
            result: dict[str, Any] = json.loads(r.stdout)
            return result
        except json.JSONDecodeError:
            raise DriverFault("D_VENDOR_CRASH", f"the vendor harness printed no JSON: {r.stdout[-200:]!r}") from None

    def _compare(self, volumes: dict[str, dict[str, float | None]], where: str) -> None:
        """The vendor's tracked volumes against our replay. Both are exact models of the
        same commands, so any difference beyond float noise is a bug on one side."""
        w = self.physical
        vial_at = {(link.rack, link.well): vid for vid, link in w.deck.linker.items()}
        problems = []
        for slot, per_well in volumes.items():
            entity = w.deck.slots[slot].entity
            assert entity is not None
            for well, vendor_ul in per_well.items():
                if entity in w.inventory.plates:
                    ours = total_ul(w.inventory.plates[entity].wells.get(well, []))
                else:
                    vid = vial_at.get((entity, well))
                    ours = w.inventory.vials[vid].volume_ul if vid else 0.0
                if vendor_ul is None:
                    if ours > self.tolerance_ul:
                        problems.append(f"{entity} {well}: we say {ours:g} uL, the vendor engine lost track of it")
                elif abs(vendor_ul - ours) > self.tolerance_ul:
                    problems.append(f"{entity} {well}: we say {ours:g} uL, the vendor engine says {vendor_ul:g} uL")
        if problems:
            raise DriverFault("D_VENDOR_MISMATCH", f"{where}: " + "; ".join(problems[:5]))


class _CompareAtPauses:
    """Hooks wrapper: before the runtime takes a reading, check the vendor snapshot
    for that pause against our replay, so sensors only ever read a verified world."""

    def __init__(self, driver: OpentronsSimDriver, hooks: Hooks, snapshots: list[dict[str, dict[str, float | None]]], segment: int) -> None:
        self.driver = driver
        self.hooks = hooks
        self.snapshots = snapshots
        self.segment = segment
        self.k = 0

    def _check(self) -> None:
        self.driver._compare(self.snapshots[self.k], f"segment {self.segment}, pause {self.k}")
        self.k += 1

    def on_observe(self, op: ObserveL, op_index: int) -> None:
        self._check()
        self.hooks.on_observe(op, op_index)

    def on_pause(self, op: Pause, op_index: int) -> None:
        self._check()
        self.hooks.on_pause(op, op_index)

    def on_op_done(self, op_index: int) -> None:
        self.hooks.on_op_done(op_index)
