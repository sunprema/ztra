"""Reservoirs and the liquid waste: troughs are plates of any grid, a waste receives
anything (hazard rules still apply) and gives nothing back, and the vendor engine
accepts the labware."""

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler import compile
from ztra.compiler_errors import CompileError
from ztra.protocol import Protocol
from ztra.viz import plate_svg
from ztra.world import Severity, World, validate
from ztra.world.inventory import total_ul
from ztra.world.summary import summary

T = "version: 1\nsteps:\n"


def err(world: World, yaml: str) -> CompileError:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(yaml))
    return e.value


def test_example_world_has_a_reservoir_and_a_waste(world: World) -> None:
    assert [i for i in validate(world) if i.severity is Severity.error] == []
    assert world.inventory.plates["WASTE"].waste and not world.inventory.plates["RES1"].waste
    s = summary(world)
    assert s["plates"]["RES1"]["mixtures"] == {"A1": "wash_buffer 100%"} and s["plates"]["WASTE"]["filled_wells"] == 0


def test_bulk_reagent_from_a_trough_into_wells_then_to_waste(world: World) -> None:
    y = T + """  - op: for_wells
    wells: [A5..D5]
    body:
      - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: $well }, volume_ul: 200 }
  - { op: transfer, from: { plate: P1, well: A5 }, to: { plate: WASTE, well: A1 }, volume_ul: 150 }
"""
    out = compile(world, Protocol.from_yaml(y))
    inv = out.outcomes[0].world.inventory
    assert total_ul(inv.plates["RES1"].wells["A1"]) == 12000 - 800
    assert total_ul(inv.plates["P1"].wells["A5"]) == 50 and total_ul(inv.plates["WASTE"].wells["A1"]) == 150
    assert out.outcomes[0].cost.reagent_consumed_ul == {}  # troughs are not stock vials; nothing is "consumed" from a bottle


def test_nothing_comes_back_out_of_the_waste(world: World) -> None:
    y = T + """  - { op: transfer, from: { vial: V_water }, to: { plate: WASTE, well: A1 }, volume_ul: 100 }
  - { op: transfer, from: { plate: WASTE, well: A1 }, to: { plate: P1, well: B5 }, volume_ul: 50 }
"""
    e = err(world, y)
    assert (e.code, e.step_path, e.resource) == ("E_WASTE_SOURCE", [1], "WASTE")
    assert err(world, T + "  - { op: mix, at: { plate: WASTE, well: A1 }, volume_ul: 50 }\n").code == "E_WASTE_SOURCE"


def test_waste_still_refuses_incompatible_hazards(world: World) -> None:
    y = T + """  - { op: transfer, from: { vial: V_hcl },  to: { plate: WASTE, well: A1 }, volume_ul: 50 }
  - { op: transfer, from: { vial: V_naoh }, to: { plate: WASTE, well: A1 }, volume_ul: 50 }
"""
    assert err(world, y).code == "E_HAZARD"


def test_reservoir_grid_and_waste_validation(world: World) -> None:
    w = world.model_copy(deep=True)
    w.inventory.plates["P1"].labware = "nest_12_reservoir_15ml"  # a plate entity may be a reservoir of any grid
    assert not any(i.code == "W_PLATE_NOT_96" for i in validate(w))
    w = world.model_copy(deep=True)
    w.inventory.plates["P1"].waste = True
    assert any(i.code == "W_WASTE_NOT_RESERVOIR" and i.severity is Severity.warning for i in validate(w))
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(T + "  - { op: transfer, from: { plate: RES1, well: B1 }, to: { plate: P1, well: B5 }, volume_ul: 50 }\n"))
    assert e.value.code == "E_COORDINATE" and "A1..A12" in e.value.expected


def test_reservoir_draws_as_troughs(world: World) -> None:
    svg = plate_svg(world, "RES1")
    assert "<rect" in svg and "wash_buffer 100%" in svg and "A12: empty" in svg and "A13" not in svg
    assert "(waste)" in plate_svg(world, "WASTE")
