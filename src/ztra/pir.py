"""PIR-H, the compiler's intermediate representation. Loops are already unrolled.
`branch` is the one structural op: a flat list can't say "do this or that depending
on a reading". Every op remembers which protocol step it came from."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import Field

from ztra.model import Strict
from ztra.protocol import Condition, Loc


class Origin(Strict):
    """Which protocol step an op came from, the loop iteration(s) it was in, and which
    wells any for_wells variables were bound to."""

    step_path: list[int] = Field(default_factory=list)
    iterations: list[int] = Field(default_factory=list)
    bindings: dict[str, str] = Field(default_factory=dict)


class Port(Strict):
    loc: Loc
    volume_ul: float


class TransformKind(str, Enum):
    thaw = "thaw"
    transfer = "transfer"
    mix = "mix"
    delay = "delay"  # time passes, nothing moves (an incubation)


class Transform(Strict):
    op: Literal["transform"] = "transform"
    kind: TransformKind
    inputs: list[Port]
    outputs: list[Port]
    repetitions: int | None = None
    seconds: float | None = None  # delay only
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
