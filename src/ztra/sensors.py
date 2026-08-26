"""What a sensor would read from a given world, and the shape of what a real sensor sent back.

A reading is a small dict of numbers keyed by metric: a scale gives {"mass_mg": ...}; a camera or
level sensor gives one entry per well it can see, keyed by well name."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, ValidationError

from ztra.model import Strict
from ztra.world import World, format_validation_error
from ztra.world.coords import WellCoord
from ztra.world.hardware import LabwareDef, Sensor, SensorKind
from ztra.world.inventory import total_ul


class Reading(Strict):
    """A sensor's view of one entity at one moment."""

    label: str
    sensor: str
    kind: SensorKind
    entity: str
    values: dict[str, float]
    sigma: float
    unit: str


def wells_seen(world: World, sensor: Sensor) -> list[str]:
    """The wells a sensor can see: the listed ones plus every well in the listed columns."""
    definition = _labware_of(world, sensor.observes.entity)
    names = list(sensor.observes.wells)
    if definition is not None:
        for col in sensor.observes.columns:
            names += [WellCoord(row, col - 1).name for row in range(definition.rows)]
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def read(world: World, sensor_id: str, label: str) -> Reading:
    """The exact reading a perfect sensor would give for this world. Noise is the caller's business."""
    sensor = world.hardware.sensors[sensor_id]
    entity = sensor.observes.entity
    values: dict[str, float] = {}
    if sensor.kind is SensorKind.plate_mass:
        values["mass_mg"] = _mass_mg(world, entity)
    elif sensor.kind is SensorKind.well_volume:
        plate = world.inventory.plates.get(entity)
        for w in wells_seen(world, sensor):
            values[w] = total_ul(plate.wells.get(w, [])) if plate is not None else 0.0
    # temperature: nothing is modelled yet, so no values
    return Reading(label=label, sensor=sensor_id, kind=sensor.kind, entity=entity, values=values, sigma=sensor.sigma, unit=sensor.unit)


def _mass_mg(world: World, entity: str) -> float:
    """Mass of the liquid in a plate or tube rack. Labware tare is not included."""
    density = {name: r.density_mg_per_ul for name, r in world.inventory.reagents.items()}
    if entity in world.inventory.plates:
        return sum(l.volume_ul * density.get(l.reagent, 1.0) for contents in world.inventory.plates[entity].wells.values() for l in contents)
    if entity in world.deck.tube_racks:
        total = 0.0
        for vid, link in world.deck.linker.items():
            vial = world.inventory.vials.get(vid)
            if vial is not None and link.rack == entity:
                total += vial.volume_ul * density.get(vial.reagent, 1.0)
        return total
    return 0.0


def _labware_of(world: World, entity: str) -> LabwareDef | None:
    key: str | None = None
    if entity in world.inventory.plates:
        key = world.inventory.plates[entity].labware
    elif entity in world.deck.tube_racks:
        key = world.deck.tube_racks[entity].labware
    elif entity in world.deck.tip_racks:
        key = world.deck.tip_racks[entity].labware
    return world.hardware.labware.get(key) if key is not None else None


# ---------------------------------------------------------------- what came back from the lab


class TelemetryReading(Strict):
    label: str
    sensor: str
    values: dict[str, float]
    at: str | None = None  # timestamp, if the instrument gave one


class Telemetry(Strict):
    """Readings taken during a run, keyed by the observe labels in the protocol."""

    readings: list[TelemetryReading] = Field(default_factory=list)
    notes: str | None = None

    @staticmethod
    def load(path: Path) -> Telemetry:
        try:
            data = yaml.safe_load(path.read_text())
            return Telemetry.model_validate(data)
        except OSError as e:
            raise ValueError(f"{path}: {e}") from e
        except yaml.YAMLError as e:
            raise ValueError(f"{path}: not valid YAML: {e}") from e
        except ValidationError as e:
            raise ValueError(f"{path}: {format_validation_error(e)}") from e

    def by_label(self) -> dict[str, TelemetryReading]:
        return {r.label: r for r in self.readings}
