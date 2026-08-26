"""The telemetry service: one adapter per instrument, readings collected by label, and the
safety interlock — a reading outside the safe envelope stops the run."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any, Protocol

from ztra.sensors import Telemetry, TelemetryReading, read
from ztra.world import World
from ztra.world.hardware import Hardware, SensorKind


class EStop(Exception):
    """Something is outside the safe operating envelope. The runtime stops the robot."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


class SensorAdapter(Protocol):
    def read(self, sensor_id: str) -> dict[str, float]:
        """Take one reading now."""
        ...


class SimulatedSensor:
    """Reads a (hidden) physical world and adds the sensor's own noise."""

    def __init__(self, source: Callable[[], World], seed: int = 0, noisy: bool = True) -> None:
        self.source = source
        self.rng = random.Random(seed)
        self.noisy = noisy

    def read(self, sensor_id: str) -> dict[str, float]:
        world = self.source()
        sensor = world.hardware.sensors[sensor_id]
        values = read(world, sensor_id, "now").values
        if self.noisy:
            values = {k: v + self.rng.gauss(0.0, sensor.sigma) for k, v in values.items()}
        return values


class FixedSensor:
    """Always answers the same thing. For tests and for pretending an instrument misbehaves."""

    def __init__(self, values: dict[str, float]) -> None:
        self.values = values

    def read(self, sensor_id: str) -> dict[str, float]:
        return dict(self.values)


class TelemetryService:
    def __init__(self, hardware: Hardware, adapters: dict[str, SensorAdapter], clock: Callable[[], str] | None = None) -> None:
        self.hardware = hardware
        self.adapters = adapters
        self.clock = clock
        self.readings: list[TelemetryReading] = []

    def read(self, sensor_id: str, label: str) -> TelemetryReading:
        adapter = self.adapters.get(sensor_id)
        if adapter is None:
            raise EStop("E_NO_ADAPTER", f"no adapter for sensor '{sensor_id}'; the run cannot take reading '{label}'")
        values = adapter.read(sensor_id)
        self.check_envelope(sensor_id, values)
        r = TelemetryReading(label=label, sensor=sensor_id, values=values, at=self.clock() if self.clock else None)
        self.readings.append(r)
        return r

    def check_envelope(self, sensor_id: str, values: dict[str, float]) -> None:
        sensor = self.hardware.sensors.get(sensor_id)
        env = self.hardware.safe_envelope
        if sensor is not None and sensor.kind is SensorKind.temperature and env.temperature_c is not None:
            for k, v in values.items():
                if not (env.temperature_c.min <= v <= env.temperature_c.max):
                    raise EStop("E_STOP_TEMPERATURE", f"{sensor_id} read {v} {sensor.unit} ({k}); safe range is {env.temperature_c.min}..{env.temperature_c.max}")

    def telemetry(self, notes: str | None = None) -> Telemetry:
        return Telemetry(readings=list(self.readings), notes=notes)
