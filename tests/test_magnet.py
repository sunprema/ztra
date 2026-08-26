"""The magnetic module: a module in the world model, engage/disengage steps, and the
physics that makes it matter — while the magnet is up, drawing from the plate takes
the supernatant and leaves the beads."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from ztra.backend.opentrons import emit_program
from ztra.compiler import compile, paths
from ztra.compiler_errors import CompileError
from ztra.driver import DriverFault
from ztra.lower import Magnet, lower
from ztra.protocol import Protocol
from ztra.simulate import nominal_world
from ztra.viz import deck_svg, trace
from ztra.world import Severity, World, validate
from ztra.world.hardware import RobotModel
from ztra.world.inventory import total_ul
from ztra.world.summary import summary

T = "version: 1\nsteps:\n"

# one round of a bead wash: pellet, pull the supernatant off gently, release, resuspend in buffer
WASH = T + """  - { op: engage_magnet, module: MAG1, height_mm: 6.5 }
  - { op: delay, minutes: 2 }
  - op: transfer
    from: { plate: P2, well: A1 }
    to: { plate: WASTE, well: A1 }
    volume_ul: 80
    aspirate: { offset_mm: 0.5, side_mm: -1, rate_ul_s: 20 }
    dispense: { at: top, offset_mm: -3, blow_out: true }
  - { op: disengage_magnet, module: MAG1 }
  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, well: A1 }, volume_ul: 80 }
  - { op: mix, at: { plate: P2, well: A1 }, volume_ul: 50, repetitions: 5 }
"""


def err(world: World, yaml: str) -> CompileError:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(yaml))
    return e.value


def test_module_is_in_the_world(world: World) -> None:
    assert [i for i in validate(world) if i.severity is Severity.error] == []
    assert world.deck.slot_of("P2") == "6" and "P2" in world.deck.placed()
    assert summary(world)["modules"] == {"MAG1": "magnetic in slot 6 holding P2, disengaged"}
    assert summary(world)["plates"]["P2"]["slot"] == "6"
    assert "MAG1 ▸ P2" in deck_svg(world)


def test_engaged_magnet_keeps_the_beads(world: World) -> None:
    out = compile(world, Protocol.from_yaml(WASH))
    inv = out.outcomes[0].world.inventory
    a1 = {l.reagent: l.volume_ul for l in inv.plates["P2"].wells["A1"]}
    assert a1["beads"] == pytest.approx(20.0), "the beads never left"
    assert a1["wash_buffer"] == pytest.approx(80.0) and "water" not in a1, "all the water went, buffer came"
    waste = {l.reagent: l.volume_ul for l in inv.plates["WASTE"].wells["A1"]}
    assert waste == {"water": pytest.approx(80.0)}
    assert not out.outcomes[0].world.deck.modules["MAG1"].engaged
    assert out.outcomes[0].cost.module_actions == 2
    assert "stay put" in " ".join(out.outcomes[0].trace)


def test_without_the_magnet_beads_move_with_the_liquid(world: World) -> None:
    y = T + "  - { op: transfer, from: { plate: P2, well: A1 }, to: { plate: WASTE, well: A1 }, volume_ul: 50 }\n"
    inv = compile(world, Protocol.from_yaml(y)).outcomes[0].world.inventory
    a1 = {l.reagent: l.volume_ul for l in inv.plates["P2"].wells["A1"]}
    assert a1 == {"beads": pytest.approx(10.0), "water": pytest.approx(40.0)}


def test_the_supernatant_is_all_there_is_while_pelleted(world: World) -> None:
    y = T + """  - { op: engage_magnet, module: MAG1, height_mm: 6.5 }
  - { op: transfer, from: { plate: P2, well: A1 }, to: { plate: WASTE, well: A1 }, volume_ul: 90 }
"""
    e = err(world, y)
    assert e.code == "E_VOLUME" and e.actual == "80 uL" and "20 uL of beads are held by the magnet" in e.hint


def test_magnet_errors(world: World) -> None:
    assert err(world, T + "  - { op: engage_magnet, module: MAG9, height_mm: 6.5 }\n").code == "E_UNKNOWN_ENTITY"
    e = err(world, T + "  - { op: engage_magnet, module: MAG1, height_mm: 30 }\n")
    assert e.code == "E_MAGNET_HEIGHT" and "22.5" in e.expected


def test_module_validation(world: World) -> None:
    w = world.model_copy(deep=True)
    w.hardware.robot.model = RobotModel.flex
    assert any(i.code == "W_MODULE_ROBOT" for i in validate(w))
    w = world.model_copy(deep=True)
    w.deck.modules["MAG1"].slot = "1"  # P1 is there
    assert any(i.code == "W_SLOT_MODULE_CLASH" for i in validate(w))
    w = world.model_copy(deep=True)
    w.deck.modules["MAG1"].holds = "P9"
    assert any(i.code == "W_MODULE_HOLDS_UNKNOWN" for i in validate(w))
    w = world.model_copy(deep=True)
    w.deck.modules["MAG1"].holds = "P1"
    assert any(i.code == "W_ENTITY_DUPLICATE_SLOT" for i in validate(w))


def test_lowering_and_vendor_code(world: World) -> None:
    out = compile(world, Protocol.from_yaml(WASH))
    program = lower(world, out.pir)
    mags = [op for op in program.segments[0].ops if isinstance(op, Magnet)]
    assert [(m.engaged, m.height_mm) for m in mags] == [(True, 6.5), (False, None)]
    src = emit_program(world, program)[0][1]
    assert '    MAG1 = ctx.load_module("magnetic module gen2", "6")\n    P2 = MAG1.load_labware("nest_96_wellplate_100ul_pcr_full_skirt")\n' in src
    assert "    MAG1.engage(height_from_base=6.5)\n" in src and "    MAG1.disengage()\n" in src
    assert 'ctx.load_labware("nest_96_wellplate_100ul_pcr_full_skirt"' not in src
    # every engine ends on the same world
    assert nominal_world(world, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(world, Protocol.from_yaml(WASH))[-1].world.hash() == out.outcomes[0].world_hash


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_vendor_engine_runs_the_wash_round(world: World) -> None:
    from ztra.drivers.otsim import OpentronsSimDriver

    src = emit_program(world, lower(world, compile(world, Protocol.from_yaml(WASH)).pir))[0][1]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wash.py"
        path.write_text(src)
        r = subprocess.run([os.environ["ZTRA_OT_SIM_OT2"], str(path), "-o", "nothing"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr + "\n" + src

    w = World.load(EXAMPLES / "world")
    w.hardware.robot.api_level = "2.22"
    program = lower(w, compile(w, Protocol.from_yaml(WASH)).pir)
    driver = OpentronsSimDriver(w)

    class _NoHooks:
        def on_observe(self, op: object, op_index: int) -> None: ...

        def on_pause(self, op: object, op_index: int) -> None: ...

        def on_op_done(self, op_index: int) -> None: ...

    try:
        driver.run_segment(w, 0, program.segments[0], "", _NoHooks())
    except DriverFault as f:
        pytest.fail(f"the vendor engine disagreed: {f.message}")
    # the vendor tracks totals only; ours also knows the 100 uL is 20 beads + 80 buffer
    assert total_ul(driver.physical.inventory.plates["P2"].wells["A1"]) == pytest.approx(100.0)
