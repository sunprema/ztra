"""Inventory.yaml — reagents, vials, plates and what's in them."""

from enum import Enum

from pydantic import Field

from ztra.model import Strict


class Hazard(str, Enum):
    """Rough MSDS hazard class. Refine when real MSDS data shows up."""

    inert = "inert"
    acid = "acid"
    base = "base"
    oxidizer = "oxidizer"
    flammable = "flammable"
    toxic = "toxic"
    biohazard = "biohazard"


_INCOMPATIBLE = {
    frozenset({Hazard.acid, Hazard.base}),
    frozenset({Hazard.oxidizer, Hazard.flammable}),
}


def incompatible(a: Hazard, b: Hazard) -> bool:
    """Pairs that must never meet in one vessel."""
    return frozenset({a, b}) in _INCOMPATIBLE


class Reagent(Strict):
    hazard: Hazard
    concentration: str | None = None  # free text like "1 M"; not interpreted yet
    msds: str | None = None  # link or id of the MSDS sheet
    density_mg_per_ul: float = 1.0  # so a volume can become an expected scale reading; water is 1.0


class ThermalState(str, Enum):
    frozen = "frozen"
    thawed = "thawed"


class Vial(Strict):
    """A source tube. Once it's empty it is consumed for good."""

    reagent: str
    volume_ul: float
    state: ThermalState = ThermalState.thawed
    freeze_thaw_cycles: int = 0
    consumed: bool = False


class Liquid(Strict):
    reagent: str
    volume_ul: float


class Plate(Strict):
    labware: str  # name from the labware catalog; must be a plate
    wells: dict[str, list[Liquid]] = Field(default_factory=dict)  # only non-empty wells are listed


class Inventory(Strict):
    version: int
    reagents: dict[str, Reagent] = Field(default_factory=dict)
    vials: dict[str, Vial] = Field(default_factory=dict)
    plates: dict[str, Plate] = Field(default_factory=dict)


def total_ul(contents: list[Liquid]) -> float:
    return sum(liquid.volume_ul for liquid in contents)
