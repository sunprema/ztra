"""The simulator: run PIR-H the way the lab would, with a bit of error, and record what
every sensor would read at each observe. The nominal run (no noise) is the prediction; the
seeded runs say how much the prediction should be trusted."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from ztra.compiler import PathCondition, deposit_liquid, paths, remove_liquid, take_liquids
from ztra.model import Strict
from ztra.pir import ObserveOp, PirH, Transform, TransformKind
from ztra.protocol import VialLoc, WellLoc
from ztra.sensors import Reading, read
from ztra.world import World
from ztra.world.hardware import Pipette
from ztra.world.inventory import ThermalState, total_ul


class Noise(Strict):
    """How the lab differs from the plan. All off by default; `pipette_accuracy` is what a
    normal run looks like, the rest are stress tests."""

    pipette_accuracy: bool = False  # use each pipette's accuracy spec: one bias per run, scatter per dispense
    dispense_drift: float = 0.0  # fraction lost on every dispense; 0.03 = 3% short
    jitter_ul: float = 0.0  # random error per dispense, standard deviation in µL
    failure_rate: float = 0.0  # chance a transfer delivers nothing (clogged tip, missed well)

    @staticmethod
    def normal() -> Noise:
        """What to expect from a healthy robot."""
        return Noise(pipette_accuracy=True)


class ReadingStats(Strict):
    """A reading in the nominal run, plus how it spread across the seeded runs."""

    label: str
    sensor: str
    entity: str
    unit: str
    sigma: float  # the sensor's own noise
    nominal: dict[str, float]
    mean: dict[str, float] = Field(default_factory=dict)
    std: dict[str, float] = Field(default_factory=dict)


class SimOutcome(Strict):
    conditions: list[dict[str, Any]]
    world_hash: str  # nominal final world
    readings: list[ReadingStats]
    samples: int
    events: dict[str, int]  # things that went wrong in the seeded runs, summed


class SimulationResult(Strict):
    noise: Noise
    seeds: int
    outcomes: list[SimOutcome]


@dataclass
class _Run:
    world: World
    rng: random.Random | None
    noise: Noise
    bias: dict[str, float] = field(default_factory=dict)  # per-pipette systematic error for this run
    readings: list[Reading] = field(default_factory=list)
    events: dict[str, int] = field(default_factory=lambda: {"failed_transfers": 0, "shortfalls": 0, "overflows": 0})
    named_tips: set[str] = field(default_factory=set)  # with_tip names holding a tip, plus "rack/name" for replenish


def simulate(world: World, pir: list[PirH], noise: Noise | None = None, seeds: int = 0, base_seed: int = 0) -> SimulationResult:
    """One nominal run per path, then `seeds` noisy runs per path."""
    noise = noise or Noise()
    outcomes = []
    for conditions, ops in paths(pir):
        nominal = _run(world, ops, None, noise)
        stats = {r.label: ReadingStats(label=r.label, sensor=r.sensor, entity=r.entity, unit=r.unit, sigma=r.sigma, nominal=dict(r.values)) for r in nominal.readings}
        samples: dict[str, dict[str, list[float]]] = {label: {m: [] for m in st.nominal} for label, st in stats.items()}
        events = dict(nominal.events)
        for s in range(seeds):
            run = _run(world, ops, random.Random(base_seed + s), noise)
            for r in run.readings:
                for m, v in r.values.items():
                    samples[r.label][m].append(v)
            for k, v in run.events.items():
                events[k] = events.get(k, 0) + v
        for label, st in stats.items():
            for m, xs in samples[label].items():
                if xs:
                    mean = sum(xs) / len(xs)
                    st.mean[m] = mean
                    st.std[m] = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs))
        outcomes.append(SimOutcome(conditions=[c.to_dict() for c in conditions], world_hash=nominal.world.hash(), readings=list(stats.values()), samples=seeds, events=events))
    return SimulationResult(noise=noise, seeds=seeds, outcomes=outcomes)


def _run(base: World, ops: list[Transform | ObserveOp], rng: random.Random | None, noise: Noise) -> _Run:
    run = _Run(base.clone(), rng, noise)
    if rng is not None and noise.pipette_accuracy:
        for p in base.hardware.pipettes:
            run.bias[p.name] = rng.gauss(0.0, p.accuracy.systematic_pct / 100.0)
    for op in ops:
        if isinstance(op, ObserveOp):
            run.readings.append(read(run.world, op.sensor, op.label))
            continue
        if op.kind is TransformKind.delay or op.kind is TransformKind.tip:
            continue  # nothing moves while waiting; a tip scope is bookkeeping, tips are taken at first use
        if op.kind is TransformKind.magnet:
            m = run.world.deck.modules.get(op.module or "")
            if m is not None:
                m.engaged, m.height_mm = bool(op.engaged), (op.height_mm if op.engaged else None)
            continue
        if op.kind is TransformKind.replenish:
            if op.rack in run.world.deck.tip_racks:
                run.world.deck.tip_racks[op.rack].used = []
            run.named_tips = {n for n in run.named_tips if not n.startswith(f"{op.rack}/")}
            continue
        if op.kind is TransformKind.thaw:
            loc = op.inputs[0].loc
            assert isinstance(loc, VialLoc)
            vial = run.world.inventory.vials[loc.vial]
            vial.state = ThermalState.thawed
            vial.freeze_thaw_cycles += 1
            continue
        # a fresh tip per transfer/mix, or one per with_tip — same as the compiler decided
        found = run.world.hardware.pipette_for(op.inputs[0].volume_ul, op.kind is TransformKind.transfer)
        pip = found[0] if found is not None else None
        if pip is not None and (op.tip_name is None or op.tip_name not in run.named_tips):
            taken = run.world.deck.take_tip(run.world.hardware, pip)
            if op.tip_name is not None and taken is not None:
                run.named_tips.add(op.tip_name)
                run.named_tips.add(f"{taken[0]}/{op.tip_name}")
        if op.kind is TransformKind.transfer:
            _transfer(run, op, pip)
        # mix: nothing moves
    return run


def _transfer(run: _Run, op: Transform, pip: Pipette | None) -> None:
    src, dst, vol = op.inputs[0].loc, op.outputs[0].loc, op.inputs[0].volume_ul
    available = _available(run.world, src)
    aspirated = min(vol, available)
    if aspirated < vol - 1e-9:
        run.events["shortfalls"] += 1
    delivered = aspirated
    if run.rng is not None:
        if run.rng.random() < run.noise.failure_rate:
            run.events["failed_transfers"] += 1
            delivered = 0.0
        else:
            delivered = aspirated * (1.0 - run.noise.dispense_drift) + run.rng.gauss(0.0, run.noise.jitter_ul)
            if run.noise.pipette_accuracy and pip is not None:
                acc = pip.accuracy
                delivered += aspirated * run.bias.get(pip.name, 0.0)
                delivered += run.rng.gauss(0.0, aspirated * acc.random_pct / 100.0 + acc.random_ul)
            delivered = max(delivered, 0.0)  # a positive bias can deliver a little more than planned
    liquids = take_liquids(run.world, src, delivered)
    remove_liquid(run.world, src, aspirated)  # what left the source; the difference stayed in the tip
    if isinstance(dst, WellLoc):
        cap = _capacity(run.world, dst)
        room = cap - total_ul(run.world.inventory.plates[dst.plate].wells.get(dst.well, []))
        if delivered > room + 1e-9:
            run.events["overflows"] += 1
            scale = max(room, 0.0) / delivered if delivered > 0 else 0.0
            liquids = [l.model_copy(update={"volume_ul": l.volume_ul * scale}) for l in liquids]
    deposit_liquid(run.world, dst, liquids)


def _available(world: World, loc: VialLoc | WellLoc) -> float:
    if isinstance(loc, VialLoc):
        return world.inventory.vials[loc.vial].volume_ul
    return total_ul(world.inventory.plates[loc.plate].wells.get(loc.well, []))


def _capacity(world: World, loc: WellLoc) -> float:
    d = world.hardware.labware[world.inventory.plates[loc.plate].labware]
    return d.well_max_ul if d.well_max_ul is not None else float("inf")


def nominal_world(world: World, ops: list[Transform | ObserveOp]) -> World:
    """The world after these straight-line ops with no noise at all."""
    return _run(world, ops, None, Noise()).world


def expected_readings(world: World, pir: list[PirH]) -> list[list[Reading]]:
    """Just the nominal readings, one list per path. Cheap; used by the store."""
    return [_run(world, ops, None, Noise()).readings for _, ops in paths(pir)]


def evaluate(condition_holds: PathCondition, reading: dict[str, float]) -> bool | None:
    """Does a reading satisfy a branch condition? None if the metric is not in the reading."""
    c = condition_holds.condition
    if c.metric not in reading:
        return None
    x = reading[c.metric]
    return {"gt": x > c.value, "ge": x >= c.value, "lt": x < c.value, "le": x <= c.value}[c.cmp.value]
