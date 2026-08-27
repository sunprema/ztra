"""A pretend lab. It keeps its own copy of the world — the truth the runtime never sees
directly — and applies each PIR-L op to it with the pipettes' real-world sloppiness.
Sensors read from this copy. Faults can be injected to see what the rest of ztra does."""

from __future__ import annotations

import random
from typing import Literal

from ztra.compiler import deposit_liquid, mobile, remove_liquid, take_liquids
from ztra.driver import DriverFault, Hooks
from ztra.lower import Aspirate, Delay, Dispense, DropTip, Magnet, MixOp, ObserveL, Pause, PickUpTip, ReturnTip, Segment
from ztra.protocol import PlaceLoc, VialLoc, WellLoc
from ztra.world import World
from ztra.world.coords import WellCoord
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
        self.held: list[list[Liquid]] = []  # what each channel's tip holds
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
                top = WellCoord.parse(op.well)
                names = [op.well] if op.channels == 1 or top is None else [WellCoord(top.row + r, top.col).name for r in range(op.channels)]
                rack.used.extend(n for n in names if n not in rack.used)
                self.held = []
                log.append(f"Picking up tip from {op.well} of {op.rack}" + (f" ({op.channels} tips, the column)" if op.channels > 1 else ""))
            elif isinstance(op, Aspirate):
                self.held = []
                for well in self._channel_wells(op.labware, op.well, op.channels):
                    loc = self._loc(op.labware, well)
                    liquids = take_liquids(self.physical, loc, min(op.volume_ul, _available(self.physical, loc)))
                    remove_liquid(self.physical, loc, total_ul(liquids))
                    self.held.append(liquids)
                log.append(f"Aspirating {op.volume_ul} uL from {op.well} of {op.labware}" + (f" ({op.channels} channels)" if op.channels > 1 else ""))
            elif isinstance(op, Dispense):
                for ch, well in enumerate(self._channel_wells(op.labware, op.well, op.channels)):
                    loc = self._loc(op.labware, well)
                    in_tip = self.held[ch] if ch < len(self.held) else []
                    held = total_ul(in_tip)
                    if fault == "clog":
                        delivered = 0.0
                    else:
                        delivered = held
                        if not self.accurate:
                            pip = self._pipette(op.pipette)
                            delivered = held * (1.0 + self.bias.get(op.pipette, 0.0)) + self.rng.gauss(0.0, held * pip.random_pct / 100.0 + pip.random_ul)
                            delivered = min(max(delivered, 0.0), held)
                    if delivered > 0 and held > 0:
                        deposit_liquid(self.physical, loc, [Liquid(reagent=l.reagent, volume_ul=l.volume_ul * delivered / held) for l in in_tip])
                clogged = " (tip clogged: nothing came out)" if fault == "clog" else ""
                log.append(f"Dispensing {op.volume_ul} uL into {op.well} of {op.labware}{clogged}")
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
            elif isinstance(op, Magnet):
                m = self.physical.deck.modules.get(op.module)
                if m is not None:
                    m.engaged, m.height_mm = op.engaged, (op.height_mm if op.engaged else None)
                log.append(f"Engaging magnet of {op.module} at {op.height_mm:g} mm" if op.engaged and op.height_mm is not None else f"Disengaging magnet of {op.module}")
            hooks.on_op_done(i)
        log.append(f"[fake] segment {index} done")
        return log

    def _channel_wells(self, labware: str, well: str, channels: int) -> list[str]:
        """The wells the channels touch: one, or a column headed by `well`, or one trough well for all."""
        if channels == 1:
            return [well]
        d = self.physical.hardware.labware.get(self.physical.inventory.plates[labware].labware) if labware in self.physical.inventory.plates else None
        if d is None or d.rows == 1:
            return [well] * channels
        top = WellCoord.parse(well)
        assert top is not None
        return [WellCoord(top.row + r, top.col).name for r in range(channels)]

    def _loc(self, labware: str, well: str) -> PlaceLoc:
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


def _available(world: World, loc: PlaceLoc) -> float:
    if isinstance(loc, VialLoc):
        return world.inventory.vials[loc.vial].volume_ul
    return total_ul(mobile(world, loc))


from ztra.world.hardware import Accuracy  # noqa: E402
