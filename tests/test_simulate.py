"""Simulator and observation scheduling."""

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler import compile
from ztra.compiler_errors import CompileError
from ztra.pir import ObserveOp
from ztra.protocol import Protocol
from ztra.schedule import Budget
from ztra.sensors import read
from ztra.simulate import Noise, simulate
from ztra.world import World

T = "version: 1\nsteps:\n"
FILL = T + "".join(f"  - {{ op: transfer, from: {{ vial: V_water }}, to: {{ plate: P1, well: {w} }}, volume_ul: 50 }}\n" for w in ["A1", "B1", "C1", "A2", "B2", "C2"])


def test_sensor_readings_from_a_world(world: World) -> None:
    scale = read(world, "scale_1", "t0")
    assert scale.values == {"mass_mg": 50.0}  # A1 holds 50 uL of water
    cam = read(world, "camera_1", "t0")
    assert list(cam.values) == ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]
    assert cam.values["A1"] == 50.0 and cam.values["B1"] == 0.0
    world.inventory.reagents["water"].density_mg_per_ul = 1.2
    assert read(world, "scale_1", "t1").values["mass_mg"] == pytest.approx(60.0)


def test_nominal_run_matches_the_compiler(world: World) -> None:
    result = compile(world, Protocol.from_yaml(FILL), budget=Budget(sensor="scale_1", every=3))
    sim = simulate(world, result.pir)
    assert len(sim.outcomes) == 1
    o = sim.outcomes[0]
    assert o.world_hash == result.outcomes[0].world_hash, "no noise → the simulator agrees with the compiler exactly"
    assert [r.label for r in o.readings] == ["auto_1", "auto_2", "auto_3"]
    assert [r.nominal["mass_mg"] for r in o.readings] == [200.0, 350.0, 350.0]  # 50 already in A1
    assert o.samples == 0 and o.events == {"failed_transfers": 0, "shortfalls": 0, "overflows": 0}


def test_noise_shifts_the_mean_and_is_seeded(world: World) -> None:
    result = compile(world, Protocol.from_yaml(FILL), budget=Budget(sensor="scale_1", every=None))
    a = simulate(world, result.pir, Noise(dispense_drift=0.03, jitter_ul=0.5), seeds=20, base_seed=7)
    b = simulate(world, result.pir, Noise(dispense_drift=0.03, jitter_ul=0.5), seeds=20, base_seed=7)
    assert a == b, "same seed, same answer"
    r = a.outcomes[0].readings[0]
    assert r.nominal["mass_mg"] == 350.0
    assert r.mean["mass_mg"] == pytest.approx(350.0 - 0.03 * 300.0, abs=1.5)  # 3% short on 300 uL dispensed
    assert 0 < r.std["mass_mg"] < 3
    c = simulate(world, result.pir, Noise(failure_rate=1.0), seeds=3)
    assert c.outcomes[0].events["failed_transfers"] == 18
    assert c.outcomes[0].readings[0].mean["mass_mg"] == 50.0, "nothing arrived, but the source still lost liquid"


def test_simulation_follows_every_path(world: World) -> None:
    demo = Protocol.load(EXAMPLES / "protocols/demo.yaml")
    result = compile(world, demo)
    sim = simulate(world, result.pir, Noise(dispense_drift=0.02), seeds=5)
    assert [o.conditions[0]["holds"] for o in sim.outcomes] == [True, False]
    assert [o.world_hash for o in sim.outcomes] == [o.world_hash for o in result.outcomes]
    for o in sim.outcomes:
        assert [r.label for r in o.readings] == ["after_fill"]
        assert r.nominal["mass_mg"] == 220.0 if (r := o.readings[0]) else False  # 50 + 150 + 20


def test_budget_scheduling(world: World) -> None:
    result = compile(world, Protocol.from_yaml(FILL), budget=Budget(sensor="camera_1", every=2, at_end=False, prefix="chk"))
    labels = [op.label for op in result.pir if isinstance(op, ObserveOp)]
    assert labels == ["chk_1", "chk_2", "chk_3"]
    assert result.outcomes[0].cost.observations == 3 and result.outcomes[0].cost.estimated_time_s == 6 * 16 + 3 * 2
    demo = compile(world, Protocol.load(EXAMPLES / "protocols/demo.yaml"), budget=Budget(sensor="scale_1", every=2))
    # observes land inside branch arms too, and one at the very end
    from ztra.pir import Branch

    br = next(op for op in demo.pir if isinstance(op, Branch))
    assert any(isinstance(op, ObserveOp) for op in br.then), "the then-arm's transfer was the 6th → observe"
    assert isinstance(demo.pir[-1], ObserveOp) and demo.pir[-1].label == "auto_4"
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(FILL), budget=Budget(sensor="nope"))
    assert e.value.code == "E_UNKNOWN_SENSOR"
    with pytest.raises(CompileError) as e:
        compile(world, Protocol.from_yaml(FILL), budget=Budget(sensor="scale_1", every=0))
    assert e.value.code == "E_BUDGET"


def test_budget_parse() -> None:
    b = Budget.parse("sensor=scale_1,every=3,end=false,prefix=w")
    assert (b.sensor, b.every, b.at_end, b.prefix) == ("scale_1", 3, False, "w")
    assert Budget.parse("sensor=camera_1").every is None
    with pytest.raises(ValueError):
        Budget.parse("every=3")


def test_pipette_accuracy_noise_is_per_run_bias_plus_scatter(world: World) -> None:
    result = compile(world, Protocol.from_yaml(FILL), budget=Budget(sensor="scale_1", every=None))
    sim = simulate(world, result.pir, Noise.normal(), seeds=200)
    r = sim.outcomes[0].readings[0]
    assert r.nominal["mass_mg"] == 350.0
    assert abs(r.mean["mass_mg"] - 350.0) < 2.0, "no drift on average"
    # 2% systematic on 300 uL dispensed ≈ 6 mg, plus scatter: expect a spread of roughly 6-8 mg
    assert 4.0 < r.std["mass_mg"] < 10.0
    world.hardware.pipettes[0].accuracy.systematic_pct = 0.0
    world.hardware.pipettes[0].accuracy.random_pct = 0.0
    world.hardware.pipettes[0].accuracy.random_ul = 0.0
    perfect = simulate(world, result.pir, Noise.normal(), seeds=20)
    assert perfect.outcomes[0].readings[0].std["mass_mg"] == 0.0
