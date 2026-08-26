"""The diff engine: what the lab reported versus what the simulator expected.

The report is only as fine as the sensors: every expected reading gets a verdict, and the
summary says whether a deviation could be pinned to a well at all. It also produces the best
guess of the world after the run, which is what the store records as observed."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import Field

from ztra.compiler import CompileOutput, PathCondition, deposit_liquid, remove_liquid
from ztra.model import Strict
from ztra.protocol import WellLoc
from ztra.sensors import Telemetry
from ztra.simulate import ReadingStats, SimulationResult, evaluate
from ztra.world import World
from ztra.world.hardware import SensorKind
from ztra.world.inventory import Liquid, total_ul

THRESHOLD_SIGMAS = 3.0


class Verdict(str, Enum):
    verified = "VERIFIED_WITHIN_SENSOR_NOISE"
    deviated = "DEVIATED"
    unobserved = "UNOBSERVED"


class Entry(Strict):
    label: str
    sensor: str
    entity: str
    metric: str  # "mass_mg", or a well name
    predicted: float
    observed: float | None = None
    delta: float | None = None
    sigma: float  # sensor noise combined with simulation spread
    verdict: Verdict


class WorldDiff(Strict):
    outcome: int
    conditions: list[dict[str, Any]]
    entries: list[Entry]
    counts: dict[str, int]
    classification: Literal["ok", "systematic", "localized", "unobserved"]
    can_localize: bool
    unaccounted: dict[str, float] = Field(default_factory=dict)  # aggregate deviations nobody could pin to a well
    notes: list[str] = Field(default_factory=list)
    observed_world_hash: str


class DiffError(Exception):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def resolve_outcome(compiled: CompileOutput, telemetry: Telemetry) -> int:
    """Which predicted outcome happened, judged from the readings the branches tested."""
    by_label = telemetry.by_label()
    for i, outcome in enumerate(compiled.outcomes):
        if all(_matches(c, by_label) for c in outcome.conditions):
            return i
    tested = sorted({c.observation for o in compiled.outcomes for c in o.conditions})
    raise DiffError("D_UNRESOLVED", f"the readings do not decide the branches; needed readings for {tested}", "supply the labelled readings the protocol branches on, or pass the outcome explicitly")


def _matches(c: PathCondition, by_label: dict[str, Any]) -> bool:
    r = by_label.get(c.observation)
    if r is None:
        return False
    got = evaluate(c, r.values)
    return got is not None and got == c.holds


def diff(compiled: CompileOutput, sim: SimulationResult, telemetry: Telemetry, outcome: int | None = None) -> tuple[WorldDiff, World]:
    """Compare telemetry with the expected readings of one outcome. Returns the report and
    the estimated world after the run."""
    idx = resolve_outcome(compiled, telemetry) if outcome is None else outcome
    if idx < 0 or idx >= len(sim.outcomes):
        raise DiffError("D_BAD_OUTCOME", f"outcome {idx} does not exist; there are {len(sim.outcomes)}")
    expected = sim.outcomes[idx]
    predicted_world = compiled.outcomes[idx].world
    by_label = telemetry.by_label()

    entries: list[Entry] = []
    notes: list[str] = []
    for st in expected.readings:
        got = by_label.get(st.label)
        if got is not None and got.sensor != st.sensor:
            notes.append(f"reading '{st.label}' came from {got.sensor}, expected {st.sensor}; ignored")
            got = None
        for metric, pred in st.nominal.items():
            sigma = math.hypot(st.sigma, st.std.get(metric, 0.0))
            if got is None or metric not in got.values:
                entries.append(Entry(label=st.label, sensor=st.sensor, entity=st.entity, metric=metric, predicted=pred, sigma=sigma, verdict=Verdict.unobserved))
                continue
            obs = got.values[metric]
            delta = obs - pred
            verdict = Verdict.deviated if abs(delta) > THRESHOLD_SIGMAS * sigma else Verdict.verified
            entries.append(Entry(label=st.label, sensor=st.sensor, entity=st.entity, metric=metric, predicted=pred, observed=obs, delta=delta, sigma=sigma, verdict=verdict))
    expected_labels = {st.label for st in expected.readings}
    for label in by_label:
        if label not in expected_labels:
            notes.append(f"reading '{label}' was not expected on this path; ignored")

    counts = {v.value: sum(1 for e in entries if e.verdict is v) for v in Verdict}
    per_well = [e for e in entries if e.metric != "mass_mg"]
    aggregate = [e for e in entries if e.metric == "mass_mg"]
    can_localize = any(e.verdict is not Verdict.unobserved for e in per_well)
    if counts[Verdict.deviated.value] == 0:
        classification: Literal["ok", "systematic", "localized", "unobserved"] = "ok" if counts[Verdict.verified.value] > 0 else "unobserved"
    elif any(e.verdict is Verdict.deviated for e in per_well):
        classification = "localized"
    else:
        classification = "systematic"

    observed_world, unaccounted = _estimate_world(predicted_world, entries, aggregate, notes)
    if classification == "systematic":
        notes.append("total is off but no observed well is: calibration drift or a failure in an unobserved well")
    if classification == "unobserved":
        notes.append("nothing was measured; the observed world is the prediction")

    report = WorldDiff(
        outcome=idx,
        conditions=expected.conditions,
        entries=entries,
        counts=counts,
        classification=classification,
        can_localize=can_localize,
        unaccounted=unaccounted,
        notes=notes,
        observed_world_hash=observed_world.hash(),
    )
    return report, observed_world


def _estimate_world(predicted: World, entries: list[Entry], aggregate: list[Entry], notes: list[str]) -> tuple[World, dict[str, float]]:
    """Start from the predicted final world and fold in what deviated: a well that read
    off gets its final volume shifted by the same amount (later steps are assumed accurate).
    Aggregate deviations cannot be placed, so they are reported as unaccounted."""
    world = predicted.clone()
    last: dict[tuple[str, str], Entry] = {}
    for e in entries:
        if e.metric != "mass_mg" and e.verdict is Verdict.deviated:
            last[(e.entity, e.metric)] = e  # readings are in program order; the last one wins
    for (entity, well), e in last.items():
        assert e.delta is not None
        plate = world.inventory.plates.get(entity)
        if plate is None:
            continue
        loc = WellLoc(plate=entity, well=well)
        contents = plate.wells.get(well, [])
        if e.delta < 0:
            remove_liquid(world, loc, min(-e.delta, total_ul(contents)))
        elif contents:
            total = total_ul(contents)
            deposit_liquid(world, loc, [Liquid(reagent=l.reagent, volume_ul=e.delta * l.volume_ul / total) for l in contents])
        else:
            notes.append(f"{entity}:{well} read {e.observed} but was predicted empty; contents unknown, left empty")
    unaccounted = {e.label: e.delta for e in aggregate if e.verdict is Verdict.deviated and e.delta is not None}
    return world, unaccounted


def sensor_kind(sim_reading: ReadingStats, world: World) -> SensorKind:
    return world.hardware.sensors[sim_reading.sensor].kind
