"""A short, agent-friendly picture of a world: what's there, how much, what's free."""

from __future__ import annotations

from typing import Any

from ztra.world import Severity, World, validate
from ztra.world.coords import WellCoord
from ztra.world.inventory import describe_mixture, total_ul


def summary(world: World) -> dict[str, Any]:
    issues = validate(world)
    tips_free: dict[str, int] = {}
    for rid, rack in world.deck.tip_racks.items():
        d = world.hardware.labware.get(rack.labware)
        if d is not None:
            tips_free[rid] = d.rows * d.cols - len(rack.used)
    plates = {}
    for pid, plate in world.inventory.plates.items():
        filled = [(w, c) for w, c in sorted(plate.wells.items(), key=lambda kv: (WellCoord.parse(kv[0]) or WellCoord(99, 99))) if c]
        wells = {w: round(total_ul(c), 3) for w, c in filled}
        mixtures = {w: describe_mixture(c, world.inventory.reagents) for w, c in filled}
        plates[pid] = {"labware": plate.labware, "slot": world.deck.slot_of(pid), "filled_wells": len(wells), "wells_ul": wells, "mixtures": mixtures}
    return {
        "hash": world.hash(),
        "robot": {"model": world.hardware.robot.model.value, "api_level": world.hardware.robot.api_level},
        "pipettes": [f"{p.name} ({p.mount.value}, {p.min_ul}-{p.max_ul} uL)" for p in world.hardware.pipettes],
        "vials": {
            vid: {"reagent": v.reagent, "volume_ul": v.volume_ul, "state": v.state.value, "consumed": v.consumed, "address": (f"{link.rack}:{link.well}" if (link := world.deck.linker.get(vid)) else None)}
            for vid, v in world.inventory.vials.items()
        },
        "plates": plates,
        "modules": {mid: f"{m.kind} in slot {m.slot}" + (f" holding {m.holds}" if m.holds else "") + (f", engaged at {m.height_mm:g} mm" if m.engaged and m.height_mm is not None else ", disengaged") for mid, m in world.deck.modules.items()},
        "tips_free": tips_free,
        "sensors": {sid: f"{s.kind.value} on {s.observes.entity} (sigma {s.sigma} {s.unit})" for sid, s in world.hardware.sensors.items()},
        "hazards": {name: r.hazard.value for name, r in world.inventory.reagents.items()},
        "errors": [i.code for i in issues if i.severity is Severity.error],
        "warnings": [f"{i.code}: {i.message}" for i in issues if i.severity is Severity.warning],
    }
