"""Eight channels: a column step is checked well by well, tips come as a column, the
robot action is one, and every engine agrees — through to the 8-channel bead wash."""

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
from ztra.lower import Aspirate, Dispense, DropTip, PickUpTip, ReturnTip, lower
from ztra.preflight import preflight
from ztra.protocol import Protocol
from ztra.schedule import Budget
from ztra.simulate import nominal_world
from ztra.viz import trace
from ztra.world import World
from ztra.world.inventory import total_ul

T = "version: 1\nsteps:\n"
COL = "ABCDEFGH"


def err(world: World, yaml: str) -> CompileError:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(yaml))
    return e.value


def agree(world: World, yaml: str) -> None:
    out = compile(world, Protocol.from_yaml(yaml))
    assert nominal_world(world, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(world, Protocol.from_yaml(yaml))[-1].world.hash() == out.outcomes[0].world_hash


def test_a_column_from_a_trough(world: World) -> None:
    y = T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, column: 2 }, volume_ul: 100 }\n"
    out = compile(world, Protocol.from_yaml(y))
    c = out.outcomes[0].cost
    assert (c.transfers, c.aspirations, c.tips_used) == (1, 1, 8)
    inv = out.outcomes[0].world.inventory
    assert all(total_ul(inv.plates["P2"].wells[f"{r}2"]) == 100 for r in COL)
    assert total_ul(inv.plates["RES1"].wells["A1"]) == 12000 - 800, "eight channels each drew 100"
    used = out.outcomes[0].world.deck.tip_racks["TIPS1"].used
    assert sorted(used) == sorted(["A1", "B1"] + [f"{r}2" for r in COL]), "column 1 had two tips missing, so the whole of column 2 went"
    ops = lower(world, out.pir).segments[0].ops
    assert [type(op).__name__ for op in ops] == ["PickUpTip", "Aspirate", "Dispense", "DropTip"]
    pick, asp, disp = ops[0], ops[1], ops[2]
    assert isinstance(pick, PickUpTip) and (pick.pipette, pick.well, pick.channels) == ("p300_multi_gen2", "A2", 8)
    assert isinstance(asp, Aspirate) and (asp.labware, asp.well, asp.channels) == ("RES1", "A1", 8)
    assert isinstance(disp, Dispense) and (disp.labware, disp.well, disp.channels) == ("P2", "A2", 8)
    src = emit_program(world, lower(world, out.pir))[0][1]
    assert 'pip_p300_multi_gen2 = ctx.load_instrument("p300_multi_gen2", "left", tip_racks=[TIPS1])' in src
    assert 'pip_p300_multi_gen2.pick_up_tip(TIPS1["A2"])' in src and 'pip_p300_multi_gen2.dispense(100, P2["A2"])' in src
    agree(world, y)


def test_column_to_column_and_column_to_waste(world: World) -> None:
    y = T + """  - { op: transfer, from: { plate: P2, column: 1 }, to: { plate: P2, column: 3 }, volume_ul: 50 }
  - { op: transfer, from: { plate: P2, column: 1 }, to: { plate: WASTE, well: A1 }, volume_ul: 25 }
"""
    inv = compile(world, Protocol.from_yaml(y)).outcomes[0].world.inventory
    for r in COL:
        assert {l.reagent: l.volume_ul for l in inv.plates["P2"].wells[f"{r}3"]} == {"beads": pytest.approx(10.0), "water": pytest.approx(40.0)}
        assert total_ul(inv.plates["P2"].wells[f"{r}1"]) == pytest.approx(25.0)
    assert total_ul(inv.plates["WASTE"].wells["A1"]) == pytest.approx(200.0), "eight channels of 25"
    agree(world, y)


def test_every_well_of_the_column_is_checked(world: World) -> None:
    w = world.model_copy(deep=True)
    w.inventory.plates["P2"].wells["E1"] = [w.inventory.plates["P2"].wells["E1"][0]]  # E1 has only its 20 uL of beads
    e = err(w, T + "  - { op: transfer, from: { plate: P2, column: 1 }, to: { plate: WASTE, well: A1 }, volume_ul: 50 }\n")
    assert (e.code, e.coordinate) == ("E_VOLUME", "E1")
    w = world.model_copy(deep=True)
    w.inventory.plates["P2"].wells["C1"] = [w.inventory.plates["P2"].wells["C1"][0].model_copy(update={"volume_ul": 1990.0})]
    e = err(w, T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, column: 1 }, volume_ul: 100 }\n")
    assert (e.code, e.coordinate) == ("E_OVERFLOW", "C1")


def test_column_steps_need_columns_and_an_8_channel_pipette(world: World) -> None:
    e = err(world, T + "  - { op: transfer, from: { plate: P2, well: A1 }, to: { plate: P2, column: 3 }, volume_ul: 50 }\n")
    assert e.code == "E_PIPETTE_CHANNELS" and "column" in e.expected
    assert err(world, T + "  - { op: transfer, from: { vial: V_water }, to: { plate: P2, column: 3 }, volume_ul: 50 }\n").code == "E_PIPETTE_CHANNELS"
    assert err(world, T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, column: 13 }, volume_ul: 50 }\n").code == "E_COORDINATE"
    w = world.model_copy(deep=True)
    w.hardware.pipettes = [p for p in w.hardware.pipettes if p.channels == 1]
    e = err(w, T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, column: 3 }, volume_ul: 50 }\n")
    assert e.code == "E_PIPETTE_CHANNELS" and "8-channel" in e.expected
    w = world.model_copy(deep=True)
    w.hardware.pipettes = [p for p in w.hardware.pipettes if p.channels == 8]
    assert err(w, T + "  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: B2 }, volume_ul: 50 }\n").code == "E_PIPETTE_CHANNELS"


def test_a_column_of_tips_must_be_whole(world: World) -> None:
    w = world.model_copy(deep=True)
    w.deck.tip_racks["TIPS1"].used = [f"A{c}" for c in range(1, 13)]  # one tip gone from every column
    e = err(w, T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, column: 3 }, volume_ul: 50 }\n")
    assert e.code == "E_TIPS" and "full column" in e.actual
    assert compile(w, Protocol.from_yaml(T + "  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, well: A3 }, volume_ul: 50 }\n")).outcomes


def test_named_column_tips_return_and_come_back(world: World) -> None:
    y = T + """  - op: with_tip
    name: col
    body:
      - { op: mix, at: { plate: P2, column: 1 }, volume_ul: 50 }
  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P2, column: 2 }, volume_ul: 100 }
  - op: with_tip
    name: col
    body:
      - { op: transfer, from: { plate: P2, column: 1 }, to: { plate: WASTE, well: A1 }, volume_ul: 50 }
"""
    out = compile(world, Protocol.from_yaml(y))
    assert out.outcomes[0].cost.tips_used == 16
    ops = lower(world, out.pir).segments[0].ops
    picks = [op for op in ops if isinstance(op, PickUpTip)]
    rets = [op for op in ops if isinstance(op, ReturnTip)]
    assert [(p.well, p.channels) for p in picks] == [("A2", 8), ("A3", 8), ("A2", 8)] and [(r.well, r.channels) for r in rets] == [("A2", 8), ("A2", 8)]
    assert sum(isinstance(op, DropTip) for op in ops) == 1
    agree(world, y)


def test_budget_counts_a_column_step_once(world: World) -> None:
    y = T + "".join(f"  - {{ op: transfer, from: {{ plate: RES1, well: A1 }}, to: {{ plate: P2, column: {c} }}, volume_ul: 50 }}\n" for c in (2, 3, 4))
    out = compile(world, Protocol.from_yaml(y), budget=Budget.parse("sensor=scale_2,every=1,end=false"))
    assert out.outcomes[0].cost.observations == 3


def test_the_8_channel_wash(world: World) -> None:
    protocol = Protocol.load(EXAMPLES / "protocols" / "bead_wash_8ch.yaml")
    budget = Budget.parse("sensor=scale_2,every=2")
    out = compile(world, protocol, budget=budget)
    c = out.outcomes[0].cost
    assert (c.transfers, c.mixes, c.tips_used) == (6, 3, 3 * 8 + 8)
    inv = out.outcomes[0].world.inventory
    for r in COL:
        assert {l.reagent: l.volume_ul for l in inv.plates["P2"].wells[f"{r}1"]} == {"beads": pytest.approx(20.0)}
    assert {l.reagent: l.volume_ul for l in inv.plates["WASTE"].wells["A1"]} == {"water": pytest.approx(640.0), "wash_buffer": pytest.approx(4320.0)}
    pf = preflight(world, protocol, budget)
    assert pf.feasible and pf.tips["p300_multi_gen2"].needed == 32
    assert nominal_world(world, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(world, protocol, budget=budget)[-1].world.hash() == out.outcomes[0].world_hash


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_vendor_engine_runs_the_8_channel_wash(world: World) -> None:
    from ztra.drivers.otsim import OpentronsSimDriver

    protocol = Protocol.load(EXAMPLES / "protocols" / "bead_wash_8ch.yaml")
    src = emit_program(world, lower(world, compile(world, protocol).pir))[0][1]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wash8.py"
        path.write_text(src)
        r = subprocess.run([os.environ["ZTRA_OT_SIM_OT2"], str(path), "-o", "nothing"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr + "\n" + src

    w = World.load(EXAMPLES / "world")
    w.hardware.robot.api_level = "2.22"
    program = lower(w, compile(w, protocol).pir)
    driver = OpentronsSimDriver(w)

    class _NoHooks:
        def on_observe(self, op: object, op_index: int) -> None: ...

        def on_pause(self, op: object, op_index: int) -> None: ...

        def on_op_done(self, op_index: int) -> None: ...

    try:
        driver.run_segment(w, 0, program.segments[0], "", _NoHooks())
    except DriverFault as f:
        pytest.fail(f"the vendor engine disagreed: {f.message}")
    assert all(total_ul(driver.physical.inventory.plates["P2"].wells[f"{r}1"]) == pytest.approx(20.0) for r in COL)
