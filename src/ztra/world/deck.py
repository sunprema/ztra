"""Deck.yaml — where things sit on the robot, which tips are used, and the address of every vial."""

from typing import Literal

from pydantic import Field

from ztra.model import Strict
from ztra.world.coords import WellCoord
from ztra.world.hardware import Hardware, Pipette


class Slot(Strict):
    """A plate, tube rack or tip rack id — or `trash: true`. One or the other, not both."""

    entity: str | None = None
    trash: bool = False


class TubeRack(Strict):
    labware: str  # name from the labware catalog; must be a tube rack


class TipRack(Strict):
    """Tips get used up one by one; `used` only grows."""

    labware: str  # name from the labware catalog; must be a tip rack
    used: list[str] = Field(default_factory=list)


class Link(Strict):
    rack: str
    well: str


class Module(Strict):
    """A module sitting in a slot with a plate on top. Only the OT-2 magnetic module so far:
    while it is engaged, magnetic reagents in the plate stay put when liquid is drawn."""

    kind: Literal["magnetic"]
    model: str = "magnetic module gen2"  # the vendor load name
    slot: str
    holds: str | None = None  # the plate on top
    engaged: bool = False
    height_mm: float | None = None  # magnet height above the labware base while engaged


class Deck(Strict):
    version: int
    slots: dict[str, Slot] = Field(default_factory=dict)  # OT-2 uses "1".."12", Flex uses "A1".."D4"
    modules: dict[str, Module] = Field(default_factory=dict)  # each names its own slot; that slot is not in `slots`
    tube_racks: dict[str, TubeRack] = Field(default_factory=dict)
    tip_racks: dict[str, TipRack] = Field(default_factory=dict)
    linker: dict[str, Link] = Field(default_factory=dict)  # where each vial is; the robot can only reach vials listed here

    def placed(self) -> list[str]:
        """Entity ids that sit in a slot, directly or on a module."""
        return [s.entity for s in self.slots.values() if s.entity is not None] + [m.holds for m in self.modules.values() if m.holds is not None]

    def slot_of(self, entity: str) -> str | None:
        for name, s in self.slots.items():
            if s.entity == entity:
                return name
        for m in self.modules.values():
            if m.holds == entity:
                return m.slot
        return None

    def module_under(self, entity: str) -> tuple[str, Module] | None:
        for mid, m in self.modules.items():
            if m.holds == entity:
                return mid, m
        return None

    def trash_slot(self) -> str | None:
        for name, s in self.slots.items():
            if s.trash:
                return name
        return None

    def take_column(self, hardware: Hardware, pipette: Pipette) -> tuple[str, str] | None:
        """Take a whole free column of tips for an 8-channel pipette and mark all of them
        used. A column with any tip missing is skipped. Returns (rack id, top well) or None."""
        placed = self.placed()
        for rack_id in sorted(self.tip_racks):
            rack = self.tip_racks[rack_id]
            if rack.labware not in pipette.tip_labware or rack_id not in placed:
                continue
            definition = hardware.labware.get(rack.labware)
            if definition is None or definition.rows < pipette.channels:
                continue
            for col in range(definition.cols):
                names = [WellCoord(row, col).name for row in range(pipette.channels)]
                if all(n not in rack.used for n in names):
                    rack.used.extend(names)
                    return rack_id, names[0]
        return None

    def take_tip(self, hardware: Hardware, pipette: Pipette) -> tuple[str, str] | None:
        """Take the next free tip this pipette can use and mark it used. Goes down each column
        first, racks in id order, only racks that sit in a slot. Returns (rack id, well) or None."""
        placed = self.placed()
        for rack_id in sorted(self.tip_racks):
            rack = self.tip_racks[rack_id]
            if rack.labware not in pipette.tip_labware or rack_id not in placed:
                continue
            definition = hardware.labware.get(rack.labware)
            if definition is None:
                return None
            for col in range(definition.cols):
                for row in range(definition.rows):
                    name = WellCoord(row, col).name
                    if name not in rack.used:
                        rack.used.append(name)
                        return rack_id, name
        return None

    def compatible_racks(self, pipette: Pipette) -> int:
        """How many placed racks this pipette could draw from at all."""
        placed = self.placed()
        return sum(1 for rack_id, r in self.tip_racks.items() if r.labware in pipette.tip_labware and rack_id in placed)
