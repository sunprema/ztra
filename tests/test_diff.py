"""The diff engine: the U5 scenarios, outcome resolution from readings, and the observed-world estimate."""

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler import compile
from ztra.diff import DiffError, Verdict, diff, resolve_outcome
from ztra.protocol import Protocol
from ztra.schedule import Budget
from ztra.sensors import Telemetry, TelemetryReading
from ztra.simulate import simulate
from ztra.world import World
from ztra.world.inventory import total_ul

T = "version: 1\nsteps:\n"
FILL = T + "".join(f"  - {{ op: transfer, from: {{ vial: V_water }}, to: {{ plate: P1, well: {w} }}, volume_ul: 50 }}\n" for w in ["A1", "B1", "C1", "A2", "B2", "C2"])


def tel(**by_label: dict[str, float]) -> Telemetry:
    sensors = {"scale": "scale_1", "cam": "camera_1"}
    return Telemetry(readings=[TelemetryReading(label=k, sensor=sensors["cam" if k.startswith("cam") else "scale"], values=v) for k, v in by_label.items()])


def fill_with_both_sensors(world: World) -> tuple:  # type: ignore[type-arg]
    y = FILL + "  - { op: observe, sensor: scale_1, label: scale_end }\n  - { op: observe, sensor: camera_1, label: cam_end }\n"
    result = compile(world, Protocol.from_yaml(y))
    return result, simulate(world, result.pir)


def test_s1_systematic_drift_is_seen_in_total_but_not_per_well(world: World) -> None:
    result, sim = fill_with_both_sensors(world)
    cam = {w: 50.0 - 1.5 for w in ["A1", "B1", "C1"]} | {w: 0.0 for w in ["D1", "E1", "F1", "G1", "H1"]}
    cam["A1"] = 100.0 - 1.5
    report, observed = diff(result, sim, tel(scale_end={"mass_mg": 350.0 - 9.0}, cam_end=cam))
    assert report.classification == "systematic" and report.can_localize
    assert report.unaccounted == {"scale_end": pytest.approx(-9.0)}
    assert all(e.verdict is Verdict.verified for e in report.entries if e.metric != "mass_mg")
    assert observed.hash() == result.outcomes[0].world.hash(), "nothing could be placed, so the world stays predicted"
    assert any("calibration" in n for n in report.notes)


def test_s2_failure_in_an_unobserved_well_cannot_be_localized(world: World) -> None:
    result, sim = fill_with_both_sensors(world)
    cam = {"A1": 100.0, "B1": 50.0, "C1": 50.0} | {w: 0.0 for w in ["D1", "E1", "F1", "G1", "H1"]}
    report, _ = diff(result, sim, tel(scale_end={"mass_mg": 300.0}, cam_end=cam))  # A2 got nothing
    assert report.classification == "systematic"
    assert report.counts[Verdict.deviated.value] == 1 and report.counts[Verdict.verified.value] == 8


def test_s3_failure_in_an_observed_well_is_localized_and_folded_into_the_world(world: World) -> None:
    result, sim = fill_with_both_sensors(world)
    cam = {"A1": 100.0, "B1": 2.0, "C1": 50.0} | {w: 0.0 for w in ["D1", "E1", "F1", "G1", "H1"]}
    report, observed = diff(result, sim, tel(scale_end={"mass_mg": 302.0}, cam_end=cam))
    assert report.classification == "localized"
    b1 = next(e for e in report.entries if e.metric == "B1")
    assert b1.verdict is Verdict.deviated and b1.delta == pytest.approx(-48.0)
    assert total_ul(observed.inventory.plates["P1"].wells["B1"]) == pytest.approx(2.0)
    assert total_ul(observed.inventory.plates["P1"].wells["A1"]) == pytest.approx(100.0)
    assert observed.hash() != result.outcomes[0].world.hash() and report.observed_world_hash == observed.hash()


def test_missing_readings_are_unobserved(world: World) -> None:
    result, sim = fill_with_both_sensors(world)
    report, _ = diff(result, sim, Telemetry())
    assert report.classification == "unobserved" and not report.can_localize
    assert report.counts == {Verdict.verified.value: 0, Verdict.deviated.value: 0, Verdict.unobserved.value: 9}
    report, _ = diff(result, sim, tel(scale_end={"mass_mg": 350.0}, bogus={"mass_mg": 1.0}))
    assert report.classification == "ok" and any("bogus" in n for n in report.notes)


def test_outcome_is_resolved_from_the_readings(world: World) -> None:
    demo = Protocol.load(EXAMPLES / "protocols/demo.yaml")
    result = compile(world, demo)
    sim = simulate(world, result.pir)
    assert resolve_outcome(result, tel(after_fill={"mass_mg": 220.0})) == 0
    assert resolve_outcome(result, tel(after_fill={"mass_mg": 211.6})) == 1
    with pytest.raises(DiffError) as e:
        resolve_outcome(result, Telemetry())
    assert e.value.code == "D_UNRESOLVED"
    report, observed = diff(result, sim, Telemetry.load(EXAMPLES / "telemetry/demo_short_fill.yaml"))
    assert report.outcome == 1 and report.classification == "systematic"
    assert report.unaccounted["after_fill"] == pytest.approx(-8.4)
    assert observed.hash() == result.outcomes[1].world.hash()


def test_simulation_spread_widens_the_tolerance(world: World) -> None:
    from ztra.simulate import Noise

    result = compile(world, Protocol.from_yaml(FILL), budget=Budget(sensor="scale_1"))
    tight = simulate(world, result.pir)
    loose = simulate(world, result.pir, Noise(jitter_ul=3.0), seeds=30)
    reading = tel(auto_1={"mass_mg": 350.0 - 2.0})
    assert diff(result, tight, reading)[0].entries[0].verdict is Verdict.deviated  # 2 mg > 3 x 0.5
    assert diff(result, loose, reading)[0].entries[0].verdict is Verdict.verified  # spread of ~7 uL absorbs it


def test_u8_a_normal_run_is_verified_but_a_clogged_tip_is_not(world: World) -> None:
    """The U8 finding: expected readings must carry the pipettes' accuracy, or every real run reads DEVIATED."""
    from ztra.simulate import Noise
    from ztra.store import EXPECTED_SEEDS

    y = T + "  - { op: thaw, vial: V_enzyme }\n"
    for w in ["A2", "B2", "C2", "D2", "E2"]:
        y += f"  - {{ op: transfer, from: {{ vial: V_water }}, to: {{ plate: P1, well: {w} }}, volume_ul: 180 }}\n"
        y += f"  - {{ op: transfer, from: {{ vial: V_enzyme }}, to: {{ plate: P1, well: {w} }}, volume_ul: 20 }}\n"
    result = compile(world, Protocol.from_yaml(y), budget=Budget(sensor="scale_1", every=3))
    bare = simulate(world, result.pir)
    normal = simulate(world, result.pir, Noise.normal(), seeds=EXPECTED_SEEDS)
    two_percent_light = tel(auto_1={"mass_mg": 421.6}, auto_2={"mass_mg": 637.0}, auto_3={"mass_mg": 1009.5}, auto_4={"mass_mg": 1029.0})
    assert diff(result, bare, two_percent_light)[0].classification == "systematic", "with only sensor sigma, a healthy run looks broken"
    report, _ = diff(result, normal, two_percent_light)
    assert report.classification == "ok" and report.counts[Verdict.deviated.value] == 0
    assert all(e.sigma > 5 for e in report.entries), "the spread of a healthy run is now part of sigma"
    clogged = tel(auto_1={"mass_mg": 421.6}, auto_2={"mass_mg": 637.0}, auto_3={"mass_mg": 829.5}, auto_4={"mass_mg": 849.0})  # one 180 uL transfer never arrived
    report, _ = diff(result, normal, clogged)
    assert report.classification == "systematic" and report.counts[Verdict.deviated.value] == 2
    assert report.unaccounted["auto_3"] == pytest.approx(-200.5)
