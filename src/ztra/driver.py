"""What the runtime needs from a driver: run one segment, and call back whenever the robot
stops for a reading or for a person. A real driver talks to a vendor's software; the fake
one in `drivers/fake.py` pretends to be a lab."""

from __future__ import annotations

from typing import Any, Protocol

from ztra.lower import ObserveL, Pause, Segment
from ztra.world import World


class DriverFault(Exception):
    """The robot could not carry on: a door opened, a motor stalled, the vendor software refused."""

    def __init__(self, code: str, message: str, op_index: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.op_index = op_index  # the op that was running, if known

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "op_index": self.op_index}


class Hooks(Protocol):
    def on_observe(self, op: ObserveL, op_index: int) -> None: ...
    def on_pause(self, op: Pause, op_index: int) -> None: ...
    def on_op_done(self, op_index: int) -> None: ...


class Driver(Protocol):
    name: str

    def run_segment(self, world: World, index: int, segment: Segment, source: str, hooks: Hooks) -> list[str]:
        """Run one segment start to finish. Return the run log. Raise DriverFault to abort."""
        ...
