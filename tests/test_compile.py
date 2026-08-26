"""Compiler: the example protocol compiles; each E_* code fires on a targeted protocol."""

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler import MAX_PATHS, CompileOutput, PathOutcome, compile
from ztra.compiler_errors import CompileError
from ztra.pir import count
from ztra.protocol import Protocol
from ztra.world import World
from ztra.world.inventory import ThermalState, total_ul

T = "version: 1\nsteps:\n"


def ok(world: World, yaml: str) -> CompileOutput:
    return compile(world, Protocol.from_yaml(yaml))


def err(world: World, yaml: str) -> CompileError:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(yaml))
    return e.value


def transfer(src: str, dst: str, vol: float) -> str:
    return f"  - {{ op: transfer, from: {src}, to: {dst}, volume_ul: {vol} }}\n"


def test_demo_protocol_compiles_to_two_outcomes(world: World) -> None:
    out = compile(world, Protocol.load(EXAMPLES / "protocols/demo.yaml"))
    assert len(out.outcomes) == 2
    assert count(out.pir) == 10  # thaw + 3 transfers + transfer + mix + observe + branch + 2 arm ops
    then, els = out.outcomes
    assert then.conditions[0].holds and not els.conditions[0].holds
    def b1(o: PathOutcome) -> float:
        return total_ul(o.world.inventory.plates["P1"].wells["B1"])

    assert b1(then) == pytest.approx(85.0)
    assert b1(els) == pytest.approx(200.0)
    assert len(then.world.inventory.plates["P1"].wells["B2"]) == 2, "B2 received the water+enzyme mixture"
    enzyme = then.world.inventory.vials["V_enzyme"]
    assert (enzyme.state, enzyme.freeze_thaw_cycles) == (ThermalState.thawed, 2)
    assert then.cost.tips_used == els.cost.tips_used == 6
    assert len(then.world.deck.tip_racks["TIPS1"].used) == 2 + 6, "tips are consumed in the predicted world"
    assert then.cost.reagent_consumed_ul["water"] == 150.0
    assert els.cost.reagent_consumed_ul["water"] == 180.0
    assert then.world_hash != els.world_hash


def test_deterministic(world: World) -> None:
    p = Protocol.load(EXAMPLES / "protocols/demo.yaml")
    a = compile(world, p).to_dict()
    b = compile(World.load(EXAMPLES / "world"), p).to_dict()
    assert a == b


def test_loop_drains_vial_reports_iteration(world: World) -> None:
    e = err(world, (EXAMPLES / "protocols/bad_loop_drains_vial.yaml").read_text())
    assert (e.code, e.step_path, e.iterations, e.resource, e.actual) == ("E_VOLUME", [0, 0], [4], "V_hcl", "20 uL")
    assert len(e.chain_of_thought) == 3


def test_frozen_vial_needs_thaw(world: World) -> None:
    e = err(world, T + transfer("{ vial: V_enzyme }", "{ plate: P1, well: A2 }", 25))
    assert (e.code, e.expected, e.actual) == ("E_STATE", "thawed", "frozen")


def test_hazard_is_detected_from_accumulated_state(world: World) -> None:
    y = T + transfer("{ vial: V_hcl }", "{ plate: P1, well: C1 }", 50) + transfer("{ vial: V_water }", "{ plate: P1, well: C1 }", 50) + transfer("{ vial: V_naoh }", "{ plate: P1, well: C1 }", 50)
    e = err(world, y)
    assert (e.code, e.step_path, e.coordinate) == ("E_HAZARD", [2], "C1")


def test_overflow_and_bad_coordinate(world: World) -> None:
    assert err(world, T + transfer("{ vial: V_water }", "{ plate: P1, well: A1 }", 320)).code == "E_OVERFLOW"  # A1 has 50; cap 360
    assert err(world, T + transfer("{ vial: V_water }", "{ plate: P1, well: H13 }", 50)).code == "E_COORDINATE"
    assert err(world, T + transfer("{ vial: V_water }", "{ plate: P7, well: A1 }", 50)).code == "E_UNKNOWN_ENTITY"


def test_pipette_range_low_errors_high_splits(world: World) -> None:
    assert err(world, T + transfer("{ vial: V_water }", "{ plate: P1, well: A2 }", 5)).code == "E_PIPETTE_RANGE"
    c = ok(world, T + transfer("{ vial: V_water }", "{ plate: P1, well: A2 }", 350)).outcomes[0].cost
    assert (c.transfers, c.aspirations, c.tips_used) == (1, 2, 1)
    assert err(world, T + "  - { op: mix, at: { plate: P1, well: A1 }, volume_ul: 400 }\n").code == "E_PIPETTE_RANGE"


def test_tips_are_linear(world: World) -> None:
    rack = world.deck.tip_racks["TIPS1"]
    rack.used = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"][:94]  # two tips left
    y = T + "  - op: repeat\n    times: 3\n    body:\n    " + transfer("{ vial: V_water }", "{ plate: P1, well: A2 }", 20).replace("  - ", "  - ", 1)
    e = err(world, y)
    assert (e.code, e.iterations) == ("E_TIPS", [3])


def test_consumed_vial_cannot_be_reused(world: World) -> None:
    e = err(world, T + transfer("{ vial: V_hcl }", "{ plate: P1, well: D1 }", 200) + transfer("{ vial: V_hcl }", "{ plate: P1, well: D2 }", 20))
    assert e.code == "E_CONSUMED" and any("consumed" in line for line in e.chain_of_thought)


def test_vial_destination_rules(world: World) -> None:
    out = ok(world, T + transfer("{ vial: V_water }", "{ vial: V_water }", 50))
    assert out.outcomes[0].world.inventory.vials["V_water"].volume_ul == 1000.0
    assert err(world, T + transfer("{ vial: V_hcl }", "{ vial: V_water }", 50)).code == "E_MIXTURE_IN_VIAL"


def test_observation_rules(world: World) -> None:
    assert err(world, T + "  - { op: observe, sensor: lidar_9, label: x }\n").code == "E_UNKNOWN_SENSOR"
    assert err(world, T + "  - { op: if_observed, observation: nope, condition: { metric: mass_mg, cmp: gt, value: 1 }, then: [] }\n").code == "E_UNKNOWN_OBSERVATION"
    y = T + """  - { op: observe, sensor: scale_1, label: a }
  - op: if_observed
    observation: a
    condition: { metric: mass_mg, cmp: gt, value: 1 }
    then: [ { op: observe, sensor: camera_1, label: inner } ]
  - { op: if_observed, observation: inner, condition: { metric: volume_ul, cmp: gt, value: 1 }, then: [] }
"""
    assert err(world, y).code == "E_UNKNOWN_OBSERVATION"


def test_branches_are_checked_path_sensitively(world: World) -> None:
    y = T + """  - { op: observe, sensor: scale_1, label: a }
  - op: if_observed
    observation: a
    condition: { metric: mass_mg, cmp: gt, value: 100 }
    then:      [ { op: transfer, from: { vial: V_hcl }, to: { plate: P1, well: D1 }, volume_ul: 200 } ]
    otherwise: [ { op: transfer, from: { vial: V_hcl }, to: { plate: P1, well: D1 }, volume_ul: 20 } ]
  - { op: transfer, from: { vial: V_hcl }, to: { plate: P1, well: D2 }, volume_ul: 20 }
"""
    e = err(world, y)
    assert (e.code, e.step_path, e.branch_path) == ("E_CONSUMED", [2], ["a: mass_mg > 100 => true"])


def test_path_count_is_capped(world: World) -> None:
    y = T + "  - { op: observe, sensor: scale_1, label: a }\n"
    n = MAX_PATHS.bit_length()  # 2^n > MAX_PATHS
    y += "  - { op: if_observed, observation: a, condition: { metric: mass_mg, cmp: gt, value: 1 }, then: [], otherwise: [] }\n" * n
    assert err(world, y).code == "E_TOO_MANY_PATHS"


def test_loop_bound_and_versions_and_world_validity(world: World) -> None:
    assert err(world, T + "  - { op: repeat, times: 0, body: [] }\n").code == "E_LOOP_BOUND"
    assert err(world, "version: 2\nsteps: []\n").code == "E_PROTOCOL_VERSION"
    del world.deck.slots["12"]
    e = err(world, "version: 1\nsteps: []\n")
    assert e.code == "E_WORLD_INVALID" and "W_TRASH_MISSING" in e.actual


def test_unknown_step_fields_are_rejected() -> None:
    with pytest.raises(ValueError):
        Protocol.from_yaml(T + "  - { op: thaw, vial: V_enzyme, speed: fast }\n")
    with pytest.raises(ValueError):
        Protocol.from_yaml(T + "  - { op: teleport, vial: V_enzyme }\n")


def test_for_wells_unrolls_and_binds(world: World) -> None:
    from ztra.pir import Transform
    from ztra.protocol import WellLoc

    out = compile(world, Protocol.load(EXAMPLES / "protocols/enzyme_dilution.yaml"))
    ops = [op for op in out.pir if isinstance(op, Transform)]
    assert len(ops) == 1 + 5 * 3
    wells = [op.outputs[0].loc.well for op in ops[1:] if isinstance(op.outputs[0].loc, WellLoc)]
    assert wells == [w for w in ["A2", "B2", "C2", "D2", "E2"] for _ in range(3)]
    assert ops[4].origin.iterations == [2] and ops[4].origin.bindings == {"w": "B2"} and ops[4].origin.step_path == [1, 0]
    c = out.outcomes[0].cost
    assert (c.transfers, c.mixes, c.tips_used, c.reagent_consumed_ul["water"]) == (10, 5, 15, 900.0)
    # the same thing written out by hand compiles to the same world
    by_hand = T + "  - { op: thaw, vial: V_enzyme }\n" + "".join(
        transfer("{ vial: V_water }", f"{{ plate: P1, well: {w} }}", 180) + transfer("{ vial: V_enzyme }", f"{{ plate: P1, well: {w} }}", 20) + f"  - {{ op: mix, at: {{ plate: P1, well: {w} }}, volume_ul: 100, repetitions: 3 }}\n"
        for w in ["A2", "B2", "C2", "D2", "E2"]
    )
    assert ok(world, by_hand).outcomes[0].world_hash == out.outcomes[0].world_hash


def test_for_wells_errors_name_the_well(world: World) -> None:
    y = T + """  - op: thaw
    vial: V_enzyme
  - op: for_wells
    wells: [A2..H2]
    as: w
    body:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: $w }, volume_ul: 180 }
"""
    e = err(world, y)  # 8 x 180 > 1000
    assert (e.code, e.step_path, e.iterations) == ("E_VOLUME", [1, 0], [6])
    assert e.chain_of_thought[-1].endswith("E2 with p300_single_gen2 (1 cycle), tip TIPS1:G1"), "the 5 wells before it succeeded"
    assert err(world, y.replace("$w", "$x")).code == "E_UNBOUND_VARIABLE"
    assert err(world, y.replace("[A2..H2]", "[A2..C5]")).code == "E_WELL_RANGE"
    assert err(world, y.replace("[A2..H2]", "[A2, 2A]")).code == "E_WELL_RANGE"
    assert err(world, y.replace("[A2..H2]", "[A2, Q9]")).code == "E_COORDINATE"  # well-formed name, not on this plate
    assert err(world, y.replace("[A2..H2]", "[]")).code == "E_LOOP_BOUND"
    nested = T + """  - op: for_wells
    wells: [A3, A4]
    as: w
    body:
      - op: for_wells
        wells: [B3]
        as: w
        body:
          - { op: mix, at: { plate: P1, well: $w }, volume_ul: 20 }
"""
    assert err(world, nested).code == "E_VARIABLE_SHADOWED"
    row = T + """  - op: for_wells
    wells: [A3..A5, H12]
    body:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: $well }, volume_ul: 20 }
"""
    out = ok(world, row)
    assert sorted(out.outcomes[0].world.inventory.plates["P1"].wells) == ["A1", "A3", "A4", "A5", "H12"]
