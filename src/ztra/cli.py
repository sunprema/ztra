"""The `ztra` command. Everything it prints on stdout is JSON.

  ztra init [dir]                        → scaffold a new project: world/*.yaml + protocols/first_protocol.yaml
  ztra world validate <dir>              → {ok, errors, warnings, issues[]}; exit 1 if errors
  ztra world dump <dir>                  → the whole world as JSON, keys sorted
  ztra world hash <dir>                  → {hash}
  ztra world summary <dir>               → a short picture: vials, wells, tips, sensors, problems
  ztra compile <world_dir> <protocol>    → {ok, pir[], outcomes[]} or {ok:false, error}; exit 1 on CompileError
      --no-worlds   omit predicted worlds from outcomes (hashes and costs stay)
  ztra lower <world_dir> <protocol>      → {ok, program, files{segment_N.py: source}}; exit 1 on error
      --out <dir>   write program.json and the segment files there instead
  ztra preflight <world_dir> <protocol>  → what the protocol needs vs what the lab has, all at once
  ztra simulate <world_dir> <protocol>   → expected readings per outcome, with spread over seeded noisy runs
      --seeds N --drift F --jitter UL --failure-rate P --ideal-pipettes
  ztra diff <world_dir> <protocol> <telemetry.yaml> [--outcome N] [--out <dir>]
                                         → the world diff and (with --out) the estimated observed world
  compile / lower / simulate / diff / store commit take --budget "sensor=scale_1,every=3,end=true"
      to add observe steps automatically.

  ztra store init <world_dir>                    create .ztra here with the world as the root of main
  ztra store branch <name> [--from main]
  ztra store commit <branch> <protocol> [-m msg] compile + lower against the branch head, record an intent
  ztra store log [branch]
  ztra store show <hash>
  ztra store checkout <branch> <out_dir>         write the head world as YAML
  ztra store files <intent_hash> [--out <dir>]   the vendor files for an intent
  ztra store execute <branch> [--observed <world_dir>] [--outcome N] [--telemetry <yaml>] [-m msg]
  ztra store rebase <branch>
  ztra store verify
  All store commands take --repo <dir> (default: ./.ztra).
  ztra run <branch> --yes [--seed N] [--accurate] [--fault SEG:OP:clog|door_open ...] [-m msg]
                                         run the branch's head intent on the FAKE driver and record the result
      --driver otsim   run each segment inside the Opentrons simulator instead (needs
                       ZTRA_OT_SIM_OT2/FLEX and apiLevel >= 2.22); aborts if the vendor
                       engine's tracked volumes disagree with ours
Exit 1 = the core refused (compile/store error), exit 2 = an input could not be loaded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ztra.backend import opentrons
from ztra.compiler import compile
from ztra.compiler_errors import CompileError
from ztra.diff import DiffError, diff
from ztra.lower import lower
from ztra.preflight import attach, preflight
from ztra.driver import DriverFault
from ztra.drivers.fake import FakeDriver
from ztra.drivers.otsim import OpentronsSimDriver
from ztra.protocol import Protocol
from ztra.runtime import Runtime
from ztra.scaffold import NEXT_STEPS, ScaffoldError, scaffold
from ztra.schedule import Budget
from ztra.telemetry import SensorAdapter, SimulatedSensor, TelemetryService
from ztra.sensors import Telemetry
from ztra.simulate import Noise, simulate
from ztra.store import EXPECTED_SEEDS, STORE_DIR, IntentCommit, Store, StoreError, write_world
from ztra.world import LoadError, Severity, World, validate
from ztra.world.summary import summary


def out(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def load_world(path: str) -> World:
    try:
        return World.load(Path(path))
    except LoadError as e:
        out({"ok": False, "load_error": str(e)})
        sys.exit(2)


def load_protocol(path: str) -> Protocol:
    try:
        return Protocol.load(Path(path))
    except ValueError as e:
        out({"ok": False, "load_error": str(e)})
        sys.exit(2)


def load_budget(args: argparse.Namespace) -> Budget | None:
    spec = getattr(args, "budget", None)
    if not spec:
        return None
    try:
        return Budget.parse(spec)
    except ValueError as e:
        out({"ok": False, "load_error": f"--budget: {e}"})
        sys.exit(2)


def load_telemetry(path: str) -> Telemetry:
    try:
        return Telemetry.load(Path(path))
    except ValueError as e:
        out({"ok": False, "load_error": str(e)})
        sys.exit(2)


def cmd_init(args: argparse.Namespace) -> int:
    try:
        created = scaffold(Path(args.dir))
    except ScaffoldError as e:
        out({"ok": False, "error": e.to_dict()})
        return 1
    out({"ok": True, "dir": args.dir, "created": created, "next": NEXT_STEPS})
    return 0


def cmd_world(args: argparse.Namespace) -> int:
    world = load_world(args.dir)
    if args.action == "validate":
        issues = validate(world)
        errors = sum(1 for i in issues if i.severity is Severity.error)
        out({"ok": errors == 0, "errors": errors, "warnings": len(issues) - errors, "issues": [i.to_dict() for i in issues]})
        return 0 if errors == 0 else 1
    if args.action == "dump":
        out(world.to_dict())
        return 0
    if args.action == "summary":
        out(summary(world))
        return 0
    out({"hash": world.hash()})
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    world = load_world(args.world_dir)
    protocol = load_protocol(args.protocol)
    budget = load_budget(args)
    try:
        result = compile(world, protocol, budget=budget)
    except CompileError as e:
        out({"ok": False, "error": attach(e.to_dict(), world, protocol, budget)})
        return 1
    out({"ok": True, **result.to_dict(include_worlds=not args.no_worlds)})
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    world = load_world(args.world_dir)
    protocol = load_protocol(args.protocol)
    try:
        report = preflight(world, protocol, load_budget(args))
    except CompileError as e:
        out({"ok": False, "error": e.to_dict()})
        return 1
    out({"ok": True, **report.model_dump(mode="json")})
    return 0 if report.feasible else 1


def cmd_simulate(args: argparse.Namespace) -> int:
    world = load_world(args.world_dir)
    protocol = load_protocol(args.protocol)
    noise = Noise(pipette_accuracy=not args.ideal_pipettes, dispense_drift=args.drift, jitter_ul=args.jitter, failure_rate=args.failure_rate)
    try:
        result = compile(world, protocol, budget=load_budget(args))
    except CompileError as e:
        out({"ok": False, "error": e.to_dict()})
        return 1
    sim = simulate(world, result.pir, noise, seeds=args.seeds, base_seed=args.seed)
    out({"ok": True, **sim.model_dump(mode="json")})
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    world = load_world(args.world_dir)
    protocol = load_protocol(args.protocol)
    telemetry = load_telemetry(args.telemetry)
    try:
        result = compile(world, protocol, budget=load_budget(args))
        sim = simulate(world, result.pir, Noise.normal(), seeds=EXPECTED_SEEDS)
        report, observed = diff(result, sim, telemetry, args.outcome)
    except (CompileError, DiffError) as e:
        out({"ok": False, "error": e.to_dict()})
        return 1
    if args.out:
        write_world(observed, Path(args.out))
    out({"ok": True, **report.model_dump(mode="json", exclude_none=True), "observed_world_written_to": args.out})
    return 0


def cmd_lower(args: argparse.Namespace) -> int:
    world = load_world(args.world_dir)
    protocol = load_protocol(args.protocol)
    try:
        program = lower(world, compile(world, protocol, budget=load_budget(args)).pir)
    except CompileError as e:
        out({"ok": False, "error": e.to_dict()})
        return 1
    files = opentrons.emit_program(world, program)
    if args.out is None:
        out({"ok": True, "program": program.to_dict(), "files": dict(files)})
        return 0
    d = Path(args.out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "program.json").write_text(json.dumps(program.to_dict(), indent=2, sort_keys=True))
    for name, src in files:
        (d / name).write_text(src)
    out({"ok": True, "out": str(d), "segments": len(program.segments)})
    return 0


def open_store(args: argparse.Namespace) -> Store:
    try:
        return Store.open(Path(args.repo))
    except StoreError as e:
        out({"ok": False, "error": e.to_dict()})
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        s = Store.open(Path(args.repo))
        head = s.head(args.branch)
        c = s.get_commit(head)
    except StoreError as e:
        out({"ok": False, "error": e.to_dict()})
        return 1
    if not isinstance(c, IntentCommit):
        out({"ok": False, "error": {"code": "S_NOTHING_TO_EXECUTE", "message": f"branch '{args.branch}' has no unexecuted intent at its head", "hint": ""}})
        return 1
    faults: dict[tuple[int, int], Any] = {}
    for spec in args.fault or []:
        seg, op, kind = spec.split(":")
        faults[(int(seg), int(op))] = kind
    physical = s.get_world(c.base_world)
    driver: FakeDriver | OpentronsSimDriver
    if args.driver == "otsim":
        if faults:
            out({"ok": False, "error": {"code": "D_NO_FAULTS", "message": "the vendor-simulator driver has no fault injection", "hint": "use the fake driver for faults"}})
            return 1
        try:
            driver = OpentronsSimDriver(physical)
        except DriverFault as e:
            out({"ok": False, "error": e.to_dict()})
            return 1
    else:
        driver = FakeDriver(physical, seed=args.seed, accurate=args.accurate, faults=faults)
    sensors: dict[str, SensorAdapter] = {sid: SimulatedSensor(lambda: driver.physical, seed=args.seed + 1) for sid in physical.hardware.sensors}
    rt = Runtime(s, driver, TelemetryService(physical.hardware, sensors), approve=lambda _c, _f: bool(args.yes))
    result = rt.run(args.branch, args.message)
    d = result.model_dump(mode="json", exclude_none=True)
    if not args.verbose:
        d.pop("log", None)
    out({"ok": result.status != "refused", **d})
    return 0 if result.status == "completed" else 1


def cmd_store(args: argparse.Namespace) -> int:
    try:
        return store_action(args)
    except StoreError as e:
        out({"ok": False, "error": e.to_dict()})
        return 1
    except (CompileError, DiffError) as e:
        out({"ok": False, "error": e.to_dict()})
        return 1


def store_action(args: argparse.Namespace) -> int:
    a = args.action
    if a == "init":
        world = load_world(args.world_dir)
        s = Store.init(Path(args.repo), world)
        out({"ok": True, "repo": str(s.root), "main": s.head("main"), "world": world.hash()})
        return 0
    s = open_store(args)
    if a == "branch":
        h = s.branch(args.name, args.from_)
        out({"ok": True, "branch": args.name, "head": h})
    elif a == "commit":
        h = s.commit_intent(args.branch, load_protocol(args.protocol), args.message, budget=load_budget(args))
        c = s.get_commit(h)
        assert isinstance(c, IntentCommit)
        out({"ok": True, "hash": h, "branch": args.branch, "outcomes": [{"conditions": o.conditions, "world": o.world, "readings": len(o.readings)} for o in c.outcomes], "segments": c.segments})
    elif a == "log":
        entries = []
        for h, c in s.history(args.branch):
            e: dict[str, Any] = {"hash": h, "kind": c.kind, "created_at": c.created_at, "branch": c.branch, "message": c.message}
            if isinstance(c, IntentCommit):
                e["outcomes"] = len(c.outcomes)
            entries.append(e)
        out({"ok": True, "branch": args.branch, "head": s.head(args.branch), "commits": entries, "branches": s.branches()})
    elif a == "show":
        out({"ok": True, "hash": args.hash, "commit": s.get_commit(args.hash).model_dump(mode="json", exclude_none=True)})
    elif a == "checkout":
        w = s.checkout(args.branch, Path(args.out_dir))
        out({"ok": True, "branch": args.branch, "out": args.out_dir, "world": w.hash()})
    elif a == "files":
        files = s.vendor_files(args.hash)
        if args.out:
            d = Path(args.out)
            d.mkdir(parents=True, exist_ok=True)
            for name, src in files:
                (d / name).write_text(src)
            out({"ok": True, "out": args.out, "files": [n for n, _ in files]})
        else:
            out({"ok": True, "files": dict(files)})
    elif a == "execute":
        observed = load_world(args.observed) if args.observed else None
        telemetry = load_telemetry(args.telemetry) if args.telemetry else None
        h = s.execute(args.branch, observed, args.outcome, telemetry, args.message)
        oc = s.get_commit(h)
        summary = oc.report and {k: oc.report[k] for k in ("classification", "counts", "can_localize", "unaccounted")}  # type: ignore[union-attr]
        out({"ok": True, "observation": h, "main": s.head("main"), "outcome": oc.chosen_outcome, "report": summary})  # type: ignore[union-attr]
    elif a == "rebase":
        new = s.rebase(args.branch)
        out({"ok": True, "branch": args.branch, "replayed": new, "head": s.head(args.branch)})
    elif a == "verify":
        problems = s.verify()
        out({"ok": not problems, "problems": problems})
        return 0 if not problems else 1
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ztra")
    sub = parser.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init")
    i.add_argument("dir", nargs="?", default=".")
    i.set_defaults(func=cmd_init)

    w = sub.add_parser("world")
    w.add_argument("action", choices=["validate", "dump", "hash", "summary"])
    w.add_argument("dir")
    w.set_defaults(func=cmd_world)

    c = sub.add_parser("compile")
    c.add_argument("world_dir")
    c.add_argument("protocol")
    c.add_argument("--no-worlds", action="store_true")
    c.add_argument("--budget")
    c.set_defaults(func=cmd_compile)

    l = sub.add_parser("lower")
    l.add_argument("world_dir")
    l.add_argument("protocol")
    l.add_argument("--out")
    l.add_argument("--budget")
    l.set_defaults(func=cmd_lower)

    pf = sub.add_parser("preflight")
    pf.add_argument("world_dir")
    pf.add_argument("protocol")
    pf.add_argument("--budget")
    pf.set_defaults(func=cmd_preflight)

    sm = sub.add_parser("simulate")
    sm.add_argument("world_dir")
    sm.add_argument("protocol")
    sm.add_argument("--budget")
    sm.add_argument("--seeds", type=int, default=0)
    sm.add_argument("--seed", type=int, default=0)
    sm.add_argument("--drift", type=float, default=0.0)
    sm.add_argument("--jitter", type=float, default=0.0)
    sm.add_argument("--failure-rate", type=float, default=0.0)
    sm.add_argument("--ideal-pipettes", action="store_true", help="switch off the pipettes' accuracy spec (default: on)")
    sm.set_defaults(func=cmd_simulate)

    df = sub.add_parser("diff")
    df.add_argument("world_dir")
    df.add_argument("protocol")
    df.add_argument("telemetry")
    df.add_argument("--budget")
    df.add_argument("--outcome", type=int)
    df.add_argument("--out")
    df.set_defaults(func=cmd_diff)

    rn = sub.add_parser("run")
    rn.add_argument("branch")
    rn.add_argument("--repo", default=STORE_DIR)
    rn.add_argument("--yes", action="store_true", help="approve dispatch (without it nothing runs)")
    rn.add_argument("--driver", choices=["fake", "otsim"], default="fake", help="otsim runs segments inside the Opentrons simulator (needs ZTRA_OT_SIM_*)")
    rn.add_argument("--seed", type=int, default=0)
    rn.add_argument("--accurate", action="store_true", help="fake lab with perfect pipettes")
    rn.add_argument("--fault", action="append", help="SEG:OP:clog or SEG:OP:door_open")
    rn.add_argument("-m", "--message")
    rn.add_argument("-v", "--verbose", action="store_true", help="include the run log")
    rn.set_defaults(func=cmd_run)

    st = sub.add_parser("store")
    st.add_argument("--repo", default=STORE_DIR)
    sa = st.add_subparsers(dest="action", required=True)
    p = sa.add_parser("init"); p.add_argument("world_dir")
    p = sa.add_parser("branch"); p.add_argument("name"); p.add_argument("--from", dest="from_", default="main")
    p = sa.add_parser("commit"); p.add_argument("branch"); p.add_argument("protocol"); p.add_argument("-m", "--message"); p.add_argument("--budget")
    p = sa.add_parser("log"); p.add_argument("branch", nargs="?", default="main")
    p = sa.add_parser("show"); p.add_argument("hash")
    p = sa.add_parser("checkout"); p.add_argument("branch"); p.add_argument("out_dir")
    p = sa.add_parser("files"); p.add_argument("hash"); p.add_argument("--out")
    p = sa.add_parser("execute"); p.add_argument("branch"); p.add_argument("--observed"); p.add_argument("--outcome", type=int); p.add_argument("--telemetry"); p.add_argument("-m", "--message")
    p = sa.add_parser("rebase"); p.add_argument("branch")
    sa.add_parser("verify")
    st.set_defaults(func=cmd_store)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
