"""Tip economy: one tip for a block of steps, named tips that go back to the rack and
come out again, and swapping in a fresh rack mid-run — all checked, all consistent
between the compiler, the simulator, the fake lab and the vendor engine."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from ztra.backend.opentrons import emit_program
from ztra.compiler import compile, paths
from ztra.compiler_errors import CompileError
from ztra.lower import DropTip, Pause, PickUpTip, ReturnTip, lower
from ztra.preflight import preflight
from ztra.protocol import Protocol
from ztra.simulate import nominal_world
from ztra.viz import trace
from ztra.world import World
from ztra.world.coords import WellCoord
from ztra.world.hardware import Mount, Pipette

T = "version: 1\nsteps:\n"

DISTRIBUTE = T + """  - op: with_tip
    body:
      - op: for_wells
        wells: [A9..D9]
        body:
          - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: $well }, volume_ul: 100 }
"""

NAMED = T + """  - op: with_tip
    name: t_A1
    body:
      - { op: transfer, from: { plate: P1, well: A1 }, to: { plate: P1, well: B9 }, volume_ul: 20 }
  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: C9 }, volume_ul: 50 }
  - op: with_tip
    name: t_A1
    body:
      - { op: transfer, from: { plate: P1, well: A1 }, to: { plate: P1, well: B9 }, volume_ul: 20 }
"""


def err(world: World, yaml: str) -> CompileError:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(yaml))
    return e.value


def nearly_empty_rack(world: World, free: int) -> World:
    w = world.model_copy(deep=True)
    names = [WellCoord(r, c).name for c in range(12) for r in range(8)]
    w.deck.tip_racks["TIPS1"].used = names[: 96 - free]
    return w


def test_one_tip_for_a_distribute_block(world: World) -> None:
    out = compile(world, Protocol.from_yaml(DISTRIBUTE))
    c = out.outcomes[0].cost
    assert (c.transfers, c.tips_used) == (4, 1)
    assert len(out.outcomes[0].world.deck.tip_racks["TIPS1"].used) == 2 + 1
    ops = lower(world, out.pir).segments[0].ops
    assert sum(isinstance(op, PickUpTip) for op in ops) == 1 and sum(isinstance(op, DropTip) for op in ops) == 1
    assert isinstance(ops[0], PickUpTip) and isinstance(ops[-1], DropTip)
    # every engine agrees on the world afterwards
    assert nominal_world(world, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(world, Protocol.from_yaml(DISTRIBUTE))[-1].world.hash() == out.outcomes[0].world_hash


def test_a_named_tip_goes_back_and_comes_out_again(world: World) -> None:
    out = compile(world, Protocol.from_yaml(NAMED))
    c = out.outcomes[0].cost
    assert (c.transfers, c.tips_used) == (3, 2)  # t_A1 once, plus the fresh tip in between
    ops = lower(world, out.pir).segments[0].ops
    picks = [op for op in ops if isinstance(op, PickUpTip)]
    returns = [op for op in ops if isinstance(op, ReturnTip)]
    assert len(picks) == 3 and len(returns) == 2
    assert (picks[0].rack, picks[0].well) == (picks[2].rack, picks[2].well) == (returns[0].rack, returns[0].well)
    assert picks[1].well != picks[0].well
    assert "pip_p300_single_gen2.return_tip()" in emit_program(world, lower(world, out.pir))[0][1]
    assert "picked up again" in " ".join(out.outcomes[0].trace)
    assert nominal_world(world, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(world, Protocol.from_yaml(NAMED))[-1].world.hash() == out.outcomes[0].world_hash


def test_a_shared_tip_draws_from_one_place_only(world: World) -> None:
    two_sources = T + """  - op: with_tip
    body:
      - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A10 }, volume_ul: 100 }
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A10 }, volume_ul: 50 }
"""
    e = err(world, two_sources)
    assert e.code == "E_TIP_CONTAMINATION" and "RES1:A1" in e.expected and "V_water" in e.actual
    mixing_the_destination = T + """  - op: with_tip
    body:
      - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A10 }, volume_ul: 100 }
      - { op: mix, at: { plate: P1, well: A10 }, volume_ul: 50 }
"""
    assert err(world, mixing_the_destination).code == "E_TIP_CONTAMINATION"
    # a named tip remembers its source across blocks: the second block draws from somewhere else
    first_block = "- { op: transfer, from: { plate: P1, well: A1 }, to: { plate: P1, well: B9 }, volume_ul: 20 }\n"
    reused_elsewhere = NAMED.replace(first_block, "- { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: B9 }, volume_ul: 20 }\n", 1)
    e = err(world, reused_elsewhere)
    assert e.code == "E_TIP_CONTAMINATION" and e.step_path == [2, 0]


def test_tip_scopes_do_not_nest_and_racks_are_not_swapped_mid_tip(world: World) -> None:
    nested = T + """  - op: with_tip
    body:
      - op: with_tip
        body:
          - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A10 }, volume_ul: 100 }
"""
    assert err(world, nested).code == "E_TIP_SCOPE"
    swap_inside = T + """  - op: with_tip
    body:
      - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A10 }, volume_ul: 100 }
      - { op: replenish_tips, rack: TIPS1 }
"""
    assert err(world, swap_inside).code == "E_TIP_SCOPE"


def test_one_pipette_per_tip(world: World) -> None:
    w = world.model_copy(deep=True)
    w.hardware.pipettes.append(Pipette(name="p20_single_gen2", mount=Mount.left, min_ul=1, max_ul=20))
    y = T + """  - op: with_tip
    body:
      - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A10 }, volume_ul: 100 }
      - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A10 }, volume_ul: 5 }
"""
    e = err(w, y)
    assert e.code == "E_TIP_PIPETTE" and (e.expected, e.actual) == ("p300_single_gen2", "p20_single_gen2")


def test_replenishing_a_rack_mid_run(world: World) -> None:
    w = nearly_empty_rack(world, free=6)
    five = "".join(f"  - {{ op: transfer, from: {{ plate: RES1, well: A1 }}, to: {{ plate: P1, well: {well} }}, volume_ul: 50 }}\n" for well in ["A11", "B11", "C11", "D11", "E11"])
    without = T + five + five
    with_swap = T + five + "  - { op: replenish_tips, rack: TIPS1 }\n" + five
    e = err(w, without)
    assert e.code == "E_TIPS" and e.iterations == [] and e.step_path == [6]
    assert not preflight(w, Protocol.from_yaml(without)).feasible

    out = compile(w, Protocol.from_yaml(with_swap))
    c = out.outcomes[0].cost
    assert (c.transfers, c.tips_used, c.tip_racks_replaced) == (10, 10, 1)
    assert sorted(out.outcomes[0].world.deck.tip_racks["TIPS1"].used) == ["A1", "B1", "C1", "D1", "E1"], "the new rack, five tips in"
    pf = preflight(w, Protocol.from_yaml(with_swap))
    assert pf.feasible and pf.tips["p300_single_gen2"].available == 6 + 96

    program = lower(w, out.pir)
    ops = program.segments[0].ops
    pause = next(op for op in ops if isinstance(op, Pause))
    assert pause.replenish_rack == "TIPS1" and "Replace tip rack TIPS1" in pause.message
    after = [op for op in ops[ops.index(pause) :] if isinstance(op, PickUpTip)]
    assert [op.well for op in after] == ["A1", "B1", "C1", "D1", "E1"]
    src = emit_program(w, program)[0][1]
    assert 'ctx.pause("Replace tip rack TIPS1 with a fresh one and resume")\n    pip_p300_single_gen2.reset_tipracks()' in src
    assert nominal_world(w, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(w, Protocol.from_yaml(with_swap))[-1].world.hash() == out.outcomes[0].world_hash


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
@pytest.mark.parametrize("name,yaml", [("distribute", DISTRIBUTE), ("named", NAMED)])
def test_vendor_engine_accepts_tip_reuse(world: World, name: str, yaml: str) -> None:
    src = emit_program(world, lower(world, compile(world, Protocol.from_yaml(yaml)).pir))[0][1]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / f"{name}.py"
        path.write_text(src)
        r = subprocess.run([os.environ["ZTRA_OT_SIM_OT2"], str(path), "-o", "nothing"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr + "\n" + src


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_vendor_engine_accepts_a_rack_swap(world: World) -> None:
    w = nearly_empty_rack(world, free=2)
    y = T + """  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: A11 }, volume_ul: 50 }
  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: B11 }, volume_ul: 50 }
  - { op: replenish_tips, rack: TIPS1 }
  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: C11 }, volume_ul: 50 }
"""
    src = emit_program(w, lower(w, compile(w, Protocol.from_yaml(y)).pir))[0][1]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "swap.py"
        path.write_text(src)
        r = subprocess.run([os.environ["ZTRA_OT_SIM_OT2"], str(path), "-o", "nothing"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr + "\n" + src
