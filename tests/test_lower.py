"""Lowering and the Opentrons backend. The vendor-simulator check runs only when
ZTRA_OT_SIM_OT2 / ZTRA_OT_SIM_FLEX point at an opentrons_simulate binary."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES, sim_binary
from ztra.backend.opentrons import emit_program
from ztra.compiler import compile
from ztra.compiler_errors import CompileError
from ztra.lower import Aspirate, Decide, Dispense, DropTip, Halt, ObserveL, Pause, PickUpTip, Program, lower
from ztra.protocol import Protocol
from ztra.world import World

T = "version: 1\nsteps:\n"


def program(world: World, yaml: str) -> Program:
    return lower(world, compile(world, Protocol.from_yaml(yaml)).pir)


def demo(world: World) -> Program:
    return lower(world, compile(world, Protocol.load(EXAMPLES / "protocols/demo.yaml")).pir)


def test_demo_lowers_to_three_segments(world: World) -> None:
    prog = demo(world)
    assert len(prog.segments) == 3
    s0 = prog.segments[0]
    assert isinstance(s0.next, Decide) and (s0.next.observation, s0.next.then, s0.next.otherwise) == ("after_fill", 1, 2)
    assert isinstance(prog.segments[1].next, Halt)
    assert len(s0.ops) == 1 + 12 + 4 + 3 + 1  # pause(thaw), 3 transfers x 4 ops, 1 transfer x 4, mix x 3, observe
    assert isinstance(s0.ops[0], Pause) and "V_enzyme" in s0.ops[0].message
    assert isinstance(s0.ops[-1], ObserveL) and s0.ops[-1].label == "after_fill"
    assert prog.walk([True]) == [0, 1] and prog.walk([False]) == [0, 2]


def test_vials_resolve_through_the_linker_and_tips_are_explicit(world: World) -> None:
    ops = demo(world).segments[0].ops
    assert isinstance(ops[1], PickUpTip) and (ops[1].rack, ops[1].well) == ("TIPS1", "C1")  # A1, B1 already used
    assert isinstance(ops[2], Aspirate) and (ops[2].labware, ops[2].well, ops[2].volume_ul) == ("TR1", "A1", 50.0)
    assert isinstance(ops[3], Dispense) and (ops[3].labware, ops[3].well) == ("P1", "B1")
    assert isinstance(ops[4], DropTip)
    prog = demo(world)
    for seg in (1, 2):  # both arms continue from the same tip (H1 = 6th tip after C1..G1)
        first = prog.segments[seg].ops[0]
        assert isinstance(first, PickUpTip) and first.well == "H1"


def test_big_volumes_split_into_cycles_under_one_tip(world: World) -> None:
    ops = program(world, T + "  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A2 }, volume_ul: 350 }\n").segments[0].ops
    assert len(ops) == 6  # pickup, 2x(aspirate, dispense), drop
    assert isinstance(ops[1], Aspirate) and ops[1].volume_ul == 175.0
    assert isinstance(ops[3], Aspirate) and ops[3].volume_ul == 175.0
    assert sum(isinstance(o, PickUpTip) for o in ops) == 1


def test_continuation_after_a_branch_is_copied_per_path(world: World) -> None:
    y = T + """  - { op: observe, sensor: scale_1, label: a }
  - op: if_observed
    observation: a
    condition: { metric: mass_mg, cmp: gt, value: 1 }
    then:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A2 }, volume_ul: 20 }
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A3 }, volume_ul: 20 }
    otherwise:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A2 }, volume_ul: 20 }
  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A4 }, volume_ul: 20 }
"""
    prog = program(world, y)
    tips = lambda seg: [o.well for o in prog.segments[seg].ops if isinstance(o, PickUpTip)]  # noqa: E731
    assert tips(1) == ["C1", "D1", "E1"], "then-arm + continuation"
    assert tips(2) == ["C1", "D1"], "else-arm + continuation"


def test_lowering_errors(world: World) -> None:
    y = T + "  - { op: transfer, from: { vial: V_hcl }, to: { plate: P1, well: A2 }, volume_ul: 20 }\n"
    del world.deck.linker["V_hcl"]
    with pytest.raises(CompileError) as e:
        program(world, y)
    assert e.value.code == "E_UNLINKED"
    w = World.load(EXAMPLES / "world")
    del w.deck.slots["1"]  # plate off the deck: a warning for the world, an error for lowering
    with pytest.raises(CompileError) as e:
        program(w, y)
    assert (e.value.code, e.value.resource) == ("E_NOT_ON_DECK", "P1")


def test_opentrons_python_has_the_expected_shape(world: World, world_flex: World) -> None:
    files = emit_program(world, demo(world))
    assert len(files) == 3 and files[0][0] == "segment_0.py"
    src = files[0][1]
    for needle in [
        'requirements = {"robotType": "OT-2", "apiLevel": "2.16"}',
        'P1 = ctx.load_labware("corning_96_wellplate_360ul_flat", "1")',
        'TIPS1 = ctx.load_labware("opentrons_96_tiprack_300ul", "3")',
        'pip_p300_single_gen2 = ctx.load_instrument("p300_single_gen2", "right", tip_racks=[TIPS1])',
        'liq_water = ctx.define_liquid(name="water", description=None, display_color=None)',
        'P1["A1"].load_liquid(liq_water, 50)',
        'TR1["A1"].load_liquid(liq_water, 1000)',
        'ctx.pause("Thaw V_enzyme and resume")',
        'pip_p300_single_gen2.pick_up_tip(TIPS1["C1"])',
        'pip_p300_single_gen2.aspirate(50, TR1["A1"])',
        'pip_p300_single_gen2.dispense(50, P1["B1"])',
        'pip_p300_single_gen2.mix(5, 100, P1["B1"])',
        'ctx.pause("OBSERVE after_fill: waiting for scale_1")',
        "# ends: runtime decides on 'after_fill'",
    ]:
        assert needle in src, f"missing {needle!r} in:\n{src}"
    assert "load_trash_bin" not in src, "OT-2 has a fixed trash"

    src = emit_program(world_flex, demo(world_flex))[0][1]
    assert '"robotType": "Flex"' in src
    assert 'ctx.load_trash_bin("A3")' in src
    assert 'P1 = ctx.load_labware("corning_96_wellplate_360ul_flat", "D1")' in src
    assert 'pip_flex_1channel_1000.pick_up_tip(TIPS1["A1"])' in src


@pytest.mark.parametrize("world_name,env", [("world", "ZTRA_OT_SIM_OT2"), ("world_flex", "ZTRA_OT_SIM_FLEX")])
def test_opentrons_simulate_accepts_every_segment(world_name: str, env: str) -> None:
    """Runs every generated segment through the vendor's own simulator."""
    sim = sim_binary(env)
    if sim is None:
        pytest.skip(f"{env} not set")
    w = World.load(EXAMPLES / world_name)
    with tempfile.TemporaryDirectory() as d:
        for name, src in emit_program(w, demo(w)):
            path = Path(d) / name
            path.write_text(src)
            r = subprocess.run([sim, str(path)], capture_output=True, text=True)
            assert r.returncode == 0, f"{world_name}/{name} rejected:\n{r.stdout}{r.stderr}\n--- source ---\n{src}"
