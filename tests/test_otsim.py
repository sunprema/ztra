"""The vendor-simulator driver: the backend declares dispense targets as empty from
API 2.22, the driver refuses worlds it cannot verify, and — when a vendor venv is
available — a full run agrees with the compiler's prediction, well for well."""

import os
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from ztra.backend.opentrons import emit_program
from ztra.compiler import compile
from ztra.driver import DriverFault
from ztra.drivers.otsim import OpentronsSimDriver
from ztra.lower import lower
from ztra.protocol import Protocol
from ztra.runtime import Runtime
from ztra.schedule import Budget
from ztra.store import ObservationCommit, Store
from ztra.telemetry import SensorAdapter, SimulatedSensor, TelemetryService
from ztra.world import World


def world_at(api: str) -> World:
    w = World.load(EXAMPLES / "world")
    w.hardware.robot.api_level = api
    return w


def enzyme() -> Protocol:
    return Protocol.load(EXAMPLES / "protocols" / "enzyme_dilution.yaml")


def test_load_empty_is_emitted_from_api_2_22() -> None:
    for api, expected in [("2.16", False), ("2.22", True)]:
        w = world_at(api)
        program = lower(w, compile(w, enzyme()).pir)
        src = emit_program(w, program)[0][1]
        assert ("load_empty" in src) == expected, api
        if expected:
            assert 'P1.load_empty([P1["A2"], P1["B2"], P1["C2"], P1["D2"], P1["E2"]])' in src


def test_driver_refuses_old_api_levels() -> None:
    with pytest.raises(DriverFault) as f:
        OpentronsSimDriver(world_at("2.16"))
    assert f.value.code == "D_API_LEVEL"


def test_driver_names_the_missing_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZTRA_OT_SIM_OT2", raising=False)
    with pytest.raises(DriverFault) as f:
        OpentronsSimDriver(world_at("2.22"))
    assert f.value.code == "D_NO_VENDOR_VENV" and "ZTRA_OT_SIM_OT2" in f.value.message


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_full_run_agrees_with_the_vendor_engine(tmp_path: Path) -> None:
    w = world_at("2.22")
    store = Store.init(tmp_path / ".ztra", w)
    store.branch("hyp", "main")
    store.commit_intent("hyp", enzyme(), None, budget=Budget.parse("sensor=scale_1,every=3"))

    driver = OpentronsSimDriver(store.get_world(store.get_commit(store.head("hyp")).base_world))  # type: ignore[union-attr]
    sensors: dict[str, SensorAdapter] = {sid: SimulatedSensor(lambda: driver.physical, seed=1) for sid in w.hardware.sensors}
    rt = Runtime(store, driver, TelemetryService(w.hardware, sensors), approve=lambda _c, _f: True)
    result = rt.run("hyp", None)

    assert result.status == "completed", result.reason
    # the vendor engine verified every pause and the end; the physical world is the prediction
    predicted = compile(w, enzyme(), budget=Budget.parse("sensor=scale_1,every=3")).outcomes[0].world
    assert driver.physical.hash() == predicted.hash()
    oc = store.get_commit(result.observation or "")
    assert isinstance(oc, ObservationCommit) and oc.status == "completed"


@pytest.mark.skipif(not os.environ.get("ZTRA_OT_SIM_OT2"), reason="ZTRA_OT_SIM_OT2 not set")
def test_vendor_refusal_surfaces_as_a_fault() -> None:
    w = world_at("2.22")
    program = lower(w, compile(w, enzyme()).pir)
    driver = OpentronsSimDriver(w)
    # sabotage: a dispense with no aspirate — the vendor engine refuses this since 2.17
    seg = program.segments[0].model_copy(deep=True)
    seg.ops = [op for op in seg.ops if op.op != "aspirate"]

    class _NoHooks:
        def on_observe(self, op: object, op_index: int) -> None: ...

        def on_pause(self, op: object, op_index: int) -> None: ...

        def on_op_done(self, op_index: int) -> None: ...

    with pytest.raises(DriverFault) as f:
        driver.run_segment(w, 0, seg, "", _NoHooks())
    assert f.value.code == "D_VENDOR_REFUSED"
