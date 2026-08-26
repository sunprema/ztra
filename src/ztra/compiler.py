"""The compiler. Two passes:
1. Unroll the protocol into PIR-H (expand loops, resolve observation labels).
2. Run PIR-H against a copy of the world and check every step. At a branch we split
   the world and check each side separately, so the result is one predicted outcome
   per path. A protocol without branches has exactly one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ztra.pir import Branch, ObserveOp, Origin, PirH, Port, Transform, TransformKind
from ztra.compiler_errors import CompileError
from ztra.schedule import Budget, schedule
from ztra.protocol import (
    PROTOCOL_VERSION,
    Condition,
    Delay,
    ForEach,
    ForWells,
    IfObserved,
    Loc,
    Mix,
    Observe,
    Protocol,
    Repeat,
    Step,
    Thaw,
    Transfer,
    VialLoc,
    WellLoc,
    fmt,
    loc_str,
)
from ztra.world.coords import expand_wells
from ztra.world import Severity, World, validate
from ztra.world.coords import WellCoord
from ztra.world.hardware import Pipette
from ztra.world.inventory import Liquid, ThermalState, incompatible, total_ul

MAX_PATHS = 64  # hard cap on branch paths (each branch doubles them) so compile time stays bounded
EPS = 1e-9


@dataclass(frozen=True)
class CostModel:
    """Rough time constants. Replace with measured values."""

    seconds_per_aspiration_cycle: float = 12.0
    seconds_per_tip_change: float = 4.0
    seconds_per_mix_repetition: float = 2.0


@dataclass
class Cost:
    thaws: int = 0
    transfers: int = 0
    aspirations: int = 0  # aspirate/dispense cycles; more than transfers when a volume had to be split
    mixes: int = 0
    delays: int = 0
    tips_used: int = 0
    observations: int = 0
    reagent_consumed_ul: dict[str, float] = field(default_factory=dict)  # stock drawn from vials, per reagent
    estimated_time_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "thaws": self.thaws,
            "transfers": self.transfers,
            "aspirations": self.aspirations,
            "mixes": self.mixes,
            "tips_used": self.tips_used,
            "observations": self.observations,
            "reagent_consumed_ul": dict(sorted(self.reagent_consumed_ul.items())),
            "estimated_time_s": self.estimated_time_s,
        }


@dataclass(frozen=True)
class PathCondition:
    observation: str
    condition: Condition
    holds: bool

    def describe(self) -> str:
        return f"{self.observation}: {self.condition} => {'true' if self.holds else 'false'}"

    def to_dict(self) -> dict[str, Any]:
        return {"observation": self.observation, "condition": self.condition.model_dump(mode="json"), "holds": self.holds}


@dataclass
class PathOutcome:
    conditions: list[PathCondition]
    world: World
    cost: Cost
    trace: list[str]

    @property
    def world_hash(self) -> str:
        return self.world.hash()

    def to_dict(self, include_world: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "conditions": [c.to_dict() for c in self.conditions],
            "world_hash": self.world_hash,
            "cost": self.cost.to_dict(),
            "trace": self.trace,
        }
        if include_world:
            d["world"] = self.world.to_dict()
        return d


@dataclass
class CompileOutput:
    pir: list[PirH]
    outcomes: list[PathOutcome]

    def to_dict(self, include_worlds: bool = True) -> dict[str, Any]:
        return {
            "pir": [op.model_dump(mode="json", exclude_none=True) for op in self.pir],
            "outcomes": [o.to_dict(include_worlds) for o in self.outcomes],
        }


def compile(world: World, protocol: Protocol, cost_model: CostModel | None = None, budget: Budget | None = None) -> CompileOutput:
    """Check a protocol against a world. With a budget, observe steps are added first (FR-2.7)."""
    cost_model = cost_model or CostModel()
    if protocol.version != PROTOCOL_VERSION:
        raise CompileError("E_PROTOCOL_VERSION", "protocol schema version", "protocol", f"version {PROTOCOL_VERSION}", f"version {protocol.version}", "this build reads protocol version 1")
    errors = [i for i in validate(world) if i.severity is Severity.error]
    if errors:
        listing = "; ".join(f"{i.code} at {i.file}:{i.path}" for i in errors)
        raise CompileError("E_WORLD_INVALID", "the world model must validate before a protocol can be compiled against it", "world", "0 validation errors", f"{len(errors)} errors: {listing}", "run `ztra world validate` and fix the world first")

    pir = _Unroller(world).unroll(protocol.steps, [], [])
    if budget is not None:
        pir = schedule(world, pir, budget)
    checker = _Checker(world, cost_model)
    checker.run(_PathState(world.clone(), [], [], Cost()), pir, [])
    return CompileOutput(pir, checker.outcomes)


# ---------------------------------------------------------------- pass 1: unroll


class _Unroller:
    def __init__(self, world: World) -> None:
        self.world = world
        self.labels: list[str] = []  # observation labels seen so far on this path
        self.bindings: dict[str, str] = {}  # for_wells variables in scope → well
        self.items: dict[str, dict[str, str | float]] = {}  # for_each variables in scope → the current item

    def scope(self) -> dict[str, str]:
        """Every variable in scope, flattened for an op's origin: w → B2, item.volume_ul → 20."""
        flat = dict(self.bindings)
        for name, item in self.items.items():
            for col, v in item.items():
                flat[f"{name}.{col}"] = fmt(v) if isinstance(v, float) else str(v)
        return flat

    def resolve(self, ref: str, want: str, origin: Origin) -> str | float:
        """What a `$name` (for_wells) or `$name.column` (for_each) stands for right now,
        checked to be the kind of value the field needs."""
        name, _, col = ref[1:].partition(".")
        in_scope = sorted(["$" + b for b in self.bindings] + [f"${n}.{c}" for n, it in self.items.items() for c in it])
        if not col:
            if name not in self.bindings:
                raise CompileError("E_UNBOUND_VARIABLE", "a $variable must come from an enclosing for_wells or for_each", ref, f"one of {in_scope or 'none in scope'}", ref, f"wrap the step in for_wells with as: {name}", origin=origin)
            value: str | float = self.bindings[name]
        else:
            if name not in self.items:
                raise CompileError("E_UNBOUND_VARIABLE", "a $variable must come from an enclosing for_wells or for_each", ref, f"one of {in_scope or 'none in scope'}", ref, f"wrap the step in for_each with as: {name}", origin=origin)
            if col not in self.items[name]:
                raise CompileError("E_UNBOUND_VARIABLE", "a $name.column must name a column every item has", ref, f"one of {sorted(f'${name}.{c}' for c in self.items[name])}", ref, "add the column to every item, or fix the name", origin=origin)
            value = self.items[name][col]
        if want == "volume" and not isinstance(value, float):
            raise CompileError("E_VARIABLE_TYPE", "a variable used as a volume must hold a number", ref, "a number", repr(value), "give the column numeric values", origin=origin)
        if want != "volume" and not isinstance(value, str):
            raise CompileError("E_VARIABLE_TYPE", f"a variable used as a {want} must hold a name", ref, "a name", repr(value), f"give the column {want} names", origin=origin)
        return value

    def bind(self, loc: Loc, origin: Origin) -> Loc:
        """Replace `$...` in a well or vial field with what it stands for."""
        if isinstance(loc, WellLoc) and loc.well.startswith("$"):
            return WellLoc(plate=loc.plate, well=str(self.resolve(loc.well, "well", origin)))
        if isinstance(loc, VialLoc) and loc.vial.startswith("$"):
            return VialLoc(vial=str(self.resolve(loc.vial, "vial", origin)))
        return loc

    def volume(self, v: float | str, origin: Origin) -> float:
        if isinstance(v, str):
            if not v.startswith("$"):
                raise CompileError("E_VARIABLE_TYPE", "a volume is a number or a $variable", "volume_ul", "a number or $item.column", repr(v), "write the volume as a number", origin=origin)
            return float(self.resolve(v, "volume", origin))
        return v

    def unroll(self, steps: list[Step], path: list[int], iters: list[int]) -> list[PirH]:
        out: list[PirH] = []
        for i, step in enumerate(steps):
            origin = Origin(step_path=[*path, i], iterations=list(iters), bindings=self.scope())
            if isinstance(step, Thaw):
                port = Port(loc=VialLoc(vial=step.vial), volume_ul=0.0)
                out.append(Transform(kind=TransformKind.thaw, inputs=[port], outputs=[port], origin=origin))
            elif isinstance(step, Transfer):
                src, dst = self.bind(step.from_, origin), self.bind(step.to, origin)
                vol = self.volume(step.volume_ul, origin)
                out.append(Transform(kind=TransformKind.transfer, inputs=[Port(loc=src, volume_ul=vol)], outputs=[Port(loc=dst, volume_ul=vol)], origin=origin))
            elif isinstance(step, Mix):
                port = Port(loc=self.bind(step.at, origin), volume_ul=self.volume(step.volume_ul, origin))
                out.append(Transform(kind=TransformKind.mix, inputs=[port], outputs=[port], repetitions=step.repetitions, origin=origin))
            elif isinstance(step, Delay):
                seconds = step.seconds + 60.0 * step.minutes
                if not seconds > 0:
                    raise CompileError("E_DELAY", "a delay must last a positive time", "delay", "> 0 s", f"{fmt(seconds)} s", "give seconds and/or minutes", origin=origin)
                out.append(Transform(kind=TransformKind.delay, inputs=[], outputs=[], seconds=seconds, origin=origin))
            elif isinstance(step, Repeat):
                if step.times == 0:
                    raise CompileError("E_LOOP_BOUND", "a repeat must run at least once", "repeat", ">= 1", "0", "remove the loop or set times >= 1", origin=origin)
                for k in range(1, step.times + 1):
                    out.extend(self.unroll(step.body, [*path, i], [*iters, k]))
            elif isinstance(step, ForWells):
                wells = expand_wells(step.wells)
                if wells is None:
                    raise CompileError("E_WELL_RANGE", "for_wells takes well names or same-row / same-column ranges", "for_wells", "names like A2, or ranges like A2..E2 / A2..A5", str(step.wells), "fix the list; a range must stay in one row or one column", origin=origin)
                if not wells:
                    raise CompileError("E_LOOP_BOUND", "a for_wells must name at least one well", "for_wells", ">= 1 well", "0", "list the wells", origin=origin)
                if step.as_ in self.bindings or step.as_ in self.items:
                    raise CompileError("E_VARIABLE_SHADOWED", "a nested loop must use a different variable name", f"${step.as_}", "an unused name", f"${step.as_} already bound", "change `as:` on the inner loop", origin=origin)
                for k, well in enumerate(wells, start=1):
                    self.bindings[step.as_] = well
                    out.extend(self.unroll(step.body, [*path, i], [*iters, k]))
                del self.bindings[step.as_]
            elif isinstance(step, ForEach):
                if not step.items:
                    raise CompileError("E_LOOP_BOUND", "a for_each must have at least one item", "for_each", ">= 1 item", "0", "list the items", origin=origin)
                if step.as_ in self.bindings or step.as_ in self.items:
                    raise CompileError("E_VARIABLE_SHADOWED", "a nested loop must use a different variable name", f"${step.as_}", "an unused name", f"${step.as_} already bound", "change `as:` on the inner loop", origin=origin)
                for k, item in enumerate(step.items, start=1):
                    self.items[step.as_] = item
                    out.extend(self.unroll(step.body, [*path, i], [*iters, k]))
                del self.items[step.as_]
            elif isinstance(step, Observe):
                sensor = self.world.hardware.sensors.get(step.sensor)
                if sensor is None:
                    known = sorted(self.world.hardware.sensors)
                    raise CompileError("E_UNKNOWN_SENSOR", "an observe must name a sensor from Hardware.sensors", step.sensor, f"one of {known}", step.sensor, "declare the sensor in Hardware.yaml", origin=origin)
                self.labels.append(step.label)
                out.append(ObserveOp(sensor=step.sensor, entity=sensor.observes.entity, label=step.label, origin=origin))
            elif isinstance(step, IfObserved):
                if step.observation not in self.labels:
                    raise CompileError("E_UNKNOWN_OBSERVATION", "a branch may only test an observation taken earlier on the same path", step.observation, f"one of {self.labels}", step.observation, "add an observe step with this label before the branch", origin=origin)
                mark = len(self.labels)
                then_ops = self.unroll(step.then, [*path, i], iters)
                del self.labels[mark:]
                else_ops = self.unroll(step.otherwise, [*path, i], iters)
                del self.labels[mark:]  # a label taken inside one arm isn't guaranteed after the branch
                out.append(Branch(observation=step.observation, condition=step.condition, then=then_ops, otherwise=else_ops, origin=origin))
        return out


# ---------------------------------------------------------------- pass 2: path-sensitive check


@dataclass
class _PathState:
    world: World
    conditions: list[PathCondition]
    trace: list[str]
    cost: Cost

    def fork(self) -> _PathState:
        return _PathState(self.world.clone(), list(self.conditions), list(self.trace), Cost(**{**self.cost.__dict__, "reagent_consumed_ul": dict(self.cost.reagent_consumed_ul)}))


@dataclass
class _Taken:
    """What a transfer picks up: one reagent from a vial, or a well's whole mixture."""

    liquids: list[Liquid]
    from_stock: str | None


class _Checker:
    def __init__(self, base: World, cost_model: CostModel) -> None:
        self.base = base
        self.cost_model = cost_model
        self.outcomes: list[PathOutcome] = []

    def run(self, st: _PathState, ops: list[PirH], conts: list[list[PirH]]) -> None:
        for i, op in enumerate(ops):
            if isinstance(op, Branch):
                inner = [*conts, ops[i + 1 :]]
                then_state = st.fork()
                then_state.conditions.append(PathCondition(op.observation, op.condition, True))
                then_state.trace.append(f"branch: assume {op.observation}: {op.condition} holds")
                self.run(then_state, op.then, inner)
                st.conditions.append(PathCondition(op.observation, op.condition, False))
                st.trace.append(f"branch: assume {op.observation}: {op.condition} does not hold")
                self.run(st, op.otherwise, inner)
                return
            self.apply(st, op)
        if conts:
            rest = conts[-1]
            self.run(st, rest, conts[:-1])
            return
        if len(self.outcomes) >= MAX_PATHS:
            raise CompileError("E_TOO_MANY_PATHS", "the number of branch paths must stay bounded", "protocol", f"<= {MAX_PATHS} paths", f"> {MAX_PATHS}", "reduce nested or sequential if_observed steps", branch_path=[c.describe() for c in st.conditions], chain_of_thought=st.trace)
        self.outcomes.append(PathOutcome(st.conditions, st.world, st.cost, st.trace))

    def err(self, st: _PathState, code: str, origin: Origin, law: str, resource: str, expected: str, actual: str, hint: str, coordinate: str | None = None) -> CompileError:
        return CompileError(code, law, resource, expected, actual, hint, origin=origin, branch_path=[c.describe() for c in st.conditions], coordinate=coordinate, chain_of_thought=list(st.trace))

    def apply(self, st: _PathState, op: PirH) -> None:
        if isinstance(op, ObserveOp):
            sensor = self.base.hardware.sensors[op.sensor]
            st.cost.observations += 1
            st.cost.estimated_time_s += sensor.read_time_s
            st.trace.append(f"observe {op.label}: {op.sensor} ({sensor.kind.value}) on {op.entity}")
            return
        assert isinstance(op, Transform)
        origin = op.origin
        if op.kind is TransformKind.thaw:
            loc = op.inputs[0].loc
            assert isinstance(loc, VialLoc)
            vial = st.world.inventory.vials.get(loc.vial)
            if vial is None:
                raise self.err(st, "E_UNKNOWN_ENTITY", origin, "every entity must exist in the world model", loc.vial, "a vial in Inventory.vials", "not found", "check the vial id")
            was = vial.state.value
            vial.state = ThermalState.thawed
            vial.freeze_thaw_cycles += 1
            st.cost.thaws += 1
            st.trace.append(f"thaw {loc.vial}: {was} -> thawed, freeze_thaw_cycles={vial.freeze_thaw_cycles}")
        elif op.kind is TransformKind.transfer:
            src, dst, vol = op.inputs[0].loc, op.outputs[0].loc, op.inputs[0].volume_ul
            pipette, cycles = self.choose_pipette(st, origin, vol, allow_split=True)
            taken = self.take(st, origin, src, vol)
            self.check_destination(st, origin, dst, taken.liquids, vol)
            tip = self.allocate_tip(st, origin, pipette)
            self.remove(st, src, vol)
            self.deposit(st, dst, taken.liquids)
            if taken.from_stock is not None:
                st.cost.reagent_consumed_ul[taken.from_stock] = st.cost.reagent_consumed_ul.get(taken.from_stock, 0.0) + vol
            st.cost.transfers += 1
            st.cost.aspirations += cycles
            st.cost.tips_used += 1
            st.cost.estimated_time_s += cycles * self.cost_model.seconds_per_aspiration_cycle + self.cost_model.seconds_per_tip_change
            plural = "s" if cycles > 1 else ""
            st.trace.append(f"transfer {fmt(vol)} uL {loc_str(src)} -> {loc_str(dst)} with {pipette.name} ({cycles} cycle{plural}), tip {tip}")
        elif op.kind is TransformKind.delay:
            seconds = op.seconds or 0.0
            st.cost.delays += 1
            st.cost.estimated_time_s += seconds
            st.trace.append(f"wait {fmt(seconds)} s")
        else:
            at, vol = op.inputs[0].loc, op.inputs[0].volume_ul
            pipette, _ = self.choose_pipette(st, origin, vol, allow_split=False)
            self.take(st, origin, at, vol)  # checks presence and volume; nothing moves
            tip = self.allocate_tip(st, origin, pipette)
            reps = op.repetitions or 1
            st.cost.mixes += 1
            st.cost.tips_used += 1
            st.cost.estimated_time_s += reps * self.cost_model.seconds_per_mix_repetition + self.cost_model.seconds_per_tip_change
            st.trace.append(f"mix {fmt(vol)} uL x{reps} at {loc_str(at)} with {pipette.name}, tip {tip}")

    def choose_pipette(self, st: _PathState, origin: Origin, vol: float, allow_split: bool) -> tuple[Pipette, int]:
        if not (vol > 0) or vol == float("inf"):
            raise self.err(st, "E_PIPETTE_RANGE", origin, "a volume must be positive", "volume", "> 0 uL", f"{vol} uL", "fix the volume")
        found = self.base.hardware.pipette_for(vol, allow_split)
        if found is not None:
            return found[0], found[1]
        ranges = self.base.hardware.pipette_ranges() or "at least one pipette"
        hint = "use a volume >= the smallest pipette minimum" if allow_split else "mix volumes cannot be split; use a volume within one pipette's range"
        raise self.err(st, "E_PIPETTE_RANGE", origin, "a volume must lie within some pipette's range", "pipettes", ranges, f"{fmt(vol)} uL", hint)

    def take(self, st: _PathState, origin: Origin, loc: Loc, vol: float) -> _Taken:
        """Check that this much can be drawn from here, and say what would come out."""
        if isinstance(loc, VialLoc):
            vial = st.world.inventory.vials.get(loc.vial)
            if vial is None:
                raise self.err(st, "E_UNKNOWN_ENTITY", origin, "every entity must exist in the world model", loc.vial, "a vial in Inventory.vials", "not found", "check the vial id")
            if vial.consumed:
                raise self.err(st, "E_CONSUMED", origin, "a consumed (linear) resource cannot be used again", loc.vial, "an unconsumed vial", "consumed", "this vial was fully used earlier in this lineage; allocate a new one")
            if vial.state is not ThermalState.thawed:
                raise self.err(st, "E_STATE", origin, "aspiration requires a thawed reagent", loc.vial, "thawed", vial.state.value, "insert a thaw step before this transfer")
            if vial.volume_ul + EPS < vol:
                raise self.err(st, "E_VOLUME", origin, "cannot aspirate more than is present", loc.vial, f">= {fmt(vol)} uL", f"{fmt(vial.volume_ul)} uL", "reduce the volume or add another source vial")
            return _Taken([Liquid(reagent=vial.reagent, volume_ul=vol)], vial.reagent)
        _, contents = self.well(st, origin, loc.plate, loc.well)
        if st.world.inventory.plates[loc.plate].waste:
            raise self.err(st, "E_WASTE_SOURCE", origin, "liquid in the waste is gone; nothing can be drawn from it", loc.plate, "a plate, reservoir or vial", "a waste reservoir", "aspirate from somewhere else", coordinate=loc.well)
        total = total_ul(contents)
        if total + EPS < vol:
            raise self.err(st, "E_VOLUME", origin, "cannot aspirate more than is present", f"{loc.plate}:{loc.well}", f">= {fmt(vol)} uL", f"{fmt(total)} uL", "reduce the volume", coordinate=loc.well)
        return _Taken([Liquid(reagent=l.reagent, volume_ul=vol * l.volume_ul / total) for l in contents], None)

    def well(self, st: _PathState, origin: Origin, plate: str, well: str) -> tuple[float, list[Liquid]]:
        """Look up a plate well: does it exist, what's in it, how much fits."""
        p = st.world.inventory.plates.get(plate)
        if p is None:
            raise self.err(st, "E_UNKNOWN_ENTITY", origin, "every entity must exist in the world model", plate, "a plate in Inventory.plates", "not found", "check the plate id")
        definition = self.base.hardware.labware[p.labware]
        c = WellCoord.parse(well)
        if c is None or not c.within(definition.rows, definition.cols):
            last = WellCoord(definition.rows - 1, definition.cols - 1).name
            raise self.err(st, "E_COORDINATE", origin, "a well must exist on the plate's labware", f"{plate}:{well}", f"A1..{last}", well, "use a valid well name", coordinate=well)
        cap = definition.well_max_ul if definition.well_max_ul is not None else float("inf")
        return cap, list(p.wells.get(well, []))

    def check_destination(self, st: _PathState, origin: Origin, to: Loc, incoming: list[Liquid], vol: float) -> None:
        reagents = self.base.inventory.reagents
        if isinstance(to, WellLoc):
            cap, contents = self.well(st, origin, to.plate, to.well)
            total = total_ul(contents)
            if total + vol > cap + EPS:
                raise self.err(st, "E_OVERFLOW", origin, "a well cannot hold more than its labware allows", f"{to.plate}:{to.well}", f"<= {fmt(cap)} uL", f"{fmt(total + vol)} uL", "use a deeper plate or reduce the volume", coordinate=to.well)
            for inc in incoming:
                for present in contents:
                    a, b = reagents[inc.reagent].hazard, reagents[present.reagent].hazard
                    if incompatible(a, b):
                        raise self.err(st, "E_HAZARD", origin, "MSDS classes that react must not share a vessel", f"{to.plate}:{to.well}", "compatible hazard classes", f"{inc.reagent} ({a.value}) into a well containing {present.reagent} ({b.value})", "neutralise in a separate vessel or change the destination", coordinate=to.well)
            return
        vial = st.world.inventory.vials.get(to.vial)
        if vial is None:
            raise self.err(st, "E_UNKNOWN_ENTITY", origin, "every entity must exist in the world model", to.vial, "a vial in Inventory.vials", "not found", "check the vial id")
        if vial.consumed:
            raise self.err(st, "E_CONSUMED", origin, "a consumed (linear) resource is retired and cannot receive liquid", to.vial, "an unconsumed vial", "consumed", "pool into a fresh vial")
        if any(l.reagent != vial.reagent for l in incoming):
            raise self.err(st, "E_MIXTURE_IN_VIAL", origin, "a vial holds a single reagent in v0.1", to.vial, vial.reagent, "+".join(l.reagent for l in incoming), "pool mixtures in a plate well instead")
        link = self.base.deck.linker.get(to.vial)
        if link is not None:
            rack = self.base.deck.tube_racks.get(link.rack)
            definition = self.base.hardware.labware.get(rack.labware) if rack else None
            if definition is not None and definition.well_max_ul is not None and vial.volume_ul + vol > definition.well_max_ul + EPS:
                raise self.err(st, "E_OVERFLOW", origin, "a tube cannot hold more than its rack labware allows", to.vial, f"<= {fmt(definition.well_max_ul)} uL", f"{fmt(vial.volume_ul + vol)} uL", "use a larger tube")

    def allocate_tip(self, st: _PathState, origin: Origin, pipette: Pipette) -> str:
        tip = st.world.deck.take_tip(self.base.hardware, pipette)
        if tip is not None:
            return f"{tip[0]}:{tip[1]}"
        n = st.world.deck.compatible_racks(pipette)
        actual = "no compatible tip rack on the deck" if n == 0 else f"{n} compatible rack(s), all exhausted"
        raise self.err(st, "E_TIPS", origin, "every transfer and mix uses one tip, and tips run out", pipette.name, "a free tip in a placed rack compatible with the pipette", actual, "add or replace a tip rack in Deck.yaml")

    def remove(self, st: _PathState, src: Loc, vol: float) -> None:
        if remove_liquid(st.world, src, vol) and isinstance(src, VialLoc):
            st.trace.append(f"{src.vial} is now consumed (linear resource retired)")

    def deposit(self, st: _PathState, dst: Loc, liquids: list[Liquid]) -> None:
        deposit_liquid(st.world, dst, liquids)


# ---------------------------------------------------------------- liquid transitions (shared with the simulator)


def liquids_at(world: World, loc: Loc) -> list[Liquid]:
    """What is at a location right now, as a list of (reagent, volume)."""
    if isinstance(loc, VialLoc):
        v = world.inventory.vials[loc.vial]
        return [Liquid(reagent=v.reagent, volume_ul=v.volume_ul)] if v.volume_ul > 0 else []
    return list(world.inventory.plates[loc.plate].wells.get(loc.well, []))


def take_liquids(world: World, loc: Loc, vol: float) -> list[Liquid]:
    """The mixture `vol` µL drawn from a location would contain. No checks."""
    contents = liquids_at(world, loc)
    total = total_ul(contents)
    if total <= 0:
        return []
    return [Liquid(reagent=l.reagent, volume_ul=vol * l.volume_ul / total) for l in contents]


def remove_liquid(world: World, src: Loc, vol: float) -> bool:
    """Take `vol` out of a location. Returns True if a vial just became consumed."""
    if isinstance(src, VialLoc):
        vial = world.inventory.vials[src.vial]
        vial.volume_ul = max(0.0, vial.volume_ul - vol)
        if vial.volume_ul <= EPS and not vial.consumed:
            vial.volume_ul = 0.0
            vial.consumed = True
            return True
        return False
    contents = world.inventory.plates[src.plate].wells.get(src.well, [])
    total = total_ul(contents)
    if total > 0:
        for l in contents:
            l.volume_ul -= vol * l.volume_ul / total
    contents[:] = [l for l in contents if l.volume_ul > EPS]
    return False


def deposit_liquid(world: World, dst: Loc, liquids: list[Liquid]) -> None:
    if isinstance(dst, VialLoc):
        world.inventory.vials[dst.vial].volume_ul += total_ul(liquids)
        return
    contents = world.inventory.plates[dst.plate].wells.setdefault(dst.well, [])
    for l in liquids:
        for existing in contents:
            if existing.reagent == l.reagent:
                existing.volume_ul += l.volume_ul
                break
        else:
            contents.append(Liquid(reagent=l.reagent, volume_ul=l.volume_ul))
    contents.sort(key=lambda x: x.reagent)


def paths(pir: list[PirH]) -> list[tuple[list[PathCondition], list[Transform | ObserveOp]]]:
    """Every straight-line path through PIR-H with the branch decisions that select it.
    The path a compile outcome corresponds to has the same index."""
    out: list[tuple[list[PathCondition], list[Transform | ObserveOp]]] = []

    def walk(prefix: list[PathCondition], done: list[Transform | ObserveOp], ops: list[PirH], conts: list[list[PirH]]) -> None:
        for i, op in enumerate(ops):
            if isinstance(op, Branch):
                inner = [*conts, ops[i + 1 :]]
                walk([*prefix, PathCondition(op.observation, op.condition, True)], list(done), op.then, inner)
                walk([*prefix, PathCondition(op.observation, op.condition, False)], list(done), op.otherwise, inner)
                return
            done.append(op)
        if conts:
            walk(prefix, done, conts[-1], conts[:-1])
        else:
            out.append((prefix, done))

    walk([], [], pir, [])
    return out
