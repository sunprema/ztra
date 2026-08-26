"""The runtime on the fake driver: the whole loop, branches, faults, the interlock, and the rules
that keep an intent from running twice or without approval."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from ztra.drivers.fake import FakeDriver
from ztra.lower import Aspirate, Dispense
from ztra.protocol import Protocol
from ztra.runtime import Runtime
from ztra.schedule import Budget
from ztra.sensors import read
from ztra.store import ObservationCommit, Store
from ztra.telemetry import FixedSensor, SimulatedSensor, TelemetryService
from ztra.world import World
from ztra.world.hardware import Range, SensorKind
from ztra.world.inventory import total_ul

def CLOCK() -> str:
    return "2026-08-26T10:00:00+00:00"


def lab(store: Store, branch: str, seed: int = 0, faults: dict | None = None, accurate: bool = False, adapters: dict | None = None, approve: bool = True) -> tuple[Runtime, FakeDriver]:  # type: ignore[type-arg]
    c = store.get_commit(store.head(branch))
    physical = store.get_world(c.base_world)  # type: ignore[union-attr]
    driver = FakeDriver(physical, seed=seed, accurate=accurate, faults=faults)
    sensors = dict(adapters or {})
    for sid in physical.hardware.sensors:
        def snapshot(d: FakeDriver = driver) -> World:
            return d.physical

        sensors.setdefault(sid, SimulatedSensor(snapshot, seed=seed + 1))
    rt = Runtime(store, driver, TelemetryService(physical.hardware, sensors, CLOCK), approve=lambda _c, _f: approve, clock=CLOCK)
    return rt, driver


def op_index(store: Store, branch: str, kind: type, labware: str, well: str, nth: int = 1) -> int:
    """Index in segment 0 of the nth op of this kind at this address."""
    ops = store.program(store.head(branch)).segments[0].ops
    hits = [i for i, op in enumerate(ops) if isinstance(op, kind) and getattr(op, "labware", None) == labware and getattr(op, "well", None) == well]
    return hits[nth - 1]


@pytest.fixture
def store(tmp_path: Path, world: World) -> Store:
    s = Store.init(tmp_path / ".ztra", world, CLOCK)
    s.branch("exp")
    return s


def test_happy_path_closes_the_loop(store: Store) -> None:
    store.commit_intent("exp", Protocol.load(EXAMPLES / "protocols/enzyme_dilution.yaml"), "dilution", CLOCK, budget=Budget(sensor="scale_1", every=3))
    rt, driver = lab(store, "exp")
    r = rt.run("exp", "first fake run")
    assert r.status == "completed" and r.segments == [0] and r.decisions == [] and r.readings == 6
    assert r.report is not None and r.report["classification"] == "ok", r.report
    oc = store.get_commit(r.observation or "")
    assert isinstance(oc, ObservationCommit) and oc.status == "completed" and oc.chosen_outcome == 0
    assert len(oc.telemetry["readings"]) == 6 and oc.telemetry["readings"][0]["at"] == CLOCK()
    # what the store now believes vs what the fake lab really holds: within the pipettes' tolerance
    believed = store.working_world("main")
    for w in ["A2", "B2", "C2", "D2", "E2"]:
        real = total_ul(driver.physical.inventory.plates["P1"].wells[w])
        assert abs(total_ul(believed.inventory.plates["P1"].wells[w]) - real) < 12, w
    assert believed.inventory.vials["V_enzyme"].state.value == "thawed" and driver.physical.inventory.vials["V_enzyme"].freeze_thaw_cycles == 2
    assert (store.root / "runs").exists() and '"recorded"' in next((store.root / "runs").iterdir()).read_text()


def test_branches_are_decided_from_the_reading(store: Store) -> None:
    store.commit_intent("exp", Protocol.load(EXAMPLES / "protocols/demo.yaml"), "demo", CLOCK)
    rt, driver = lab(store, "exp", accurate=True)  # accurate lab: mass lands on 220 >= 215
    r = rt.run("exp")
    assert r.status == "completed" and r.decisions == [True] and r.segments == [0, 1] and driver.segments_run == [0, 1]
    assert store.get_commit(r.observation or "").chosen_outcome == 0  # type: ignore[union-attr]
    assert "B2" in store.working_world("main").inventory.plates["P1"].wells

    s2 = Store.init(store.root.parent / "two", World.load(EXAMPLES / "world"), CLOCK)
    s2.branch("exp")
    s2.commit_intent("exp", Protocol.load(EXAMPLES / "protocols/demo.yaml"), "demo", CLOCK)
    enzyme_dispense = op_index(s2, "exp", Aspirate, "TR1", "B1") + 1  # the dispense right after the enzyme aspirate
    rt2, _ = lab(s2, "exp", faults={(0, enzyme_dispense): "clog"})  # 20 uL never arrives → mass < 215
    r2 = rt2.run("exp")
    assert r2.status == "completed" and r2.decisions == [False] and r2.segments == [0, 2]
    assert s2.get_commit(r2.observation or "").chosen_outcome == 1  # type: ignore[union-attr]


def test_a_clogged_tip_shows_up_in_the_diff(store: Store) -> None:
    store.commit_intent("exp", Protocol.load(EXAMPLES / "protocols/enzyme_dilution.yaml"), "dilution", CLOCK, budget=Budget(sensor="camera_1", every=3))
    rt, driver = lab(store, "exp", faults={(0, op_index(store, "exp", Dispense, "P1", "A2")): "clog"})  # first water dispense into A2
    # A2 is not in the camera's column, so the loss is only visible in the scale... which is not scheduled: a camera every 3
    r = rt.run("exp")
    assert r.status == "completed"
    assert total_ul(driver.physical.inventory.plates["P1"].wells["A2"]) < 30, "the fake lab really lost that transfer"
    assert r.report is not None and r.report["classification"] in ("ok", "systematic"), "nobody looked at A2: honest, not localized"

    s2 = Store.init(store.root.parent / "two", World.load(EXAMPLES / "world"), CLOCK)
    s2.branch("exp")
    y = "version: 1\nsteps:\n  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: B1 }, volume_ul: 100 }\n  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: C1 }, volume_ul: 100 }\n"
    s2.commit_intent("exp", Protocol.from_yaml(y), "fill column 1", CLOCK, budget=Budget(sensor="camera_1"))
    rt2, _ = lab(s2, "exp", faults={(0, op_index(s2, "exp", Dispense, "P1", "B1")): "clog"})
    r2 = rt2.run("exp")
    assert r2.report is not None and r2.report["classification"] == "localized"
    assert total_ul(s2.working_world("main").inventory.plates["P1"].wells.get("B1", [])) < 20, "the observed world was corrected from the camera"


def test_an_opened_door_aborts_and_records_the_partial_world(store: Store) -> None:
    store.commit_intent("exp", Protocol.load(EXAMPLES / "protocols/enzyme_dilution.yaml"), "dilution", CLOCK, budget=Budget(sensor="scale_1", every=3))
    b2_enzyme_aspirate = op_index(store, "exp", Aspirate, "TR1", "B1", nth=2)  # B2's enzyme transfer, mid-way
    rt, driver = lab(store, "exp", faults={(0, b2_enzyme_aspirate): "door_open"})
    r = rt.run("exp", "door")
    assert r.status == "aborted" and r.reason is not None and r.reason["code"] == "D_DOOR_OPEN"
    oc = store.get_commit(r.observation or "")
    assert isinstance(oc, ObservationCommit) and oc.status == "aborted" and oc.chosen_outcome is None and oc.reason == r.reason
    believed = store.working_world("main")
    assert total_ul(believed.inventory.plates["P1"].wells["A2"]) == 200.0, "A2 was fully done"
    assert total_ul(believed.inventory.plates["P1"].wells.get("B2", [])) == 180.0, "B2 got its water; the enzyme transfer was interrupted"
    assert "C2" not in believed.inventory.plates["P1"].wells
    assert believed.inventory.vials["V_enzyme"].state.value == "thawed"
    assert store.head("exp") == store.head("main")
    assert rt.run("exp").status == "refused", "the intent counts as executed; retrying means a new intent"


def test_estop_on_a_reading_outside_the_envelope(store: Store, world: World) -> None:
    w = store.working_world("main")
    w.hardware.sensors["temp_1"] = w.hardware.sensors["scale_1"].model_copy(update={"kind": SensorKind.temperature, "unit": "C", "sigma": 0.2})
    w.hardware.safe_envelope.temperature_c = Range(min=4, max=40)
    s = Store.init(store.root.parent / "hot", w, CLOCK)
    s.branch("exp")
    y = "version: 1\nsteps:\n  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: B1 }, volume_ul: 100 }\n  - { op: observe, sensor: temp_1, label: t }\n  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: C1 }, volume_ul: 100 }\n"
    s.commit_intent("exp", Protocol.from_yaml(y), "hot", CLOCK)
    rt, _ = lab(s, "exp", adapters={"temp_1": FixedSensor({"temperature_c": 80.0})})
    r = rt.run("exp")
    assert r.status == "aborted" and r.reason is not None and r.reason["code"] == "E_STOP_TEMPERATURE"
    believed = s.working_world("main")
    assert "B1" in believed.inventory.plates["P1"].wells and "C1" not in believed.inventory.plates["P1"].wells


def test_nothing_runs_without_approval_or_twice(store: Store) -> None:
    store.commit_intent("exp", Protocol.load(EXAMPLES / "protocols/enzyme_dilution.yaml"), "dilution", CLOCK)
    rt, driver = lab(store, "exp", approve=False)
    r = rt.run("exp")
    assert r.status == "refused" and r.reason is not None and r.reason["code"] == "S_NOT_APPROVED"
    assert driver.segments_run == [] and not (store.root / "runs").exists()
    assert [c.kind for _, c in store.history("main")] == ["root"]

    rt, driver = lab(store, "exp")
    assert rt.run("exp").status == "completed"
    again = rt.run("exp")
    assert again.status == "refused" and again.reason is not None and again.reason["code"] == "S_NOTHING_TO_EXECUTE"

    # a crash between dispatch and record leaves a journal; the next run must not re-dispatch
    store.branch("exp2")
    store.commit_intent("exp2", Protocol.from_yaml("version: 1\nsteps:\n  - { op: mix, at: { plate: P1, well: A1 }, volume_ul: 40 }\n"), None, CLOCK)
    head = store.head("exp2")
    (store.root / "runs" / f"{head}.json").write_text('{"status": "dispatched"}\n')
    rt, driver = lab(store, "exp2")
    r = rt.run("exp2")
    assert r.status == "refused" and r.reason is not None and r.reason["code"] == "S_RUN_IN_PROGRESS" and driver.segments_run == []


def test_stale_branch_is_refused_before_dispatch(store: Store) -> None:
    store.branch("other")
    store.commit_intent("exp", Protocol.from_yaml("version: 1\nsteps:\n  - { op: mix, at: { plate: P1, well: A1 }, volume_ul: 40 }\n"), None, CLOCK)
    store.commit_intent("other", Protocol.from_yaml("version: 1\nsteps:\n  - { op: mix, at: { plate: P1, well: A1 }, volume_ul: 30 }\n"), None, CLOCK)
    store.execute("other", clock=CLOCK)
    rt, driver = lab(store, "exp")
    r = rt.run("exp")
    assert r.status == "refused" and r.reason is not None and r.reason["code"] == "S_NOT_FAST_FORWARD" and driver.segments_run == []


def test_fake_lab_reads_like_the_model(world: World) -> None:
    d = FakeDriver(world, accurate=True)
    assert read(d.physical, "scale_1", "x").values == {"mass_mg": 50.0}
    s = SimulatedSensor(lambda: d.physical, noisy=False)
    assert s.read("camera_1")["A1"] == 50.0
