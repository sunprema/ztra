"""ztra as an MCP server, so an agent can validate worlds, write and check protocols,
simulate them, record intents and executions, and read the reference docs.

Every tool returns a plain JSON object. Refusals come back as {"ok": false, "error": {...}}
with the same codes the CLI uses (E_* compile, W_* world, S_* store, D_* diff), never as
exceptions, so the agent can read them and try again.

Run with `ztra-mcp` (stdio). Claude Code picks it up from the repo's .mcp.json.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ztra.backend import opentrons
from ztra.compiler import CompileOutput, compile
from ztra.compiler_errors import CompileError
from ztra.diff import DiffError, diff
from ztra.lower import lower
from ztra.preflight import attach, preflight
from ztra.protocol import Protocol
from ztra.schedule import Budget
from ztra.sensors import Telemetry
from ztra.simulate import Noise, simulate
from ztra.driver import DriverFault
from ztra.drivers.fake import FakeDriver
from ztra.drivers.otsim import OpentronsSimDriver
from ztra.runtime import Runtime
from ztra.store import EXPECTED_SEEDS, IntentCommit, Store, StoreError, write_world
from ztra.telemetry import SensorAdapter, SimulatedSensor, TelemetryService
from ztra.world import LoadError, Severity, World, validate
from ztra.world.summary import summary

DOCS = Path(__file__).resolve().parents[2] / "docs"
TOPICS = {
    "protocol": "PROTOCOL.md",
    "world": "WORLD_MODEL.md",
    "lowering": "LOWERING.md",
    "store": "STORE.md",
    "simulation": "SIMULATION.md",
    "opentrons": "OPENTRONS_NOTES.md",
    "architecture": "ARCHITECTURE.md",
}

server = MCPServer(
    "ztra",
    instructions=(
        "ztra lets you run liquid-handling experiments safely. Typical loop: world_summary → write a protocol "
        "(see reference('protocol')) → preflight_protocol to size it to the stock → compile_protocol until it passes → simulate_protocol → store_commit on a "
        "branch → a person runs the vendor files → store_execute with the telemetry → read the diff. "
        "Every tool returns JSON; when ok is false, read error.code, error.hint and try again."
    ),
)


# ---------------------------------------------------------------- helpers


class _Refusal(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message") or error.get("code"))
        self.error = error


def _world(world_dir: str) -> World:
    try:
        return World.load(Path(world_dir))
    except LoadError as e:
        raise _Refusal({"code": "LOAD_ERROR", "message": str(e), "hint": "check the path and the YAML"}) from e


def _protocol(protocol_yaml: str | None, protocol_path: str | None) -> Protocol:
    try:
        if protocol_path:
            return Protocol.load(Path(protocol_path))
        if protocol_yaml:
            return Protocol.from_yaml(protocol_yaml)
    except ValueError as e:
        raise _Refusal({"code": "LOAD_ERROR", "message": str(e), "hint": "see reference('protocol') for the shape"}) from e
    raise _Refusal({"code": "LOAD_ERROR", "message": "give protocol_yaml or protocol_path", "hint": ""})


def _telemetry(telemetry_yaml: str | None, telemetry_path: str | None) -> Telemetry | None:
    try:
        if telemetry_path:
            return Telemetry.load(Path(telemetry_path))
        if telemetry_yaml:
            import yaml

            return Telemetry.model_validate(yaml.safe_load(telemetry_yaml))
    except (ValueError, Exception) as e:  # pydantic errors included
        raise _Refusal({"code": "LOAD_ERROR", "message": f"telemetry: {e}", "hint": "readings: [{label, sensor, values: {metric: number}}]"}) from e
    return None


def _budget(spec: str | None) -> Budget | None:
    if not spec:
        return None
    try:
        return Budget.parse(spec)
    except ValueError as e:
        raise _Refusal({"code": "LOAD_ERROR", "message": f"budget: {e}", "hint": 'like "sensor=scale_1,every=3,end=true"'}) from e


def _store(repo: str) -> Store:
    try:
        return Store.open(Path(repo))
    except StoreError as e:
        raise _Refusal(e.to_dict()) from e


def _run(fn: Any) -> dict[str, Any]:
    """Turn any refusal into {"ok": false, "error": {...}}."""
    try:
        result: dict[str, Any] = fn()
        return {"ok": True, **result}
    except _Refusal as e:
        return {"ok": False, "error": e.error}
    except CompileError as e:
        return {"ok": False, "error": e.to_dict()}
    except (StoreError, DiffError) as e:
        return {"ok": False, "error": e.to_dict()}
    except DriverFault as e:
        return {"ok": False, "error": e.to_dict()}


def _compile_view(result: CompileOutput) -> dict[str, Any]:
    return {
        "pir_ops": len(result.pir),
        "outcomes": [o.to_dict(include_world=False) for o in result.outcomes],
    }


# ---------------------------------------------------------------- reference


@server.tool()
def reference(topic: str = "protocol") -> str:
    """Read ztra's reference docs. Topics: protocol (how to write one, every error code), world, lowering, store, simulation, opentrons, architecture."""
    name = TOPICS.get(topic)
    if name is None:
        return f"unknown topic '{topic}'; one of {sorted(TOPICS)}"
    path = DOCS / name
    return path.read_text() if path.exists() else f"{name} is not available in this install"


for _topic, _file in TOPICS.items():

    def _make(file: str) -> Any:
        def _read() -> str:
            path = DOCS / file
            return path.read_text() if path.exists() else f"{file} is not available in this install"

        return _read

    server.resource(f"ztra://docs/{_topic}", name=f"ztra {_topic} reference", mime_type="text/markdown")(_make(_file))


# ---------------------------------------------------------------- world


@server.tool()
def world_summary(world_dir: str) -> dict[str, Any]:
    """What's in the lab: vials and their volumes, filled wells with their mixtures and concentrations, free tips, sensors, hazards, and any validation problems."""
    return _run(lambda: summary(_world(world_dir)))


@server.tool()
def world_validate(world_dir: str) -> dict[str, Any]:
    """Check a world directory (Inventory.yaml, Deck.yaml, Hardware.yaml). Errors make it unusable; warnings are advice."""

    def go() -> dict[str, Any]:
        issues = validate(_world(world_dir))
        errors = sum(1 for i in issues if i.severity is Severity.error)
        return {"valid": errors == 0, "errors": errors, "warnings": len(issues) - errors, "issues": [i.to_dict() for i in issues]}

    return _run(go)


# ---------------------------------------------------------------- protocols


@server.tool()
def compile_protocol(world_dir: str, protocol_yaml: str | None = None, protocol_path: str | None = None, budget: str | None = None) -> dict[str, Any]:
    """Check a protocol against a world. Returns one predicted outcome per branch path (conditions, world hash, cost, trace), or a structured error saying which step cannot happen and why — resource errors also carry a `preflight` summary of the whole shortfall. budget like "sensor=scale_1,every=3" adds observe steps."""

    def go() -> dict[str, Any]:
        world = _world(world_dir)
        protocol = _protocol(protocol_yaml, protocol_path)
        b = _budget(budget)
        try:
            return _compile_view(compile(world, protocol, budget=b))
        except CompileError as e:
            raise _Refusal(attach(e.to_dict(), world, protocol, b)) from e

    return _run(go)


@server.tool()
def preflight_protocol(world_dir: str, protocol_yaml: str | None = None, protocol_path: str | None = None, budget: str | None = None) -> dict[str, Any]:
    """Before compiling: everything the protocol needs versus what the lab has — stock per vial and reagent, tips per pipette, wells that would overflow, frozen vials used without a thaw — worst case across branch paths. Use it to size a protocol to the stock in one pass."""
    return _run(lambda: preflight(_world(world_dir), _protocol(protocol_yaml, protocol_path), _budget(budget)).model_dump(mode="json"))


@server.tool()
def simulate_protocol(
    world_dir: str,
    protocol_yaml: str | None = None,
    protocol_path: str | None = None,
    budget: str | None = None,
    seeds: int = 30,
    pipette_accuracy: bool = True,
    dispense_drift: float = 0.0,
    jitter_ul: float = 0.0,
    failure_rate: float = 0.0,
) -> dict[str, Any]:
    """Predict what every sensor should read at each observe, and how much that varies on a healthy robot (each pipette's accuracy spec, seeded runs). Add dispense_drift / jitter_ul / failure_rate to stress-test. Also counts failed transfers, shortfalls and overflows."""

    def go() -> dict[str, Any]:
        world = _world(world_dir)
        result = compile(world, _protocol(protocol_yaml, protocol_path), budget=_budget(budget))
        sim = simulate(world, result.pir, Noise(pipette_accuracy=pipette_accuracy, dispense_drift=dispense_drift, jitter_ul=jitter_ul, failure_rate=failure_rate), seeds=seeds)
        return sim.model_dump(mode="json")

    return _run(go)


@server.tool()
def lower_protocol(world_dir: str, protocol_yaml: str | None = None, protocol_path: str | None = None, budget: str | None = None) -> dict[str, Any]:
    """Turn a protocol into robot steps (PIR-L segments with real deck addresses and tips) and the vendor Python files a person can run."""

    def go() -> dict[str, Any]:
        world = _world(world_dir)
        program = lower(world, compile(world, _protocol(protocol_yaml, protocol_path), budget=_budget(budget)).pir)
        return {"program": program.to_dict(), "files": dict(opentrons.emit_program(world, program))}

    return _run(go)


@server.tool()
def diff_run(
    world_dir: str,
    protocol_yaml: str | None = None,
    protocol_path: str | None = None,
    telemetry_yaml: str | None = None,
    telemetry_path: str | None = None,
    budget: str | None = None,
    outcome: int | None = None,
    write_observed_world_to: str | None = None,
) -> dict[str, Any]:
    """Compare what the sensors reported with what was expected. Verdicts per reading, a run-level classification (ok / localized / systematic / unobserved), and the estimated world after the run."""

    def go() -> dict[str, Any]:
        world = _world(world_dir)
        result = compile(world, _protocol(protocol_yaml, protocol_path), budget=_budget(budget))
        telemetry = _telemetry(telemetry_yaml, telemetry_path)
        if telemetry is None:
            raise _Refusal({"code": "LOAD_ERROR", "message": "give telemetry_yaml or telemetry_path", "hint": ""})
        report, observed = diff(result, simulate(world, result.pir, Noise.normal(), seeds=EXPECTED_SEEDS), telemetry, outcome)
        if write_observed_world_to:
            write_world(observed, Path(write_observed_world_to))
        return report.model_dump(mode="json", exclude_none=True)

    return _run(go)


# ---------------------------------------------------------------- store


@server.tool()
def store_init(repo: str, world_dir: str) -> dict[str, Any]:
    """Start a history at `repo` (a .ztra directory) with this world as the root of main."""

    def go() -> dict[str, Any]:
        world = _world(world_dir)
        s = Store.init(Path(repo), world)
        return {"repo": str(s.root), "main": s.head("main"), "world": world.hash()}

    return _run(go)


@server.tool()
def store_branch(repo: str, name: str, from_branch: str = "main") -> dict[str, Any]:
    """Create a branch to try a hypothesis without touching main."""
    return _run(lambda: {"branch": name, "head": _store(repo).branch(name, from_branch)})


@server.tool()
def store_log(repo: str, branch: str = "main") -> dict[str, Any]:
    """History of a branch, newest first, plus every branch head."""

    def go() -> dict[str, Any]:
        s = _store(repo)
        commits = []
        for h, c in s.history(branch):
            e: dict[str, Any] = {"hash": h, "kind": c.kind, "created_at": c.created_at, "message": c.message}
            if isinstance(c, IntentCommit):
                e["outcomes"] = len(c.outcomes)
            commits.append(e)
        return {"branch": branch, "head": s.head(branch), "commits": commits, "branches": s.branches()}

    return _run(go)


@server.tool()
def store_show(repo: str, hash: str) -> dict[str, Any]:
    """Everything recorded in one commit."""
    return _run(lambda: {"commit": _store(repo).get_commit(hash).model_dump(mode="json", exclude_none=True)})


@server.tool()
def store_world(repo: str, branch: str = "main") -> dict[str, Any]:
    """The world at a branch head, summarised. Refuses if the head is unresolved (last intent branches on a reading not yet taken)."""
    return _run(lambda: summary(_store(repo).working_world(branch)))


@server.tool()
def store_checkout(repo: str, branch: str, out_dir: str) -> dict[str, Any]:
    """Write the head world of a branch as Inventory/Deck/Hardware.yaml, e.g. to compile against it."""
    return _run(lambda: {"out": out_dir, "world": _store(repo).checkout(branch, Path(out_dir)).hash()})


@server.tool()
def store_commit(repo: str, branch: str, protocol_yaml: str | None = None, protocol_path: str | None = None, message: str | None = None, budget: str | None = None) -> dict[str, Any]:
    """Compile, lower and simulate a protocol against the branch head and record it as an intent. Fails with the compile error if it cannot happen."""

    def go() -> dict[str, Any]:
        s = _store(repo)
        protocol = _protocol(protocol_yaml, protocol_path)
        b = _budget(budget)
        try:
            h = s.commit_intent(branch, protocol, message, budget=b)
        except CompileError as e:
            raise _Refusal(attach(e.to_dict(), s.working_world(branch), protocol, b)) from e
        c = s.get_commit(h)
        assert isinstance(c, IntentCommit)
        return {"hash": h, "branch": branch, "segments": c.segments, "outcomes": [{"conditions": o.conditions, "world": o.world, "expected_readings": o.readings} for o in c.outcomes]}

    return _run(go)


@server.tool()
def store_files(repo: str, hash: str, out_dir: str | None = None) -> dict[str, Any]:
    """The vendor (Opentrons Python) files for an intent, one per segment. With out_dir they are written there."""

    def go() -> dict[str, Any]:
        files = _store(repo).vendor_files(hash)
        if out_dir:
            d = Path(out_dir)
            d.mkdir(parents=True, exist_ok=True)
            for name, src in files:
                (d / name).write_text(src)
            return {"out": out_dir, "files": [n for n, _ in files]}
        return {"files": dict(files)}

    return _run(go)


@server.tool()
def store_execute(
    repo: str,
    branch: str,
    telemetry_yaml: str | None = None,
    telemetry_path: str | None = None,
    observed_world_dir: str | None = None,
    outcome: int | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Record that the branch's head intent was run for real. With telemetry, the diff engine works out which outcome happened and what the world looks like now; main adopts it. Refuses if the branch is not on top of main (rebase first)."""

    def go() -> dict[str, Any]:
        s = _store(repo)
        observed = _world(observed_world_dir) if observed_world_dir else None
        h = s.execute(branch, observed, outcome, _telemetry(telemetry_yaml, telemetry_path), message)
        c = s.get_commit(h)
        report = getattr(c, "report", None)
        return {"observation": h, "main": s.head("main"), "outcome": getattr(c, "chosen_outcome", None), "report": report}

    return _run(go)


@server.tool()
def run_intent(repo: str, branch: str, approve: bool = False, seed: int = 0, faults: list[str] | None = None, message: str | None = None, driver_name: str = "fake") -> dict[str, Any]:
    """Run the branch's head intent on a simulated lab (no hardware). driver_name "fake" (default) is a pretend robot with realistic pipette error; "otsim" runs each segment inside Opentrons' own simulator and aborts if its tracked volumes disagree with ztra's (needs ZTRA_OT_SIM_OT2/FLEX and apiLevel >= 2.22; ignores seed/faults). Nothing runs unless approve=true. faults like ["0:5:clog", "0:9:door_open"] inject a clogged tip or an opened door at (segment, op). Records the observation (completed or aborted) on main and returns the diff summary."""

    def go() -> dict[str, Any]:
        s = _store(repo)
        c = s.get_commit(s.head(branch))
        if not isinstance(c, IntentCommit):
            raise StoreError("S_NOTHING_TO_EXECUTE", f"branch '{branch}' has no unexecuted intent at its head")
        fault_map: dict[tuple[int, int], Any] = {}
        for spec in faults or []:
            seg, op, kind = spec.split(":")
            fault_map[(int(seg), int(op))] = kind
        physical = s.get_world(c.base_world)
        driver: FakeDriver | OpentronsSimDriver
        if driver_name == "otsim":
            driver = OpentronsSimDriver(physical)
        else:
            driver = FakeDriver(physical, seed=seed, faults=fault_map)
        sensors: dict[str, SensorAdapter] = {sid: SimulatedSensor(lambda: driver.physical, seed=seed + 1) for sid in physical.hardware.sensors}
        rt = Runtime(s, driver, TelemetryService(physical.hardware, sensors), approve=lambda _c, _f: approve)
        return rt.run(branch, message).model_dump(mode="json", exclude_none=True)

    return _run(go)


@server.tool()
def store_rebase(repo: str, branch: str) -> dict[str, Any]:
    """Recompile a branch's intents on top of the current main after reality moved on. A protocol that no longer fits fails with its compile error and the branch is left alone."""

    def go() -> dict[str, Any]:
        s = _store(repo)
        return {"branch": branch, "replayed": s.rebase(branch), "head": s.head(branch)}

    return _run(go)


@server.tool()
def store_verify(repo: str) -> dict[str, Any]:
    """Recompute every hash in the history. Any problem means something was edited after the fact."""

    def go() -> dict[str, Any]:
        problems = _store(repo).verify()
        return {"intact": not problems, "problems": problems}

    return _run(go)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
