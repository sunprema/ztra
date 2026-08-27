"""The acceptance test for the cookbook gaps: the bead wash — per-well tables, a
reservoir and a waste, delays, dedicated tips kept across rounds, motion control,
the magnetic module — compiles with a budget, is feasible, lowers to one vendor
file, runs on the fake lab, and runs inside the vendor engine with every volume
agreeing."""

import os
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from ztra.backend.opentrons import emit_program
from ztra.compiler import compile, paths
from ztra.drivers.fake import FakeDriver
from ztra.lower import lower
from ztra.preflight import preflight
from ztra.protocol import Protocol
from ztra.runtime import Runtime
from ztra.schedule import Budget
from ztra.simulate import nominal_world
from ztra.store import ObservationCommit, Store
from ztra.telemetry import SensorAdapter, SimulatedSensor, TelemetryService
from ztra.viz import trace
from ztra.world import World
from ztra.world.inventory import total_ul

WASH = EXAMPLES / "protocols" / "bead_wash.yaml"
BUDGET = Budget.parse("sensor=scale_2,every=4")
SAMPLES = ["A1", "B1", "C1", "D1"]


def test_the_wash_is_predicted_well_for_well(world: World) -> None:
    out = compile(world, Protocol.load(WASH), budget=BUDGET)
    assert len(out.outcomes) == 1
    c = out.outcomes[0].cost
    assert (c.transfers, c.mixes, c.delays, c.module_actions) == (24, 12, 3, 6)
    assert c.tips_used == 3 + 4, "one clean tip per round for the buffer, one dedicated tip per sample for everything else"
    assert c.observations >= 9 and c.estimated_time_s > 3 * 180
    inv = out.outcomes[0].world.inventory
    for w in SAMPLES:
        assert {l.reagent: l.volume_ul for l in inv.plates["P2"].wells[w]} == {"beads": pytest.approx(20.0)}, f"{w}: only the beads remain, ready for elution"
    waste = {l.reagent: l.volume_ul for l in inv.plates["WASTE"].wells["A1"]}
    assert waste == {"water": pytest.approx(320.0), "wash_buffer": pytest.approx(2160.0)} and "beads" not in waste
    assert total_ul(inv.plates["RES1"].wells["A1"]) == pytest.approx(12000 - 2160)
    assert not out.outcomes[0].world.deck.modules["MAG1"].engaged
    assert sorted(out.outcomes[0].world.deck.tip_racks["TIPS1"].used) == sorted(["A1", "B1"] + ["C1", "D1", "E1", "F1", "G1", "H1", "A2"])


def test_the_wash_is_feasible_and_every_engine_agrees(world: World) -> None:
    pf = preflight(world, Protocol.load(WASH), BUDGET)
    assert pf.feasible and pf.tips["p300_single_gen2"].needed == 7
    out = compile(world, Protocol.load(WASH), budget=BUDGET)
    program = lower(world, out.pir)
    assert len(program.segments) == 1
    src = emit_program(world, program)[0][1]
    for line in ['MAG1.engage(height_from_base=6.5)', 'MAG1.disengage()', 'ctx.delay(seconds=180)', '.return_tip()', '.air_gap(10)', '.blow_out()', 'WASTE["A1"].top(-3)', 'flow_rate.aspirate = 20']:
        assert line in src, line
    assert nominal_world(world, paths(out.pir)[0][1]).hash() == out.outcomes[0].world_hash
    assert trace(world, Protocol.load(WASH), budget=BUDGET)[-1].world.hash() == out.outcomes[0].world_hash


def _run(world: World, tmp_path: Path, driver_factory: object) -> tuple[Store, object]:
    store = Store.init(tmp_path / ".ztra", world)
    store.branch("wash", "main")
    store.commit_intent("wash", Protocol.load(WASH), None, budget=BUDGET)
    physical = store.get_world(store.get_commit(store.head("wash")).base_world)  # type: ignore[union-attr]
    driver = driver_factory(physical)  # type: ignore[operator]

    def snapshot() -> World:
        return driver.physical  # type: ignore[no-any-return]

    sensors: dict[str, SensorAdapter] = {sid: SimulatedSensor(snapshot, seed=1) for sid in world.hardware.sensors}
    rt = Runtime(store, driver, TelemetryService(world.hardware, sensors), approve=lambda _c, _f: True)
    return store, rt.run("wash", None)


def test_the_wash_runs_on_the_fake_lab(world: World, tmp_path: Path) -> None:
    store, result = _run(world, tmp_path, lambda physical: FakeDriver(physical, seed=3))
    assert result.status == "completed", result.reason  # type: ignore[attr-defined]
    oc = store.get_commit(result.observation or "")  # type: ignore[attr-defined]
    assert isinstance(oc, ObservationCommit) and oc.status == "completed"
    assert oc.report is not None and oc.report["classification"] in ("ok", "systematic")


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_the_wash_runs_inside_the_vendor_engine(tmp_path: Path) -> None:
    from ztra.drivers.otsim import OpentronsSimDriver

    w = World.load(EXAMPLES / "world")
    w.hardware.robot.api_level = "2.22"
    store, result = _run(w, tmp_path, OpentronsSimDriver)
    assert result.status == "completed", result.reason  # type: ignore[attr-defined]
