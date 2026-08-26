"""Checks a loaded world for the things the schema can't catch: dangling references, wells off
the grid, a deck with no trash, two vials in one tube position, and so on.
Issues use the same shape as compiler errors so an agent can act on them."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from ztra.world.coords import WellCoord
from ztra.world.hardware import KNOWN_PIPETTES, LabwareDef, LabwareKind, RobotModel, SensorKind
from ztra.world.inventory import incompatible, parse_concentration, total_ul

if TYPE_CHECKING:
    from ztra.world import World

INVENTORY_FILE = "Inventory.yaml"
DECK_FILE = "Deck.yaml"
HARDWARE_FILE = "Hardware.yaml"
SCHEMA_VERSION = 1


class Severity(str, Enum):
    error = "error"
    warning = "warning"


@dataclass(frozen=True, order=True)
class Issue:
    severity: Severity
    code: str
    file: str
    path: str  # where the problem is, like plates.P1.wells.A1
    message: str
    hint: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


class _Ctx:
    def __init__(self, world: World) -> None:
        self.w = world
        self.issues: list[Issue] = []

    def error(self, code: str, file: str, path: str, message: str, hint: str) -> None:
        self.issues.append(Issue(Severity.error, code, file, path, message, hint))

    def warn(self, code: str, file: str, path: str, message: str, hint: str) -> None:
        self.issues.append(Issue(Severity.warning, code, file, path, message, hint))

    def labware_of(self, entity: str) -> LabwareDef | None:
        """Find the labware definition for a plate, tube rack or tip rack."""
        w = self.w
        if entity in w.inventory.plates:
            key = w.inventory.plates[entity].labware
        elif entity in w.deck.tube_racks:
            key = w.deck.tube_racks[entity].labware
        elif entity in w.deck.tip_racks:
            key = w.deck.tip_racks[entity].labware
        else:
            return None
        return w.hardware.labware.get(key)

    def check_well(self, code: str, file: str, path: str, well: str, definition: LabwareDef) -> bool:
        c = WellCoord.parse(well)
        if c is not None and c.within(definition.rows, definition.cols):
            return True
        last = WellCoord(definition.rows - 1, definition.cols - 1).name
        self.error(code, file, path, f"well '{well}' is not on a {definition.rows}x{definition.cols} grid", f"use A1..{last}")
        return False


def validate(world: World) -> list[Issue]:
    c = _Ctx(world)
    _versions(c)
    _hardware(c)
    _inventory(c)
    _deck(c)
    return sorted(set(c.issues))


def _versions(c: _Ctx) -> None:
    for file, v in [(INVENTORY_FILE, c.w.inventory.version), (DECK_FILE, c.w.deck.version), (HARDWARE_FILE, c.w.hardware.version)]:
        if v != SCHEMA_VERSION:
            c.error("W_VERSION", file, "version", f"schema version {v} is not supported", f"this build reads version {SCHEMA_VERSION}")


def _parse_api_level(s: str) -> tuple[int, int] | None:
    parts = s.split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _hardware(c: _Ctx) -> None:
    hw = c.w.hardware
    f = HARDWARE_FILE
    model = hw.robot.model

    if hw.robot.api_level is not None:
        lvl = _parse_api_level(hw.robot.api_level)
        lo, hi = model.api_levels
        if lvl is None or not (lo <= lvl <= hi):
            c.error("W_API_LEVEL", f, "robot.api_level", f"'{hw.robot.api_level}' is not a Protocol API level {model.value} runs ({lo[0]}.{lo[1]}..{hi[0]}.{hi[1]})", "pick a level in that range")
        elif model is RobotModel.flex and lvl < (2, 16):
            c.error("W_API_LEVEL", f, "robot.api_level", "Flex needs apiLevel 2.16 or later so the trash bin can be loaded", "use 2.16 or later")

    for name, d in hw.labware.items():
        p = f"labware.{name}"
        if d.rows <= 0 or d.cols <= 0:
            c.error("W_LABWARE_GRID", f, p, "rows and cols must be >= 1", "check the labware definition")
        if d.height_mm <= 0:
            c.error("W_LABWARE_HEIGHT", f, p, "height_mm must be > 0", "needed for deck clearance checks")
        if d.well_depth_mm is not None and d.well_depth_mm <= 0:
            c.error("W_LABWARE_GEOMETRY", f, p, "well_depth_mm must be > 0", "take it from the vendor definition")
        if d.well_diameter_mm is not None and d.well_diameter_mm <= 0:
            c.error("W_LABWARE_GEOMETRY", f, p, "well_diameter_mm must be > 0", "take it from the vendor definition")
        if d.kind in (LabwareKind.plate, LabwareKind.reservoir, LabwareKind.tube_rack) and not (d.well_max_ul is not None and d.well_max_ul > 0):
            c.error("W_LABWARE_CAPACITY", f, p, "plates, reservoirs and tube racks need well_max_ul > 0", "add well_max_ul")
        if d.kind is LabwareKind.tip_rack and not (d.tip_volume_ul is not None and d.tip_volume_ul > 0):
            c.error("W_LABWARE_TIP_VOLUME", f, p, "tip racks need tip_volume_ul > 0", "add tip_volume_ul")

    mounts: set[str] = set()
    for i, pip in enumerate(hw.pipettes):
        p = f"pipettes[{i}]"
        if pip.min_ul <= 0 or pip.min_ul >= pip.max_ul:
            c.error("W_PIPETTE_RANGE", f, p, f"{pip.name}: need 0 < min_ul < max_ul (got {pip.min_ul}..{pip.max_ul})", "fix the volume range")
        if pip.mount.value in mounts:
            c.error("W_PIPETTE_MOUNT_DUP", f, p, f"mount {pip.mount.value} is used by more than one pipette", "one pipette per mount")
        mounts.add(pip.mount.value)
        if not pip.tip_labware:
            c.warn("W_PIPETTE_NO_TIPS", f, p, f"{pip.name} lists no compatible tip labware", "add tip_labware so lowering can allocate tips")
        a = pip.accuracy
        if a.systematic_pct < 0 or a.random_pct < 0 or a.random_ul < 0:
            c.error("W_PIPETTE_ACCURACY", f, p, "accuracy values must be >= 0", "use the vendor's accuracy/precision spec")
        elif a.systematic_pct > 25 or a.random_pct > 25:
            c.warn("W_PIPETTE_ACCURACY", f, p, f"accuracy of {a.systematic_pct}% / {a.random_pct}% is implausibly loose", "check the units: these are percentages of each volume")
        for t in pip.tip_labware:
            td = hw.labware.get(t)
            if td is None:
                c.error("W_PIPETTE_TIP_UNKNOWN", f, p, f"tip_labware '{t}' is not in the labware catalog", "add it under labware")
            elif td.kind is not LabwareKind.tip_rack:
                c.error("W_PIPETTE_TIP_KIND", f, p, f"tip_labware '{t}' is not kind: tip_rack", "point at a tip rack definition")
            elif model is RobotModel.flex and td.tip_volume_ul is not None and td.tip_volume_ul > pip.max_ul:
                c.warn("W_PIPETTE_TIP_TOO_BIG", f, p, f"Flex pipettes only take tips up to their own capacity; {t} holds {td.tip_volume_ul} uL", "use smaller tips")
        known = KNOWN_PIPETTES.get(pip.name)
        if known is None:
            c.warn("W_PIPETTE_UNKNOWN_NAME", f, p, f"'{pip.name}' is not a pipette name the Opentrons API documents", "check the spelling against the Opentrons docs")
        else:
            robot, vlo, vhi, channels = known
            if robot is not model:
                c.error("W_PIPETTE_ROBOT_MISMATCH", f, p, f"{pip.name} is a {robot.value} pipette but the robot is {model.value}", "use a pipette for this robot")
            if (pip.min_ul, pip.max_ul) != (vlo, vhi):
                c.warn("W_PIPETTE_RANGE_MISMATCH", f, p, f"{pip.name} is documented as {vlo}..{vhi} uL, not {pip.min_ul}..{pip.max_ul}", "match the vendor range unless you have a reason")
            if pip.channels != channels:
                c.warn("W_PIPETTE_CHANNELS_MISMATCH", f, p, f"{pip.name} has {channels} channels, not {pip.channels}", "fix channels")

    for name, s in hw.sensors.items():
        p = f"sensors.{name}"
        if s.sigma <= 0:
            c.error("W_SENSOR_SIGMA", f, p, "sigma must be > 0", "measure the sensor noise and record it")
        sd = c.labware_of(s.observes.entity)
        if sd is None:
            c.error("W_SENSOR_TARGET_UNKNOWN", f, p, f"observes.entity '{s.observes.entity}' is not a plate, tube rack or tip rack", "reference an entity from Inventory or Deck")
            continue
        for well in s.observes.wells:
            c.check_well("W_SENSOR_WELL_INVALID", f, p, well, sd)
        for col in s.observes.columns:
            if col < 1 or col > sd.cols:
                c.error("W_SENSOR_COLUMN_INVALID", f, p, f"column {col} is outside 1..{sd.cols}", "use 1-based column numbers")
        if s.kind is SensorKind.well_volume and not s.observes.wells and not s.observes.columns:
            c.warn("W_SENSOR_NO_WELLS", f, p, "well_volume sensor observes no wells", "list wells or columns; otherwise every well is UNOBSERVED")

    r = hw.safe_envelope.temperature_c
    if r is not None and r.min >= r.max:
        c.error("W_ENVELOPE_RANGE", f, "safe_envelope.temperature_c", "min must be < max", "fix the range")


def _inventory(c: _Ctx) -> None:
    inv = c.w.inventory
    f = INVENTORY_FILE

    for name, r in inv.reagents.items():
        if r.density_mg_per_ul <= 0:
            c.error("W_DENSITY", f, f"reagents.{name}", "density_mg_per_ul must be > 0", "water is 1.0")
        if r.concentration is not None and parse_concentration(r.concentration) is None:
            c.warn("W_CONCENTRATION_FORMAT", f, f"reagents.{name}", f"concentration '{r.concentration}' is not '<number> <unit>'", 'write it like "1 M" or "10 U/uL" so dilutions can be computed')

    for vid, v in inv.vials.items():
        p = f"vials.{vid}"
        if v.reagent not in inv.reagents:
            c.error("W_REAGENT_UNKNOWN", f, p, f"reagent '{v.reagent}' is not defined", "add it under reagents with a hazard class")
        if v.volume_ul < 0:
            c.error("W_VOLUME_NEGATIVE", f, p, f"volume_ul is {v.volume_ul}", "volumes are >= 0")
        if v.consumed and v.volume_ul > 0:
            c.error("W_CONSUMED_MISMATCH", f, p, f"marked consumed but still holds {v.volume_ul} uL", "a consumed vial has volume 0; set one or the other")
        if not v.consumed and v.volume_ul == 0:
            c.warn("W_EMPTY_NOT_CONSUMED", f, p, "volume is 0 but vial is not marked consumed", "mark consumed: true so the compiler retires it")

    for pid, plate in inv.plates.items():
        p = f"plates.{pid}"
        d = c.w.hardware.labware.get(plate.labware)
        if d is None:
            c.error("W_LABWARE_UNKNOWN", f, p, f"labware '{plate.labware}' is not in the catalog", "add it to Hardware.labware")
            continue
        if d.kind not in (LabwareKind.plate, LabwareKind.reservoir):
            c.error("W_LABWARE_KIND", f, p, f"labware '{plate.labware}' is {d.kind.value}, expected plate or reservoir", "use a plate or reservoir definition")
            continue
        if d.kind is LabwareKind.plate and (d.rows, d.cols) != (8, 12):
            c.error("W_PLATE_NOT_96", f, p, f"plate is {d.rows}x{d.cols}; v0.1 supports SBS 96-well only", "use an 8x12 plate, or kind: reservoir for troughs")
        if plate.waste and d.kind is not LabwareKind.reservoir:
            c.warn("W_WASTE_NOT_RESERVOIR", f, p, "a liquid waste is normally a reservoir", "use a reservoir labware, or drop waste: true")
        cap = d.well_max_ul if d.well_max_ul is not None else float("inf")
        for well, contents in plate.wells.items():
            wp = f"{p}.wells.{well}"
            c.check_well("W_WELL_INVALID", f, wp, well, d)
            hazards = []
            for i, liquid in enumerate(contents):
                rg = inv.reagents.get(liquid.reagent)
                if rg is None:
                    c.error("W_REAGENT_UNKNOWN", f, f"{wp}[{i}]", f"reagent '{liquid.reagent}' is not defined", "add it under reagents")
                else:
                    hazards.append(rg.hazard)
                if liquid.volume_ul < 0:
                    c.error("W_VOLUME_NEGATIVE", f, f"{wp}[{i}]", f"volume_ul is {liquid.volume_ul}", "volumes are >= 0")
            total = total_ul(contents)
            if total > cap:
                c.error("W_WELL_OVERFLOW", f, wp, f"holds {total} uL but labware allows {cap} uL", "the recorded state is impossible; correct the inventory")
            for i in range(len(hazards)):
                for j in range(i + 1, len(hazards)):
                    if incompatible(hazards[i], hazards[j]):
                        c.warn("W_HAZARD_MIX", f, wp, f"well already contains incompatible classes {hazards[i].value} and {hazards[j].value}", "verify the recorded contents; the compiler would have refused this")


def _deck(c: _Ctx) -> None:
    w = c.w
    d = w.deck
    f = DECK_FILE
    model = w.hardware.robot.model

    seen: dict[str, str] = {}
    for ns, ids in [("plates", list(w.inventory.plates)), ("tube_racks", list(d.tube_racks)), ("tip_racks", list(d.tip_racks))]:
        for eid in ids:
            if eid in seen:
                c.error("W_ENTITY_ID_DUP", f, f"{ns}.{eid}", f"id '{eid}' is also used in {seen[eid]}", "entity ids are global")
            seen[eid] = ns

    for rid, r in d.tube_racks.items():
        ld = w.hardware.labware.get(r.labware)
        if ld is None:
            c.error("W_LABWARE_UNKNOWN", f, f"tube_racks.{rid}", f"labware '{r.labware}' is not in the catalog", "add it to Hardware.labware")
        elif ld.kind is not LabwareKind.tube_rack:
            c.error("W_LABWARE_KIND", f, f"tube_racks.{rid}", f"labware '{r.labware}' is {ld.kind.value}, expected tube_rack", "use a tube rack definition")

    for tid, t in d.tip_racks.items():
        p = f"tip_racks.{tid}"
        ld = w.hardware.labware.get(t.labware)
        if ld is None:
            c.error("W_LABWARE_UNKNOWN", f, p, f"labware '{t.labware}' is not in the catalog", "add it to Hardware.labware")
            continue
        if ld.kind is not LabwareKind.tip_rack:
            c.error("W_LABWARE_KIND", f, p, f"labware '{t.labware}' is {ld.kind.value}, expected tip_rack", "use a tip rack definition")
            continue
        used: set[str] = set()
        for well in t.used:
            if c.check_well("W_TIP_USED_INVALID", f, p, well, ld):
                if well in used:
                    c.error("W_TIP_USED_DUP", f, p, f"tip {well} listed as used more than once", "list each used tip once")
                used.add(well)
        if not any(t.labware in pip.tip_labware for pip in w.hardware.pipettes):
            c.warn("W_TIP_LABWARE_UNUSABLE", f, p, f"no pipette lists '{t.labware}' as compatible", "add it to a pipette's tip_labware or remove the rack")

    placed: dict[str, str] = {}
    trash_slots: list[str] = []
    for slot, s in d.slots.items():
        p = f"slots.{slot}"
        if not model.valid_slot(slot):
            hint = 'OT-2 slots are "1".."12"' if model is RobotModel.ot2 else 'Flex slots are "A1".."D4"'
            c.error("W_SLOT_INVALID", f, p, f"'{slot}' is not a slot on {model.value}", hint)
        if (s.entity is None) == (not s.trash):
            c.error("W_SLOT_CONTENT", f, p, "a slot holds exactly one of entity / trash: true", "set one of them")
        elif s.trash:
            trash_slots.append(slot)
        else:
            assert s.entity is not None
            if c.labware_of(s.entity) is None:
                c.error("W_SLOT_ENTITY_UNKNOWN", f, p, f"entity '{s.entity}' is not a plate, tube rack or tip rack", "define it in Inventory.plates, Deck.tube_racks or Deck.tip_racks")
            if s.entity in placed:
                c.error("W_ENTITY_DUPLICATE_SLOT", f, p, f"entity '{s.entity}' is also in slot {placed[s.entity]}", "an entity occupies one slot")
            placed[s.entity] = slot
    if not trash_slots:
        c.error("W_TRASH_MISSING", f, "slots", "no trash slot declared", "add a slot with trash: true")
    elif len(trash_slots) > 1:
        c.error("W_TRASH_MULTIPLE", f, "slots", f"{len(trash_slots)} trash slots declared", "declare exactly one trash")
    fixed = model.fixed_trash_slot
    if fixed is not None:
        for ts in trash_slots:
            if ts != fixed:
                c.error("W_TRASH_SLOT", f, f"slots.{ts}", f"{model.value} has a fixed trash in slot {fixed}", f"move trash to slot {fixed}")
    for eid in list(w.inventory.plates) + list(d.tube_racks) + list(d.tip_racks):
        if eid not in placed:
            c.warn("W_ENTITY_NOT_ON_DECK", f, "slots", f"entity '{eid}' is defined but not placed in any slot", "the robot cannot reach it; place it or accept that protocols touching it will fail lowering")

    addresses: dict[tuple[str, str], str] = {}
    for vial, link in d.linker.items():
        p = f"linker.{vial}"
        if vial not in w.inventory.vials:
            c.error("W_LINK_TARGET_UNKNOWN", f, p, f"'{vial}' is not a vial in Inventory", "remove the entry or add the vial")
        rack = d.tube_racks.get(link.rack)
        if rack is None:
            c.error("W_LINK_RACK_UNKNOWN", f, p, f"rack '{link.rack}' is not in Deck.tube_racks", "define the tube rack")
            continue
        ld = w.hardware.labware.get(rack.labware)
        if ld is not None:
            c.check_well("W_LINK_WELL_INVALID", f, p, link.well, ld)
        key = (link.rack, link.well)
        if key in addresses:
            c.error("W_LINK_COLLISION", f, p, f"{link.rack}:{link.well} is already assigned to vial '{addresses[key]}'", "one vial per rack position")
        addresses[key] = vial
    for vial in w.inventory.vials:
        if vial not in d.linker:
            c.warn("W_LINK_MISSING", f, f"linker.{vial}", f"vial '{vial}' has no deck address", "add a linker entry; without it lowering to PIR-L fails")
