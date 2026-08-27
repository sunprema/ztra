"""PIR-H, the compiler's intermediate representation. Loops are already unrolled.
`branch` is the one structural op: a flat list can't say "do this or that depending
on a reading". Every op remembers which protocol step it came from."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import Field

from ztra.model import Strict
from ztra.protocol import Condition, Motion, PlaceLoc


class Origin(Strict):
    """Which protocol step an op came from, the loop iteration(s) it was in, and which
    wells any for_wells variables were bound to."""

    step_path: list[int] = Field(default_factory=list)
    iterations: list[int] = Field(default_factory=list)
    bindings: dict[str, str] = Field(default_factory=dict)


class Port(Strict):
    loc: PlaceLoc
    volume_ul: float


class TransformKind(str, Enum):
    thaw = "thaw"
    transfer = "transfer"
    mix = "mix"
    delay = "delay"  # time passes, nothing moves (an incubation)
    tip = "tip"  # opens or closes a shared-tip scope (with_tip)
    replenish = "replenish"  # a person swaps in a fresh tip rack
    magnet = "magnet"  # engage or disengage a magnetic module


class Transform(Strict):
    op: Literal["transform"] = "transform"
    kind: TransformKind
    inputs: list[Port]
    outputs: list[Port]
    repetitions: int | None = None
    seconds: float | None = None  # delay only
    tip_name: str | None = None  # transfer/mix inside a with_tip: use that tip instead of a fresh one
    tip_action: Literal["pick", "drop", "return"] | None = None  # tip only
    rack: str | None = None  # replenish only
    module: str | None = None  # magnet only
    engaged: bool | None = None  # magnet only
    height_mm: float | None = None  # magnet only
    aspirate: Motion | None = None  # transfer: how to draw
    dispense: Motion | None = None  # transfer: how to deliver
    air_gap_ul: float = 0.0  # transfer
    position: Motion | None = None  # mix
    gang: str | None = None  # an 8-channel step: the eight per-well transforms share a gang id
    channel: int | None = None  # 0..7 within the gang; channel 0 carries the tips and the robot action
    origin: Origin


class ObserveOp(Strict):
    op: Literal["observe"] = "observe"
    sensor: str
    entity: str  # what the sensor looks at; copied from Hardware.yaml so PIR stands on its own
    label: str
    origin: Origin


class Branch(Strict):
    op: Literal["branch"] = "branch"
    observation: str
    condition: Condition
    then: list[PirH]
    otherwise: list[PirH]
    origin: Origin


PirH = Annotated[Union[Transform, ObserveOp, Branch], Field(discriminator="op")]

Branch.model_rebuild()


def count(ops: list[PirH]) -> int:
    """Total ops, counting inside branch arms too."""
    n = 0
    for op in ops:
        n += 1
        if isinstance(op, Branch):
            n += count(op.then) + count(op.otherwise)
    return n
