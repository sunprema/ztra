"""A pretend lab. It keeps its own copy of the world — the truth the runtime never sees
directly — and applies each PIR-L op to it with the pipettes' real-world sloppiness.
Sensors read from this copy. Faults can be injected to see what the rest of ztra does."""

from __future__ import annotations

import random
from typing import Literal

from ztra.compiler import deposit_liquid, remove_liquid, take_liquids
from ztra.driver import DriverFault, Hooks
from ztra.lower import Aspirate, Delay, Dispense, DropTip, MixOp, ObserveL, Pause, PickUpTip, ReturnTip, Segment
from ztra.protocol import Loc, VialLoc, WellLoc
from ztra.world import World
from ztra.world.inventory import Liquid, ThermalState, total_ul

Fault = Literal["clog", "door_open"]


class FakeDriver:
    name = "fake"

    def __init__(self, physical: World, seed: int = 0, accurate: bool = False, faults: dict[tuple[int, int], Fault] | None = None) -> None:
        """`physical` is what the lab really looks like. `faults` maps (segment, op index) to
        what goes wrong there: a clogged tip delivers nothing; an opened door stops the run."""
        self.physical = physical.clone()
        self.rng = random.Random(seed)
        self.accurate = accurate
        self.faults = dict(faults or {})
        self.bias = {} if accurate else {p.name: self.rng.gauss(0.0, p.accuracy.systematic_pct / 100.0) for p in physical.hardware.pipettes}
        self.held: list[Liquid] = []
        self.segments_run: list[int] = []

    def run_segment(self, world: World, index: int, segment: Segment, source: str, hooks: Hooks) -> list[str]:
        self.segments_run.append(index)
        log = [f"[fake] running segment {index} ({len(segment.ops)} ops)"]
        for i, op in enumerate(segment.ops):
            fault = self.faults.get((index, i))
            if fault == "door_open":
                raise DriverFault("D_DOOR_OPEN", f"the door was opened during op {i} of segment {index}", op_index=i)
            if isinstance(op, PickUpTip):
                rack = self.physical.deck.tip_racks[op.rack]
                if op.well not in rack.used:
                    rack.used.append(op.well)
                self.held = []
                log.append(f"Picking up tip from {op.well} of {op.rack}")
            elif isinstance(op, Aspirate):
                loc = self._loc(op.labware, op.well)
                self.held = take_liquids(self.physical, loc, min(op.volume_ul, _available(self.physical, loc)))
                remove_liquid(self.physical, loc, total_ul(self.held))
                log.append(f"Aspirating {op.volume_ul} uL from {op.well} of {op.labware}")
            elif isinstance(op, Dispense):
                loc = self._loc(op.labware, op.well)
                held = total_ul(self.held)
                if fault == "clog":
                    delivered = 0.0
                    log.append(f"Dispensing {op.volume_ul} uL into {op.well} of {op.labware} (tip clogged: nothing came out)")
                else:
                    delivered = held
                    if not self.accurate:
                        pip = self._pipette(op.pipette)
                        delivered = held * (1.0 + self.bias.get(op.pipette, 0.0)) + self.rng.gauss(0.0, held * pip.random_pct / 100.0 + pip.random_ul)
                        delivered = min(max(delivered, 0.0), held)
                    log.append(f"Dispensing {op.volume_ul} uL into {op.well} of {op.labware}")
                if delivered > 0 and held > 0:
                    deposit_liquid(self.physical, loc, [Liquid(reagent=l.reagent, volume_ul=l.volume_ul * delivered / held) for l in self.held])
                self.held = []  # whatever stayed in the tip goes to the trash
            elif isinstance(op, MixOp):
                log.append(f"Mixing {op.repetitions} times with {op.volume_ul} uL at {op.well} of {op.labware}")
            elif isinstance(op, DropTip):
                self.held = []
                log.append("Dropping tip into trash")
            elif isinstance(op, ReturnTip):
                self.held = []
                log.append(f"Returning tip to {op.well} of {op.rack}")
            elif isinstance(op, Pause):
                if op.message.startswith("Thaw "):
                    vial = op.message.split()[1]
                    if vial in self.physical.inventory.vials:
                        v = self.physical.inventory.vials[vial]
                        v.state = ThermalState.thawed
                        v.freeze_thaw_cycles += 1
                if op.replenish_rack in self.physical.deck.tip_racks:
                    self.physical.deck.tip_racks[op.replenish_rack].used = []  # the person did swap the rack
                log.append(f"Pausing: {op.message}")
                hooks.on_pause(op, i)
            elif isinstance(op, ObserveL):
                log.append(f"Pausing for reading {op.label} ({op.sensor})")
                hooks.on_observe(op, i)
            elif isinstance(op, Delay):
                log.append(f"Delaying for {op.seconds:g} seconds")  # the fake lab does not actually wait
            hooks.on_op_done(i)
        log.append(f"[fake] segment {index} done")
        return log

    def _loc(self, labware: str, well: str) -> Loc:
        if labware in self.physical.inventory.plates:
            return WellLoc(plate=labware, well=well)
        for vid, link in self.physical.deck.linker.items():
            if link.rack == labware and link.well == well:
                return VialLoc(vial=vid)
        raise DriverFault("D_UNKNOWN_ADDRESS", f"nothing at {labware}:{well}")

    def _pipette(self, name: str) -> "Accuracy":
        for p in self.physical.hardware.pipettes:
            if p.name == name:
                return p.accuracy
        from ztra.world.hardware import Accuracy

        return Accuracy()


def _available(world: World, loc: Loc) -> float:
    if isinstance(loc, VialLoc):
        return world.inventory.vials[loc.vial].volume_ul
    return total_ul(world.inventory.plates[loc.plate].wells.get(loc.well, []))


from ztra.world.hardware import Accuracy  # noqa: E402
