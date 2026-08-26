"""The mixture model: stock concentrations parse, dilution scales them by volume
fraction, and the numbers stay right through a compiled serial dilution."""

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler import compile
from ztra.protocol import Protocol
from ztra.world import Severity, World, validate
from ztra.world.inventory import Liquid, composition, describe_mixture, parse_concentration


def world() -> World:
    return World.load(EXAMPLES / "world")


def test_concentration_parsing() -> None:
    for text, value, unit in [("1 M", 1.0, "M"), ("10 U/uL", 10.0, "U/uL"), ("0.9%", 0.9, "%"), ("5 mg/mL", 5.0, "mg/mL"), ("1e-3 M", 0.001, "M"), ("10 µM", 10.0, "uM")]:
        c = parse_concentration(text)
        assert c is not None and (c.value, c.unit) == (value, unit), text
    for bad in [None, "", "lots of it", "10", "M 10"]:
        assert parse_concentration(bad) is None, bad


def test_composition_scales_the_stock_concentration() -> None:
    w = world()  # enzyme_x stock is "10 U/uL"
    contents = [Liquid(reagent="water", volume_ul=180), Liquid(reagent="enzyme_x", volume_ul=20)]
    water, enzyme = composition(contents, w.inventory.reagents)
    assert water.reagent == "water" and water.fraction == pytest.approx(0.9) and water.concentration is None
    assert enzyme.fraction == pytest.approx(0.1)
    assert enzyme.dilution_from_stock == pytest.approx(10.0)
    assert enzyme.concentration is not None and enzyme.concentration.value == pytest.approx(1.0) and enzyme.concentration.unit == "U/uL"
    assert describe_mixture(contents, w.inventory.reagents) == "water 90% + enzyme_x 10% (1 U/uL, 1:10)"


def test_serial_dilution_through_the_compiler() -> None:
    """1:10 into B1, then 1:10 of that into B2 — the predicted world must say 0.1 U/uL."""
    protocol = Protocol.from_yaml(
        """
version: 1
name: serial_dilution
steps:
  - op: thaw
    vial: V_enzyme
  - { op: transfer, from: { vial: V_water },  to: { plate: P1, well: B1 }, volume_ul: 180 }
  - { op: transfer, from: { vial: V_enzyme }, to: { plate: P1, well: B1 }, volume_ul: 20 }
  - { op: mix, at: { plate: P1, well: B1 }, volume_ul: 100, repetitions: 3 }
  - { op: transfer, from: { plate: P1, well: B1 }, to: { plate: P1, well: B2 }, volume_ul: 20 }
  - { op: transfer, from: { vial: V_water },  to: { plate: P1, well: B2 }, volume_ul: 180 }
"""
    )
    w = world()
    predicted = compile(w, protocol).outcomes[0].world
    b2 = composition(predicted.inventory.plates["P1"].wells["B2"], w.inventory.reagents)
    enzyme = next(c for c in b2 if c.reagent == "enzyme_x")
    assert enzyme.volume_ul == pytest.approx(2.0)  # 20 uL of a 10% mixture
    assert enzyme.fraction == pytest.approx(0.01)
    assert enzyme.dilution_from_stock == pytest.approx(100.0)
    assert enzyme.concentration is not None and enzyme.concentration.value == pytest.approx(0.1)
    # and the source well kept its ratio when the 20 uL left
    b1 = composition(predicted.inventory.plates["P1"].wells["B1"], w.inventory.reagents)
    assert b1[0].fraction == pytest.approx(0.9) and b1[1].fraction == pytest.approx(0.1)


def test_unparseable_concentration_warns() -> None:
    w = world()
    w.inventory.reagents["enzyme_x"].concentration = "lots of it"
    issues = validate(w)
    warning = next(i for i in issues if i.code == "W_CONCENTRATION_FORMAT")
    assert warning.severity is Severity.warning and "lots of it" in warning.message


def test_summary_shows_mixtures() -> None:
    from ztra.world.summary import summary

    w = world()
    w.inventory.plates["P1"].wells["B1"] = [Liquid(reagent="water", volume_ul=180), Liquid(reagent="enzyme_x", volume_ul=20)]
    s = summary(w)
    assert s["plates"]["P1"]["mixtures"]["B1"] == "water 90% + enzyme_x 10% (1 U/uL, 1:10)"
    assert s["plates"]["P1"]["mixtures"]["A1"] == "water 100%"


def test_backend_names_a_mixture_honestly() -> None:
    from ztra.backend.opentrons import emit_program
    from ztra.compiler import compile as _compile
    from ztra.lower import lower

    w = world()
    w.inventory.plates["P1"].wells["B1"] = [Liquid(reagent="water", volume_ul=180), Liquid(reagent="enzyme_x", volume_ul=20)]
    protocol = Protocol.from_yaml("{version: 1, name: n, steps: [{op: transfer, from: {vial: V_water}, to: {plate: P1, well: B2}, volume_ul: 50}]}")
    src = emit_program(w, lower(w, _compile(w, protocol).pir))[0][1]
    assert 'name="enzyme_x+water"' in src
    assert '.load_liquid(liq_enzyme_x_water, 200)' in src
