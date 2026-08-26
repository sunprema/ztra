"""Position and flow control: where the tip goes in the well, how fast, air gaps and
blow-out — checked against the labware geometry and the safe envelope, carried into
the vendor code, and consistent between every engine."""

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
from ztra.lower import Aspirate, Dispense, MixOp, lower
from ztra.protocol import Protocol
from ztra.simulate import nominal_world
from ztra.viz import trace
from ztra.world import Severity, World, validate
from ztra.world.inventory import total_ul

T = "version: 1\nsteps:\n"

# the cookbook's supernatant removal: draw gently from just above the bottom, off to the
# side of the pellet, with an air gap so nothing drips; deliver from above the waste
SUPERNATANT = T + """  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A12 }, volume_ul: 200 }
  - { op: delay, minutes: 3 }
  - op: transfer
    from: { plate: P1, well: A12 }
    to: { plate: WASTE, well: A1 }
    volume_ul: 180
    aspirate: { at: bottom, offset_mm: 0.5, side_mm: -1, rate_ul_s: 20 }
    dispense: { at: top, offset_mm: -3, blow_out: true }
    air_gap_ul: 10
"""


def err(world: World, yaml: str) -> CompileError:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(yaml))
    return e.value


def test_supernatant_removal_compiles_and_lowers_with_its_motion(world: World) -> None:
    out = compile(world, Protocol.from_yaml(SUPERNATANT))
    inv = out.outcomes[0].world.inventory
    assert total_ul(inv.plates["P1"].wells["A12"]) == 20 and total_ul(inv.plates["WASTE"].wells["A1"]) == 180
    program = lower(world, out.pir)
    asp = [op for op in program.segments[0].ops if isinstance(op, Aspirate)][-1]
    disp = [op for op in program.segments[0].ops if isinstance(op, Dispense)][-1]
    assert (asp.at, asp.offset_mm, asp.side_mm, asp.rate_ul_s, asp.air_gap_ul) == ("bottom", 0.5, -1.0, 20.0, 10.0)
    assert (disp.at, disp.offset_mm, disp.blow_out, disp.air_gap_ul, disp.volume_ul) == ("top", -3.0, True, 10.0, 180.0)
    src = emit_program(world, program)[0][1]
    assert 'pip_p300_single_gen2.aspirate(180, P1["A12"].bottom(0.5).move(types.Point(x=-1, y=0, z=0)))' in src
    assert "_aspirate_rate = pip_p300_single_gen2.flow_rate.aspirate\n    pip_p300_single_gen2.flow_rate.aspirate = 20\n" in src
    assert "pip_p300_single_gen2.flow_rate.aspirate = _aspirate_rate\n    pip_p300_single_gen2.air_gap(10)\n" in src
    assert 'pip_p300_single_gen2.dispense(190, WASTE["A1"].top(-3))\n    pip_p300_single_gen2.blow_out()\n' in src
    assert "from opentrons import types" in src
    # plain transfers are untouched
    assert 'pip_p300_single_gen2.aspirate(200, RES1["A1"])' in src
    # every engine lands on the same world
    assert nominal_world(world, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(world, Protocol.from_yaml(SUPERNATANT))[-1].world.hash() == out.outcomes[0].world_hash


def test_air_gap_takes_room_in_the_tip(world: World) -> None:
    y = T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: WASTE, well: A1 }, volume_ul: 590, air_gap_ul: 10 }\n"
    out = compile(world, Protocol.from_yaml(y))
    assert out.outcomes[0].cost.aspirations == 3  # 590 over 290 uL of room, not 300
    assert sum(isinstance(op, Aspirate) for op in lower(world, out.pir).segments[0].ops) == 3
    assert compile(world, Protocol.from_yaml(y.replace(", air_gap_ul: 10", ""))).outcomes[0].cost.aspirations == 2
    e = err(world, y.replace("air_gap_ul: 10", "air_gap_ul: 300"))
    assert e.code == "E_PIPETTE_RANGE" and "air gap" in e.physical_law
    assert err(world, y.replace("air_gap_ul: 10", "air_gap_ul: -1")).code == "E_PIPETTE_RANGE"


def test_the_tip_must_stay_inside_the_well(world: World) -> None:
    base = T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A12 }, volume_ul: 100, ASPIRATE }\n"
    ok = base.replace("ASPIRATE ", "dispense: { at: bottom, offset_mm: 10 }")  # corning wells are 10.67 deep
    compile(world, Protocol.from_yaml(ok))
    for spec in [
        "dispense: { at: bottom, offset_mm: 12 }",
        "dispense: { at: bottom, offset_mm: -1 }",
        "dispense: { at: top, offset_mm: 1 }",
        "dispense: { at: top, offset_mm: -11 }",
        "dispense: { side_mm: 5 }",  # 6.86 mm across
    ]:
        e = err(world, base.replace("ASPIRATE ", spec))
        assert e.code == "E_POSITION", spec
    # a vial's geometry comes from its rack through the linker
    vial = T + "  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A12 }, volume_ul: 100, aspirate: { offset_mm: 40 } }\n"
    assert err(world, vial).code == "E_POSITION"
    assert compile(world, Protocol.from_yaml(vial.replace("offset_mm: 40", "offset_mm: 30"))).outcomes


def test_flow_rate_stays_in_the_safe_envelope(world: World) -> None:
    base = T + "  - { op: mix, at: { plate: P1, well: A1 }, volume_ul: 40, position: { rate_ul_s: RATE } }\n"
    out = compile(world, Protocol.from_yaml(base.replace("RATE", "150")))
    src = emit_program(world, lower(world, out.pir))[0][1]
    assert "flow_rate.aspirate = 150" in src and "flow_rate.dispense = 150" in src and "= _dispense_rate" in src
    e = err(world, base.replace("RATE", "500"))
    assert e.code == "E_FLOW_RATE" and "300" in e.expected
    assert err(world, base.replace("RATE", "0")).code == "E_FLOW_RATE"


def test_geometry_is_validated_and_optional(world: World) -> None:
    w = world.model_copy(deep=True)
    w.hardware.labware["corning_96_wellplate_360ul_flat"].well_depth_mm = -1
    assert any(i.code == "W_LABWARE_GEOMETRY" and i.severity is Severity.error for i in validate(w))
    w = world.model_copy(deep=True)
    w.hardware.labware["corning_96_wellplate_360ul_flat"].well_depth_mm = None
    w.hardware.labware["corning_96_wellplate_360ul_flat"].well_diameter_mm = None
    # with no geometry only the sign rules apply
    y = T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A12 }, volume_ul: 100, dispense: { at: bottom, offset_mm: 50, side_mm: 9 } }\n"
    assert compile(w, Protocol.from_yaml(y)).outcomes
    assert err(w, y.replace("offset_mm: 50", "offset_mm: -2")).code == "E_POSITION"


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_vendor_engine_accepts_the_motion_and_tracks_the_air_gap_right(world: World) -> None:
    from ztra.drivers.otsim import OpentronsSimDriver

    src = emit_program(world, lower(world, compile(world, Protocol.from_yaml(SUPERNATANT)).pir))[0][1]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "supernatant.py"
        path.write_text(src)
        r = subprocess.run([os.environ["ZTRA_OT_SIM_OT2"], str(path), "-o", "nothing"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr + "\n" + src

    w = World.load(EXAMPLES / "world")
    w.hardware.robot.api_level = "2.22"
    program = lower(w, compile(w, Protocol.from_yaml(SUPERNATANT)).pir)
    driver = OpentronsSimDriver(w)

    class _NoHooks:
        def on_observe(self, op: object, op_index: int) -> None: ...

        def on_pause(self, op: object, op_index: int) -> None: ...

        def on_op_done(self, op_index: int) -> None: ...

    try:
        driver.run_segment(w, 0, program.segments[0], "", _NoHooks())
    except DriverFault as f:
        pytest.fail(f"the vendor engine disagreed: {f.message}")
    assert total_ul(driver.physical.inventory.plates["WASTE"].wells["A1"]) == 180
