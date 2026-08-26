"""for_each: a per-well table inside the protocol. Rows bind wells, vials and
volumes; every row is compiled; bad references fail with a named column list."""

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler import compile
from ztra.compiler_errors import CompileError
from ztra.pir import Transform
from ztra.protocol import Protocol, VialLoc, WellLoc
from ztra.world import World
from ztra.world.inventory import total_ul

T = "version: 1\nsteps:\n"


def err(world: World, yaml: str) -> CompileError:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(yaml))
    return e.value


def test_gradient_example_binds_well_and_volume_per_row(world: World) -> None:
    out = compile(world, Protocol.load(EXAMPLES / "protocols/volume_gradient.yaml"))
    ops = [op for op in out.pir if isinstance(op, Transform)]
    assert [(op.outputs[0].loc.well, op.outputs[0].volume_ul) for op in ops if isinstance(op.outputs[0].loc, WellLoc)] == [("A3", 20.0), ("B3", 40.0), ("C3", 60.0), ("D3", 80.0), ("E3", 100.0)]
    assert ops[1].origin.iterations == [2] and ops[1].origin.bindings == {"row.well": "B3", "row.volume_ul": "40"}
    wells = out.outcomes[0].world.inventory.plates["P1"].wells
    assert total_ul(wells["E3"]) == 100.0 and out.outcomes[0].cost.reagent_consumed_ul["water"] == 300.0


def test_rows_can_pick_the_source_vial_too(world: World) -> None:
    y = T + """  - { op: thaw, vial: V_enzyme }
  - op: for_each
    items:
      - { source: V_water,  well: B4, volume_ul: 180 }
      - { source: V_enzyme, well: B4, volume_ul: 20 }
    body:
      - { op: transfer, from: { vial: $item.source }, to: { plate: P1, well: $item.well }, volume_ul: $item.volume_ul }
"""
    out = compile(world, Protocol.from_yaml(y))
    srcs = [op.inputs[0].loc.vial for op in out.pir if isinstance(op, Transform) and isinstance(op.inputs[0].loc, VialLoc) and op.inputs[0].volume_ul > 0]
    assert srcs == ["V_water", "V_enzyme"]
    assert [l.reagent for l in out.outcomes[0].world.inventory.plates["P1"].wells["B4"]] == ["enzyme_x", "water"]


def test_for_each_nests_with_for_wells(world: World) -> None:
    y = T + """  - op: for_wells
    wells: [C5, C6]
    as: w
    body:
      - op: for_each
        items: [{ volume_ul: 20 }, { volume_ul: 30 }]
        body:
          - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: $w }, volume_ul: $item.volume_ul }
"""
    out = compile(world, Protocol.from_yaml(y))
    wells = out.outcomes[0].world.inventory.plates["P1"].wells
    assert total_ul(wells["C5"]) == 50.0 and total_ul(wells["C6"]) == 50.0
    last = [op for op in out.pir if isinstance(op, Transform)][-1]
    assert last.origin.iterations == [2, 2] and last.origin.bindings == {"w": "C6", "item.volume_ul": "30"}


def test_for_each_errors(world: World) -> None:
    base = T + """  - op: for_each
    items: [{ well: D1, volume_ul: 20 }]
    body:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: $item.well }, volume_ul: $item.volume_ul }
"""
    e = err(world, base.replace("$item.volume_ul", "$item.vol"))
    assert e.code == "E_UNBOUND_VARIABLE" and "$item.volume_ul" in e.expected and "$item.well" in e.expected
    e = err(world, base.replace("$item.well", "$row.well"))
    assert e.code == "E_UNBOUND_VARIABLE"
    e = err(world, base.replace("volume_ul: 20", "volume_ul: lots"))
    assert e.code == "E_VARIABLE_TYPE" and "number" in e.expected
    e = err(world, base.replace("$item.well", "$item.volume_ul"))
    assert e.code == "E_VARIABLE_TYPE"
    e = err(world, base.replace("$item.volume_ul", "twenty"))
    assert e.code == "E_VARIABLE_TYPE"
    assert err(world, base.replace("[{ well: D1, volume_ul: 20 }]", "[]")).code == "E_LOOP_BOUND"
    shadow = T + """  - op: for_wells
    wells: [D2]
    as: item
    body:
      - op: for_each
        items: [{ volume_ul: 20 }]
        body:
          - { op: mix, at: { plate: P1, well: $item }, volume_ul: $item.volume_ul }
"""
    assert err(world, shadow).code == "E_VARIABLE_SHADOWED"
    # a physical error inside a row names the row
    e = err(world, base.replace("volume_ul: 20 }", "volume_ul: 2000 }"))
    assert (e.code, e.iterations) == ("E_VOLUME", [1])
