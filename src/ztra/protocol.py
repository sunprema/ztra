"""Protocol.ztra — the experiment, written as data. Loops have fixed counts and branches
can only test earlier observations, so the compiler can check everything ahead of time."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import Field, ValidationError

from ztra.model import Strict

PROTOCOL_VERSION = 1


class VialLoc(Strict):
    vial: str


class WellLoc(Strict):
    plate: str
    well: str


Loc = VialLoc | WellLoc  # where liquid is: a vial, or a well on a plate


def loc_str(loc: Loc) -> str:
    return loc.vial if isinstance(loc, VialLoc) else f"{loc.plate}:{loc.well}"


class Cmp(str, Enum):
    gt = "gt"
    ge = "ge"
    lt = "lt"
    le = "le"


class Condition(Strict):
    metric: str  # what the reading is compared as, e.g. mass_mg or volume_ul
    cmp: Cmp
    value: float

    def __str__(self) -> str:
        op = {Cmp.gt: ">", Cmp.ge: ">=", Cmp.lt: "<", Cmp.le: "<="}[self.cmp]
        return f"{self.metric} {op} {fmt(self.value)}"


class Thaw(Strict):
    """Frozen → thawed. Bumps the vial's freeze–thaw count."""

    op: Literal["thaw"]
    vial: str


class Motion(Strict):
    """Where in the well the tip goes and how fast the liquid moves. Left out, the
    vendor defaults apply (1 mm above the bottom, the pipette's standard speed)."""

    at: Literal["bottom", "top"] = "bottom"  # the reference point
    offset_mm: float | None = None  # up (+) or down (−) from it; default 1 from the bottom, −1 from the top
    side_mm: float = 0.0  # sideways, e.g. away from a bead pellet
    rate_ul_s: float | None = None  # flow rate; gentle for supernatant, fast for mixing
    blow_out: bool = False  # dispense only: push the last of it out with air


class Transfer(Strict):
    """Move liquid from one place to another, with a fresh tip."""

    op: Literal["transfer"]
    from_: Loc = Field(alias="from")
    to: Loc
    volume_ul: float | str  # a number, or `$item.column` inside a for_each
    aspirate: Motion | None = None
    dispense: Motion | None = None
    air_gap_ul: float = 0.0  # air drawn after the liquid so nothing drips on the way


class Mix(Strict):
    """Pipette up and down in place a few times."""

    op: Literal["mix"]
    at: Loc
    volume_ul: float | str
    repetitions: int = 3
    position: Motion | None = None


class Delay(Strict):
    """Wait — an incubation, a settle, beads on the magnet. Seconds and minutes add up."""

    op: Literal["delay"]
    seconds: float = 0
    minutes: float = 0


class WithTip(Strict):
    """One tip for the whole body instead of a fresh tip per step. The tip may only ever
    draw from one location (the compiler refuses cross-contamination). A named tip goes
    back to its rack position at the end and can be picked up again later by name — the
    dedicated-tip-per-well pattern of wash protocols."""

    op: Literal["with_tip"]
    name: str | None = None
    body: list[Step]


class ReplenishTips(Strict):
    """A person swaps in a fresh tip rack. Every position counts as fresh from here on;
    the robot pauses for it. Explicit, so the compiler can verify what follows."""

    op: Literal["replenish_tips"]
    rack: str


class EngageMagnet(Strict):
    """Raise the magnet under a plate: magnetic beads pellet, and from then on drawing
    liquid from that plate takes the supernatant and leaves the beads."""

    op: Literal["engage_magnet"]
    module: str
    height_mm: float  # above the labware base; the vendor allows 0..22.5 on the GEN2 module


class DisengageMagnet(Strict):
    op: Literal["disengage_magnet"]
    module: str


class Repeat(Strict):
    """Run the body a fixed number of times."""

    op: Literal["repeat"]
    times: int
    body: list[Step]


class ForWells(Strict):
    """Run the body once per well, with `$<as>` standing for the well in `well:` fields.
    Wells are listed explicitly or as a same-row / same-column range like A2..E2."""

    op: Literal["for_wells"]
    wells: list[str]
    as_: str = Field(default="well", alias="as")
    body: list[Step]


class ForEach(Strict):
    """Run the body once per item of a table. Each item is a small mapping, like
    {well: A1, volume_ul: 20}; `$<as>.<column>` stands for that item's value in
    well, vial and volume_ul fields. This is the per-well spreadsheet, kept inside
    the protocol so the compiler checks every row."""

    op: Literal["for_each"]
    items: list[dict[str, str | float]]
    as_: str = Field(default="item", alias="as")
    body: list[Step]


class Observe(Strict):
    """Take a reading with a sensor from Hardware.yaml. The label lets a later if_observed refer to it."""

    op: Literal["observe"]
    sensor: str
    label: str


class IfObserved(Strict):
    """Do one thing or another depending on an earlier reading. The compiler checks both."""

    op: Literal["if_observed"]
    observation: str
    condition: Condition
    then: list[Step]
    otherwise: list[Step] = Field(default_factory=list)


Step = Annotated[Union[Thaw, Transfer, Mix, Delay, WithTip, ReplenishTips, EngageMagnet, DisengageMagnet, Repeat, ForWells, ForEach, Observe, IfObserved], Field(discriminator="op")]

WithTip.model_rebuild()
Repeat.model_rebuild()
ForWells.model_rebuild()
ForEach.model_rebuild()
IfObserved.model_rebuild()


class Protocol(Strict):
    version: int
    name: str | None = None
    steps: list[Step]

    @staticmethod
    def from_yaml(text: str) -> Protocol:
        try:
            data = yaml.safe_load(text)
            return Protocol.model_validate(data)
        except yaml.YAMLError as e:
            raise ValueError(f"not valid YAML: {e}") from e
        except ValidationError as e:
            from ztra.world import format_validation_error

            raise ValueError(format_validation_error(e)) from e

    @staticmethod
    def load(path: Path) -> Protocol:
        try:
            text = path.read_text()
        except OSError as e:
            raise ValueError(f"{path}: {e}") from e
        try:
            return Protocol.from_yaml(text)
        except ValueError as e:
            raise ValueError(f"{path}: {e}") from e


def fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)
