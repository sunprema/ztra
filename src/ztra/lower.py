"""Lowering: PIR-H → PIR-L. This is where names become deck addresses, tips get
picked, and big volumes get split into pipette-sized cycles.

A robot can't change its mind mid-run based on a reading, so the result is a
tree of *segments*: straight runs of PIR-L that end either by halting or by
asking the runtime to decide which child segment runs next. Each path gets its
own copy of what follows a branch, so tip wells are exact on every path.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field

from ztra.compiler_errors import CompileError
from ztra.model import Strict
from ztra.pir import Branch, ObserveOp, Origin, PirH, Transform, TransformKind
from ztra.protocol import Condition, Loc, VialLoc, loc_str
from ztra.world import World
from ztra.world.deck import Deck
from ztra.world.hardware import Pipette


class PickUpTip(Strict):
    op: Literal["pick_up_tip"] = "pick_up_tip"
    pipette: str
    rack: str
    well: str
    origin: Origin


class Aspirate(Strict):
    op: Literal["aspirate"] = "aspirate"
    pipette: str
    labware: str
    well: str
    volume_ul: float
    origin: Origin


class Dispense(Strict):
    op: Literal["dispense"] = "dispense"
    pipette: str
    labware: str
    well: str
    volume_ul: float
    origin: Origin


class MixOp(Strict):
    op: Literal["mix"] = "mix"
    pipette: str
    labware: str
    well: str
    volume_ul: float
    repetitions: int
    origin: Origin


class DropTip(Strict):
    op: Literal["drop_tip"] = "drop_tip"
    pipette: str
    origin: Origin


class Pause(Strict):
    """Robot waits for a person or an instrument. Used for thawing."""

    op: Literal["pause"] = "pause"
    message: str
    origin: Origin


class ObserveL(Strict):
    """Robot waits while the telemetry service takes a reading."""

    op: Literal["observe"] = "observe"
    sensor: str
    label: str
    origin: Origin


class Delay(Strict):
    """Robot waits a fixed time on its own."""

    op: Literal["delay"] = "delay"
    seconds: float
    origin: Origin


PirL = Annotated[Union[PickUpTip, Aspirate, Dispense, MixOp, DropTip, Pause, ObserveL, Delay], Field(discriminator="op")]


class Halt(Strict):
    kind: Literal["halt"] = "halt"


class Decide(Strict):
    """Runtime checks the reading and continues with `then` or `otherwise`."""

    kind: Literal["decide"] = "decide"
    observation: str
    condition: Condition
    then: int
    otherwise: int


Next = Annotated[Union[Halt, Decide], Field(discriminator="kind")]


class Segment(Strict):
    ops: list[PirL]
    next: Next


class Program(Strict):
    """The lowered program. Segment 0 is the entry."""

    segments: list[Segment]

    def walk(self, decisions: list[bool]) -> list[int]:
        """Follow the tree with a fixed list of branch decisions; returns the segments visited."""
        path = [0]
        it = iter(decisions)
        while True:
            nxt = self.segments[path[-1]].next
            if isinstance(nxt, Halt):
                return path
            d = next(it, None)
            if d is None:
                return path
            path.append(nxt.then if d else nxt.otherwise)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def lower(world: World, pir: list[PirH]) -> Program:
    l = _Lowerer(world)
    l.build(world.deck.model_copy(deep=True), pir, [])
    return Program(segments=l.segments)


class _Lowerer:
    def __init__(self, world: World) -> None:
        self.world = world
        self.segments: list[Segment] = []

    def err(self, code: str, origin: Origin, law: str, resource: str, expected: str, actual: str, hint: str) -> CompileError:
        return CompileError(code, law, resource, expected, actual, hint, origin=origin)

    def build(self, deck: Deck, ops: list[PirH], conts: list[list[PirH]]) -> int:
        """Build one segment from `ops` (plus whatever continuations follow), then its children.
        `deck` carries tip usage along the path."""
        seg_id = len(self.segments)
        self.segments.append(Segment(ops=[], next=Halt()))
        out: list[PirL] = []
        conts = list(conts)
        while True:
            for i, op in enumerate(ops):
                if isinstance(op, Branch):
                    inner = [*conts, ops[i + 1 :]]
                    then_id = self.build(deck.model_copy(deep=True), op.then, inner)
                    else_id = self.build(deck, op.otherwise, inner)
                    self.segments[seg_id] = Segment(ops=out, next=Decide(observation=op.observation, condition=op.condition, then=then_id, otherwise=else_id))
                    return seg_id
                self.lower_op(deck, op, out)
            if not conts:
                break
            ops = conts.pop()
        self.segments[seg_id] = Segment(ops=out, next=Halt())
        return seg_id

    def lower_op(self, deck: Deck, op: PirH, out: list[PirL]) -> None:
        if isinstance(op, ObserveOp):
            out.append(ObserveL(sensor=op.sensor, label=op.label, origin=op.origin))
            return
        assert isinstance(op, Transform)
        o = op.origin
        if op.kind is TransformKind.thaw:
            out.append(Pause(message=f"Thaw {loc_str(op.inputs[0].loc)} and resume", origin=o))
        elif op.kind is TransformKind.delay:
            out.append(Delay(seconds=op.seconds or 0.0, origin=o))
        elif op.kind is TransformKind.transfer:
            vol = op.inputs[0].volume_ul
            pip, cycles = self.pipette(vol, True, o)
            src_lw, src_well = self.address(op.inputs[0].loc, o)
            dst_lw, dst_well = self.address(op.outputs[0].loc, o)
            rack, well = self.tip(deck, pip, o)
            out.append(PickUpTip(pipette=pip.name, rack=rack, well=well, origin=o))
            per = vol / cycles
            for _ in range(cycles):
                out.append(Aspirate(pipette=pip.name, labware=src_lw, well=src_well, volume_ul=per, origin=o))
                out.append(Dispense(pipette=pip.name, labware=dst_lw, well=dst_well, volume_ul=per, origin=o))
            out.append(DropTip(pipette=pip.name, origin=o))
        else:
            vol = op.inputs[0].volume_ul
            pip, _ = self.pipette(vol, False, o)
            lw, well = self.address(op.inputs[0].loc, o)
            rack, tip_well = self.tip(deck, pip, o)
            out.append(PickUpTip(pipette=pip.name, rack=rack, well=tip_well, origin=o))
            out.append(MixOp(pipette=pip.name, labware=lw, well=well, volume_ul=vol, repetitions=op.repetitions or 1, origin=o))
            out.append(DropTip(pipette=pip.name, origin=o))

    def pipette(self, vol: float, allow_split: bool, origin: Origin) -> tuple[Pipette, int]:
        found = self.world.hardware.pipette_for(vol, allow_split)
        if found is None:
            raise self.err("E_PIPETTE_RANGE", origin, "a volume must lie within some pipette's range", "pipettes", self.world.hardware.pipette_ranges(), f"{vol} uL", "change the volume or add a pipette")
        return found

    def address(self, loc: Loc, origin: Origin) -> tuple[str, str]:
        """Turn a protocol location into (labware entity, well) on the deck."""
        deck = self.world.deck
        if isinstance(loc, VialLoc):
            link = deck.linker.get(loc.vial)
            if link is None:
                raise self.err("E_UNLINKED", origin, "the robot can only reach vials with a deck address", loc.vial, "an entry in Deck.linker", "none", "add `linker: { VIAL: { rack: RACK, well: A1 } }` to Deck.yaml")
            if deck.slot_of(link.rack) is None:
                raise self.err("E_NOT_ON_DECK", origin, "labware must sit in a slot to be reachable", link.rack, "a slot in Deck.slots", "not placed", "place the tube rack in a slot")
            return link.rack, link.well
        if deck.slot_of(loc.plate) is None:
            raise self.err("E_NOT_ON_DECK", origin, "labware must sit in a slot to be reachable", loc.plate, "a slot in Deck.slots", "not placed", "place the plate in a slot")
        return loc.plate, loc.well

    def tip(self, deck: Deck, pip: Pipette, origin: Origin) -> tuple[str, str]:
        tip = deck.take_tip(self.world.hardware, pip)
        if tip is None:
            raise self.err("E_TIPS", origin, "every transfer and mix uses one tip, and tips run out", pip.name, "a free compatible tip on the deck", f"{deck.compatible_racks(pip)} compatible rack(s), all exhausted", "add or replace a tip rack in Deck.yaml")
        return tip
