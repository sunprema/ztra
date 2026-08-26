"""Pre-flight: what a protocol needs versus what the lab has, in one go.

The compiler stops at the first impossible step. This walks every path to the end,
tallying stock per vial and reagent, tips per pipette, peak well volumes and frozen vials
used without a thaw, so an agent sees the whole budget before fixing anything."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ztra.compiler import _Unroller, paths
from ztra.model import Strict
from ztra.pir import ObserveOp, Transform, TransformKind
from ztra.protocol import Protocol, VialLoc, WellLoc
from ztra.schedule import Budget, schedule
from ztra.world import World
from ztra.world.hardware import fmt
from ztra.world.inventory import ThermalState, total_ul


class Demand(Strict):
    needed: float
    available: float
    shortfall: float  # 0 when there is enough
    unit: str


class WellPeak(Strict):
    plate: str
    well: str
    peak_ul: float
    capacity_ul: float


class Preflight(Strict):
    feasible: bool  # nothing is short, nothing overflows, nothing is frozen
    paths: int  # branch paths considered; figures are the worst case across them
    vials: dict[str, Demand] = Field(default_factory=dict)  # stock drawn from each vial
    reagents: dict[str, Demand] = Field(default_factory=dict)  # same, summed per reagent, against every vial of it
    tips: dict[str, Demand] = Field(default_factory=dict)  # per pipette
    wells_over_capacity: list[WellPeak] = Field(default_factory=list)
    frozen_used: list[str] = Field(default_factory=list)  # vials aspirated while still frozen
    summary: list[str] = Field(default_factory=list)


def preflight(world: World, protocol: Protocol, budget: Budget | None = None) -> Preflight:
    pir = _Unroller(world).unroll(protocol.steps, [], [])
    if budget is not None:
        pir = schedule(world, pir, budget)
    hw = world.hardware
    inv = world.inventory

    vial_need: dict[str, float] = {}
    tip_need: dict[str, int] = {}
    peaks: dict[tuple[str, str], float] = {}
    frozen: list[str] = []

    all_paths = paths(pir)
    for _, ops in all_paths:
        v_need: dict[str, float] = {}
        t_need: dict[str, int] = {}
        wells: dict[tuple[str, str], float] = {(pid, w): total_ul(c) for pid, p in inv.plates.items() for w, c in p.wells.items()}
        thawed = {vid for vid, v in inv.vials.items() if v.state is ThermalState.thawed}
        for op in ops:
            if isinstance(op, ObserveOp) or op.kind is TransformKind.delay:
                continue
            vol = op.inputs[0].volume_ul
            if op.kind is TransformKind.thaw:
                loc = op.inputs[0].loc
                assert isinstance(loc, VialLoc)
                thawed.add(loc.vial)
                continue
            found = hw.pipette_for(vol, op.kind is TransformKind.transfer)
            if found is not None:
                t_need[found[0].name] = t_need.get(found[0].name, 0) + 1
            if op.kind is TransformKind.mix:
                continue
            src, dst = op.inputs[0].loc, op.outputs[0].loc
            if isinstance(src, VialLoc):
                v_need[src.vial] = v_need.get(src.vial, 0.0) + vol
                if src.vial in inv.vials and src.vial not in thawed and src.vial not in frozen:
                    frozen.append(src.vial)
            else:
                key = (src.plate, src.well)
                wells[key] = max(0.0, wells.get(key, 0.0) - vol)
            if isinstance(dst, WellLoc):
                key = (dst.plate, dst.well)
                wells[key] = wells.get(key, 0.0) + vol
                peaks[key] = max(peaks.get(key, 0.0), wells[key])
            else:
                v_need[dst.vial] = v_need.get(dst.vial, 0.0) - vol  # pooling back into a vial is a credit
        for k, amount in v_need.items():
            vial_need[k] = max(vial_need.get(k, 0.0), amount)
        for k, n in t_need.items():
            tip_need[k] = max(tip_need.get(k, 0), n)

    vials: dict[str, Demand] = {}
    reagent_need: dict[str, float] = {}
    for vid, need in sorted(vial_need.items()):
        v = inv.vials.get(vid)
        have = 0.0 if v is None or v.consumed else v.volume_ul
        vials[vid] = Demand(needed=need, available=have, shortfall=max(0.0, need - have), unit="uL")
        if v is not None:
            reagent_need[v.reagent] = reagent_need.get(v.reagent, 0.0) + need
    reagents: dict[str, Demand] = {}
    for reagent, need in sorted(reagent_need.items()):
        have = sum(x.volume_ul for x in inv.vials.values() if x.reagent == reagent and not x.consumed)
        reagents[reagent] = Demand(needed=need, available=have, shortfall=max(0.0, need - have), unit="uL")

    tips: dict[str, Demand] = {}
    for pip in hw.pipettes:
        need_n = tip_need.get(pip.name, 0)
        if need_n == 0:
            continue
        free = 0
        for rid, rack in world.deck.tip_racks.items():
            d = hw.labware.get(rack.labware)
            if d is not None and rack.labware in pip.tip_labware and world.deck.slot_of(rid) is not None:
                free += d.rows * d.cols - len(rack.used)
        tips[pip.name] = Demand(needed=need_n, available=free, shortfall=max(0, need_n - free), unit="tips")

    over: list[WellPeak] = []
    for (pid, well), peak in sorted(peaks.items()):
        plate = inv.plates.get(pid)
        d = hw.labware.get(plate.labware) if plate else None
        cap = d.well_max_ul if d is not None and d.well_max_ul is not None else float("inf")
        if peak > cap + 1e-9:
            over.append(WellPeak(plate=pid, well=well, peak_ul=peak, capacity_ul=cap))

    summary: list[str] = []
    for vid, dm in vials.items():
        if dm.shortfall > 0:
            reagent = inv.vials[vid].reagent if vid in inv.vials else "?"
            others = [o for o, v in inv.vials.items() if o != vid and v.reagent == reagent and not v.consumed and v.volume_ul > 0]
            extra = f"; other {reagent} vials: {', '.join(f'{o} ({fmt(inv.vials[o].volume_ul)} uL)' for o in others)}" if others else f"; no other {reagent} vial"
            summary.append(f"{vid}: needs {fmt(dm.needed)} uL, has {fmt(dm.available)} uL, short by {fmt(dm.shortfall)} uL{extra}")
    for name, dm in tips.items():
        if dm.shortfall > 0:
            summary.append(f"{name}: needs {int(dm.needed)} tips, {int(dm.available)} free on the deck, short by {int(dm.shortfall)}")
    for wp in over:
        summary.append(f"{wp.plate}:{wp.well} would reach {fmt(wp.peak_ul)} uL; the labware holds {fmt(wp.capacity_ul)} uL")
    for vid in frozen:
        summary.append(f"{vid} is frozen and is used without a thaw step")
    feasible = not summary
    if feasible:
        parts = [f"{vid}: {fmt(dm.needed)}/{fmt(dm.available)} uL" for vid, dm in vials.items()]
        parts += [f"{name}: {int(dm.needed)}/{int(dm.available)} tips" for name, dm in tips.items()]
        summary.append("enough of everything — " + ", ".join(parts) if parts else "nothing is consumed")
    return Preflight(feasible=feasible, paths=len(all_paths), vials=vials, reagents=reagents, tips=tips, wells_over_capacity=over, frozen_used=frozen, summary=summary)


RESOURCE_ERRORS = {"E_VOLUME", "E_TIPS", "E_OVERFLOW", "E_CONSUMED", "E_STATE"}


def attach(error: dict[str, Any], world: World, protocol: Protocol, budget: Budget | None) -> dict[str, Any]:
    """Add the pre-flight summary to a resource-related compile error, so the whole shortfall
    is visible, not just the step that hit it."""
    if error.get("code") in RESOURCE_ERRORS:
        try:
            error["preflight"] = preflight(world, protocol, budget).summary
        except Exception:  # never let the extra help break the error itself
            pass
    return error
