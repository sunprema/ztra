"""Inventory.yaml — reagents, vials, plates and what's in them."""

import re
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
    concentration: str | None = None  # what the stock bottle says, like "1 M" or "10 U/uL"
    msds: str | None = None  # link or id of the MSDS sheet
    density_mg_per_ul: float = 1.0  # so a volume can become an expected scale reading; water is 1.0
    magnetic: bool = False  # beads: held in place while the plate sits on an engaged magnetic module


class Concentration(Strict):
    """An amount of something per volume, exactly as the stock bottle labels it.
    The unit is carried through dilution unchanged — no unit conversion happens."""

    value: float
    unit: str  # "M", "mM", "U/uL", "mg/mL", "%", ...

    def scaled(self, fraction: float) -> "Concentration":
        return Concentration(value=self.value * fraction, unit=self.unit)

    def describe(self) -> str:
        return f"{_fmt(self.value)} {self.unit}"


def parse_concentration(text: str | None) -> Concentration | None:
    """Read "10 U/uL", "1 M", "0.9%" as a number and a unit; None when it doesn't parse."""
    if not text:
        return None
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([^\d\s.].*?)\s*", text)
    if m is None:
        return None
    return Concentration(value=float(m.group(1)), unit=m.group(2).replace("µ", "u"))


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
    """A plate or a reservoir: labware with wells that hold liquid. A reservoir marked
    `waste` only receives; nothing may be drawn back out of it."""

    labware: str  # name from the labware catalog; kind plate or reservoir
    wells: dict[str, list[Liquid]] = Field(default_factory=dict)  # only non-empty wells are listed
    waste: bool = False


class Inventory(Strict):
    version: int
    reagents: dict[str, Reagent] = Field(default_factory=dict)
    vials: dict[str, Vial] = Field(default_factory=dict)
    plates: dict[str, Plate] = Field(default_factory=dict)


def total_ul(contents: list[Liquid]) -> float:
    return sum(liquid.volume_ul for liquid in contents)


class Component(Strict):
    """One reagent's share of a mixture: its volume fraction, how far it has been
    diluted from the stock, and what its concentration works out to."""

    reagent: str
    volume_ul: float
    fraction: float  # of the mixture's total volume
    dilution_from_stock: float  # 10.0 means a 1:10 dilution
    concentration: Concentration | None  # stock concentration x fraction, if the stock declares one


def composition(contents: list[Liquid], reagents: dict[str, Reagent]) -> list[Component]:
    """What a well actually holds, largest share first.

    The mixture model: liquids are volume-additive, a well is homogeneous (every
    aspiration draws the same proportions), and diluting a stock scales its labelled
    concentration by the volume fraction. Nothing reacts; hazards that must not meet
    are refused by the compiler before this can describe them."""
    total = total_ul(contents)
    if total <= 0:
        return []
    out = []
    for liquid in sorted(contents, key=lambda l: (-l.volume_ul, l.reagent)):
        fraction = liquid.volume_ul / total
        stock = reagents.get(liquid.reagent)
        conc = parse_concentration(stock.concentration) if stock else None
        out.append(
            Component(
                reagent=liquid.reagent,
                volume_ul=liquid.volume_ul,
                fraction=fraction,
                dilution_from_stock=1.0 / fraction,
                concentration=conc.scaled(fraction) if conc else None,
            )
        )
    return out


def describe_mixture(contents: list[Liquid], reagents: dict[str, Reagent]) -> str:
    """One line for a well, e.g. "water 90% + enzyme_x 10% (1 U/uL, 1:10)"."""
    parts = []
    for c in composition(contents, reagents):
        # the dilution ratio only means something for a component whose stock names a concentration
        detail = []
        if c.concentration is not None:
            detail.append(c.concentration.describe())
            if c.dilution_from_stock > 1.0 + 1e-9:
                detail.append(f"1:{_fmt(round(c.dilution_from_stock, 2))}")
        suffix = f" ({', '.join(detail)})" if detail else ""
        parts.append(f"{c.reagent} {_fmt(round(c.fraction * 100, 1))}%{suffix}")
    return " + ".join(parts)


def _fmt(v: float) -> str:
    return f"{v:g}"
