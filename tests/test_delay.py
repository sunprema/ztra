"""delay: time passes, nothing moves. It is costed, lowered to ctx.delay, and runs
through the fake lab and the vendor engine."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from ztra.backend.opentrons import emit_program
from ztra.compiler import compile
from ztra.compiler_errors import CompileError
from ztra.lower import Delay, lower
from ztra.pir import Transform, TransformKind
from ztra.protocol import Protocol
from ztra.viz import trace
from ztra.world import World

T = "version: 1\nsteps:\n"
Y = T + """  - { op: transfer, from: { plate: RES1, well: A1 }, to: { plate: P1, well: B6 }, volume_ul: 100 }
  - { op: delay, minutes: 3 }
  - { op: delay, seconds: 30, minutes: 1 }
  - { op: transfer, from: { plate: P1, well: B6 }, to: { plate: WASTE, well: A1 }, volume_ul: 80 }
"""


def test_delay_is_costed_and_moves_nothing(world: World) -> None:
    out = compile(world, Protocol.from_yaml(Y))
    delays = [op for op in out.pir if isinstance(op, Transform) and op.kind is TransformKind.delay]
    assert [op.seconds for op in delays] == [180.0, 90.0]
    c = out.outcomes[0].cost
    assert c.delays == 2 and c.estimated_time_s >= 270.0 and c.transfers == 2
    assert "wait 180 s" in out.outcomes[0].trace
    with_delays = out.outcomes[0].world_hash
    without = compile(world, Protocol.from_yaml(Y.replace("  - { op: delay, minutes: 3 }\n  - { op: delay, seconds: 30, minutes: 1 }\n", ""))).outcomes[0].world_hash
    assert with_delays == without


def test_delay_must_be_positive(world: World) -> None:
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(T + "  - { op: delay }\n"))
    assert e.value.code == "E_DELAY"


def test_delay_lowers_to_ctx_delay(world: World) -> None:
    program = lower(world, compile(world, Protocol.from_yaml(Y)).pir)
    ops = [op for op in program.segments[0].ops if isinstance(op, Delay)]
    assert [op.seconds for op in ops] == [180.0, 90.0]
    src = emit_program(world, program)[0][1]
    assert "    ctx.delay(seconds=180)\n" in src and "    ctx.delay(seconds=90)\n" in src


def test_delay_replays_on_the_fake_lab(world: World) -> None:
    frames = trace(world, Protocol.from_yaml(Y))
    assert "wait 180 s" in [f.description for f in frames]


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_vendor_simulator_accepts_a_delay(world: World) -> None:
    src = emit_program(world, lower(world, compile(world, Protocol.from_yaml(Y)).pir))[0][1]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "segment_0.py"
        path.write_text(src)
        r = subprocess.run([os.environ["ZTRA_OT_SIM_OT2"], str(path), "-o", "nothing"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
