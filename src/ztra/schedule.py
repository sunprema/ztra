"""Observation scheduling: add observe steps to a program according to a budget.
More checkpoints cost run time and buy the ability to say *where* a run went wrong."""

from __future__ import annotations

from ztra.compiler_errors import CompileError
from ztra.model import Strict
from ztra.pir import Branch, ObserveOp, PirH, Transform, TransformKind
from ztra.world import World


class Budget(Strict):
    """Which sensor to read, and how often."""

    sensor: str
    every: int | None = None  # after every N transfers/mixes
    at_end: bool = True  # once more when the protocol finishes
    prefix: str = "auto"

    @staticmethod
    def parse(spec: str) -> Budget:
        """From a CLI string like `sensor=scale_1,every=3,end=false`."""
        fields: dict[str, str] = {}
        for part in spec.split(","):
            if "=" not in part:
                raise ValueError(f"budget part '{part}' should look like key=value")
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
        if "sensor" not in fields:
            raise ValueError("budget needs sensor=<id>")
        every = int(fields["every"]) if "every" in fields else None
        at_end = fields.get("end", "true").lower() in ("true", "yes", "1")
        return Budget(sensor=fields["sensor"], every=every, at_end=at_end, prefix=fields.get("prefix", "auto"))


def schedule(world: World, pir: list[PirH], budget: Budget) -> list[PirH]:
    """Return a copy of the program with observe ops added. Labels are `<prefix>_1`, `<prefix>_2`, …"""
    sensor = world.hardware.sensors.get(budget.sensor)
    if sensor is None:
        raise CompileError("E_UNKNOWN_SENSOR", "the observation budget must name a sensor from Hardware.sensors", budget.sensor, f"one of {sorted(world.hardware.sensors)}", budget.sensor, "declare the sensor in Hardware.yaml")
    if budget.every is not None and budget.every < 1:
        raise CompileError("E_BUDGET", "`every` must be at least 1", "budget", ">= 1", str(budget.every), "use every=N with N >= 1 or drop it")
    counter = [0, 0]  # transfers seen, labels issued

    def observe(origin: "Origin") -> ObserveOp:
        counter[1] += 1
        assert sensor is not None
        return ObserveOp(sensor=budget.sensor, entity=sensor.observes.entity, label=f"{budget.prefix}_{counter[1]}", origin=origin)

    def walk(ops: list[PirH]) -> list[PirH]:
        out: list[PirH] = []
        for op in ops:
            if isinstance(op, Branch):
                out.append(Branch(observation=op.observation, condition=op.condition, then=walk(op.then), otherwise=walk(op.otherwise), origin=op.origin))
                continue
            out.append(op)
            if budget.every is not None and isinstance(op, Transform) and op.kind in (TransformKind.transfer, TransformKind.mix) and op.channel in (None, 0):
                counter[0] += 1
                if counter[0] % budget.every == 0:
                    out.append(observe(op.origin))
        return out

    scheduled = walk(pir)
    if budget.at_end:
        last = pir[-1].origin if pir else Origin()
        scheduled.append(observe(last))
    return scheduled


from ztra.pir import Origin  # noqa: E402
