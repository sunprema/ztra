"""The store: an append-only, hash-chained history of what we meant to do and what
actually happened, with branches for hypotheses.

Two kinds of commit besides the root:
- intent      — a protocol compiled against the branch's world, with the predicted outcome(s)
- observation — what the lab looked like after an intent really ran; only ever on `main`

Rules that make it physical rather than git-like:
- Only `main` is real. A branch is a plan.
- Execute is a fast-forward: the branch must sit on top of main's head. Otherwise rebase
  (= recompile every intent against the new reality) first. There is no merge.
- You cannot plan past an unresolved reading: an intent with several outcomes must be
  executed before another intent can go on top of it.

On disk, everything is a JSON file named by its SHA-256:
  .ztra/objects/<hash>.json   commits and world snapshots
  .ztra/refs/<branch>         branch head hash
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import Field

from ztra.backend import opentrons
from ztra.compiler import CompileOutput, compile
from ztra.diff import WorldDiff, diff
from ztra.lower import Program, lower
from ztra.schedule import Budget
from ztra.sensors import Telemetry
from ztra.simulate import Noise, simulate

EXPECTED_SEEDS = 30  # seeded runs behind every expected reading's spread
from ztra.model import Strict
from ztra.protocol import Protocol
from ztra.world import DECK_FILE, HARDWARE_FILE, INVENTORY_FILE, World, canonical, sha256_hex

STORE_DIR = ".ztra"
Clock = Callable[[], str]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StoreError(Exception):
    """Something the store refuses to do, with a code an agent can key on."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


# ---------------------------------------------------------------- commits


class Outcome(Strict):
    """One predicted result of an intent: the branch decisions that lead to it, the world hash,
    and what every sensor should read at each observe along the way."""

    conditions: list[dict[str, Any]]
    world: str
    readings: list[dict[str, Any]] = Field(default_factory=list)


class RootCommit(Strict):
    kind: Literal["root"] = "root"
    parent: None = None
    branch: str = "main"
    created_at: str
    message: str = "root"
    world: str  # hash of the starting world


class IntentCommit(Strict):
    kind: Literal["intent"] = "intent"
    parent: str
    branch: str
    created_at: str
    message: str | None = None
    protocol: dict[str, Any]  # the protocol document, so it can be replayed on rebase
    budget: dict[str, Any] | None = None  # observation budget used, so a rebase schedules the same way
    base_world: str  # what it was compiled against
    outcomes: list[Outcome]
    pir_ops: int
    segments: int
    program: dict[str, Any]  # the lowered PIR-L, exactly what would be sent


class ObservationCommit(Strict):
    kind: Literal["observation"] = "observation"
    parent: str
    branch: Literal["main"] = "main"
    created_at: str
    message: str | None = None
    executed: str  # hash of the intent that ran
    chosen_outcome: int | None = None  # None when the run did not finish
    observed_world: str
    telemetry: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] | None = None  # the world diff, when telemetry was supplied
    status: Literal["completed", "aborted"] = "completed"
    reason: dict[str, Any] | None = None  # why it stopped, for aborted runs


Commit = Annotated[Union[RootCommit, IntentCommit, ObservationCommit], Field(discriminator="kind")]


class _CommitEnvelope(Strict):
    commit: Commit


def commit_hash(commit: RootCommit | IntentCommit | ObservationCommit) -> str:
    return sha256_hex(canonical(commit.model_dump(mode="json", exclude_none=True)))


# ---------------------------------------------------------------- the store


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.objects = root / "objects"
        self.refs = root / "refs"

    # --- lifecycle

    @staticmethod
    def init(root: Path, world: World, clock: Clock = utc_now) -> Store:
        if root.exists():
            raise StoreError("S_EXISTS", f"{root} already exists", "open it instead of initialising")
        s = Store(root)
        s.objects.mkdir(parents=True)
        s.refs.mkdir()
        wh = s.put_world(world)
        h = s.put_commit(RootCommit(created_at=clock(), world=wh))
        s.set_head("main", h)
        return s

    @staticmethod
    def open(root: Path) -> Store:
        if not (root / "refs" / "main").exists():
            raise StoreError("S_NOT_A_STORE", f"{root} is not a ztra store", "run `ztra store init` first")
        return Store(root)

    # --- objects

    def put_world(self, world: World) -> str:
        h = world.hash()
        self._write(h, {"world": world.to_dict()})
        return h

    def get_world(self, h: str) -> World:
        data = self._read(h)
        if "world" not in data:
            raise StoreError("S_NOT_A_WORLD", f"{h} is not a world snapshot")
        return World.model_validate(data["world"])

    def put_commit(self, commit: RootCommit | IntentCommit | ObservationCommit) -> str:
        h = commit_hash(commit)
        self._write(h, {"commit": commit.model_dump(mode="json", exclude_none=True)})
        return h

    def get_commit(self, h: str) -> RootCommit | IntentCommit | ObservationCommit:
        data = self._read(h)
        if "commit" not in data:
            raise StoreError("S_NOT_A_COMMIT", f"{h} is not a commit")
        return _CommitEnvelope.model_validate(data).commit

    def _write(self, h: str, data: dict[str, Any]) -> None:
        path = self.objects / f"{h}.json"
        if not path.exists():  # content-addressed: never overwrite
            path.write_text(canonical(data))

    def _read(self, h: str) -> dict[str, Any]:
        path = self.objects / f"{h}.json"
        if not path.exists():
            raise StoreError("S_MISSING_OBJECT", f"no object {h}")
        data: dict[str, Any] = json.loads(path.read_text())
        return data

    # --- refs

    def branches(self) -> dict[str, str]:
        return {p.name: p.read_text().strip() for p in sorted(self.refs.iterdir())}

    def head(self, branch: str) -> str:
        path = self.refs / branch
        if not path.exists():
            raise StoreError("S_NO_BRANCH", f"no branch '{branch}'", f"one of {sorted(self.branches())}")
        return path.read_text().strip()

    def set_head(self, branch: str, h: str) -> None:
        (self.refs / branch).write_text(h + "\n")

    def branch(self, name: str, from_: str = "main") -> str:
        if (self.refs / name).exists():
            raise StoreError("S_BRANCH_EXISTS", f"branch '{name}' already exists")
        if not name or "/" in name or name.startswith("."):
            raise StoreError("S_BAD_NAME", f"'{name}' is not a usable branch name")
        h = self.head(from_)
        self.set_head(name, h)
        return h

    # --- history

    def history(self, branch: str) -> list[tuple[str, RootCommit | IntentCommit | ObservationCommit]]:
        """Newest first, back to the root."""
        out = []
        h: str | None = self.head(branch)
        while h is not None:
            c = self.get_commit(h)
            out.append((h, c))
            h = c.parent
        return out

    def is_ancestor(self, maybe_ancestor: str, h: str) -> bool:
        cur: str | None = h
        while cur is not None:
            if cur == maybe_ancestor:
                return True
            cur = self.get_commit(cur).parent
        return False

    def is_fast_forward_of_main(self, branch: str) -> bool:
        return self.is_ancestor(self.head("main"), self.head(branch))

    # --- worlds at a head

    def head_worlds(self, branch: str) -> list[tuple[list[dict[str, Any]], str]]:
        """Candidate (conditions, world hash) pairs at the branch head. More than one means
        the last intent had a branch whose reading is not known yet."""
        c = self.get_commit(self.head(branch))
        if isinstance(c, RootCommit):
            return [([], c.world)]
        if isinstance(c, ObservationCommit):
            return [([], c.observed_world)]
        return [(o.conditions, o.world) for o in c.outcomes]

    def working_world(self, branch: str) -> World:
        """The one world at the head, or a refusal if the head is unresolved."""
        worlds = self.head_worlds(branch)
        if len(worlds) != 1:
            raise StoreError(
                "S_UNRESOLVED",
                f"branch '{branch}' has {len(worlds)} possible worlds at its head; the last intent branches on a reading",
                "execute that intent first, then plan the next step from what was observed",
            )
        return self.get_world(worlds[0][1])

    # --- intents

    def commit_intent(self, branch: str, protocol: Protocol, message: str | None = None, clock: Clock = utc_now, budget: Budget | None = None) -> str:
        """Compile and lower the protocol against the branch head and record it. Raises
        CompileError if the protocol cannot happen."""
        h = self._make_intent(self.head(branch), branch, protocol, message, clock, budget)
        self.set_head(branch, h)
        return h

    def _make_intent(self, parent: str, branch: str, protocol: Protocol, message: str | None, clock: Clock, budget: Budget | None) -> str:
        worlds = self._worlds_at(parent)
        if len(worlds) != 1:
            raise StoreError("S_UNRESOLVED", f"the previous intent has {len(worlds)} possible outcomes", "execute it first")
        base = self.get_world(worlds[0][1])
        result = compile(base, protocol, budget=budget)
        program = lower(base, result.pir)
        sim = simulate(base, result.pir, Noise.normal(), seeds=EXPECTED_SEEDS)
        outcomes = [
            Outcome(conditions=[c.to_dict() for c in o.conditions], world=self.put_world(o.world), readings=[r.model_dump(mode="json") for r in s.readings])
            for o, s in zip(result.outcomes, sim.outcomes, strict=True)
        ]
        commit = IntentCommit(
            parent=parent,
            branch=branch,
            created_at=clock(),
            message=message,
            protocol=protocol.model_dump(mode="json", by_alias=True, exclude_none=True),
            budget=budget.model_dump(mode="json") if budget is not None else None,
            base_world=base.hash(),
            outcomes=outcomes,
            pir_ops=sum(1 for _ in result.pir),
            segments=len(program.segments),
            program=program.to_dict(),
        )
        return self.put_commit(commit)

    def _worlds_at(self, h: str) -> list[tuple[list[dict[str, Any]], str]]:
        c = self.get_commit(h)
        if isinstance(c, RootCommit):
            return [([], c.world)]
        if isinstance(c, ObservationCommit):
            return [([], c.observed_world)]
        return [(o.conditions, o.world) for o in c.outcomes]

    def program(self, intent_hash: str) -> Program:
        c = self.get_commit(intent_hash)
        if not isinstance(c, IntentCommit):
            raise StoreError("S_NOT_AN_INTENT", f"{intent_hash} is not an intent")
        return Program.model_validate(c.program)

    def vendor_files(self, intent_hash: str) -> list[tuple[str, str]]:
        c = self.get_commit(intent_hash)
        if not isinstance(c, IntentCommit):
            raise StoreError("S_NOT_AN_INTENT", f"{intent_hash} is not an intent")
        return opentrons.emit_program(self.get_world(c.base_world), Program.model_validate(c.program))

    # --- execution

    def execute(
        self,
        branch: str,
        observed: World | None = None,
        outcome: int | None = None,
        telemetry: Telemetry | None = None,
        message: str | None = None,
        clock: Clock = utc_now,
    ) -> str:
        """Record that the branch's head intent ran. `main` adopts the branch, then an
        observation commit lands with the observed world.

        With telemetry: the diff engine works out which outcome happened and estimates the
        observed world from the readings; the report is stored with the commit.
        Without either telemetry or an observed world, the chosen predicted outcome stands
        in for reality (and the commit says so)."""
        head = self.head(branch)
        c = self.get_commit(head)
        if not isinstance(c, IntentCommit):
            raise StoreError("S_NOTHING_TO_EXECUTE", f"branch '{branch}' has no unexecuted intent at its head")
        if not self.is_fast_forward_of_main(branch):
            raise StoreError("S_NOT_FAST_FORWARD", f"branch '{branch}' is not on top of main; reality moved on", "run `ztra store rebase` to recompile it against the current main, then execute")
        report: WorldDiff | None = None
        raw: dict[str, Any] = {}
        if telemetry is not None:
            raw = telemetry.model_dump(mode="json", exclude_none=True)
            compiled, sim = self._replay(c)
            report, estimate = diff(compiled, sim, telemetry, outcome)
            idx = report.outcome
            if observed is None:
                observed = estimate
        else:
            if len(c.outcomes) > 1 and outcome is None:
                raise StoreError("S_OUTCOME_REQUIRED", f"the intent has {len(c.outcomes)} outcomes; say which one happened", "pass --outcome N (0-based) or supply telemetry")
            idx = outcome or 0
            if idx < 0 or idx >= len(c.outcomes):
                raise StoreError("S_BAD_OUTCOME", f"outcome {idx} does not exist; there are {len(c.outcomes)}")
            if observed is None:
                observed = self.get_world(c.outcomes[idx].world)
                raw["observed_world"] = "assumed equal to the predicted outcome (no telemetry supplied)"
        oh = self.put_world(observed)
        self.set_head("main", head)
        obs = ObservationCommit(
            parent=head, created_at=clock(), message=message, executed=head, chosen_outcome=idx, observed_world=oh, telemetry=raw,
            report=report.model_dump(mode="json", exclude_none=True) if report is not None else None,
        )
        h = self.put_commit(obs)
        self.set_head("main", h)
        if branch != "main":
            self.set_head(branch, h)
        return h

    def execute_aborted(
        self,
        branch: str,
        partial: World,
        telemetry: Telemetry,
        reason: dict[str, Any],
        message: str | None = None,
        clock: Clock = utc_now,
    ) -> str:
        """Record a run that stopped before the end. `main` adopts the branch and the world the
        completed steps produced; the intent counts as executed (retrying means rebasing)."""
        head = self.head(branch)
        c = self.get_commit(head)
        if not isinstance(c, IntentCommit):
            raise StoreError("S_NOTHING_TO_EXECUTE", f"branch '{branch}' has no unexecuted intent at its head")
        if not self.is_fast_forward_of_main(branch):
            raise StoreError("S_NOT_FAST_FORWARD", f"branch '{branch}' is not on top of main; reality moved on")
        oh = self.put_world(partial)
        self.set_head("main", head)
        obs = ObservationCommit(
            parent=head, created_at=clock(), message=message, executed=head, chosen_outcome=None, observed_world=oh,
            telemetry=telemetry.model_dump(mode="json", exclude_none=True), report=None, status="aborted", reason=reason,
        )
        h = self.put_commit(obs)
        self.set_head("main", h)
        if branch != "main":
            self.set_head(branch, h)
        return h

    def _replay(self, c: IntentCommit) -> tuple[CompileOutput, Any]:
        """Recompile and resimulate an intent exactly as it was committed."""
        base = self.get_world(c.base_world)
        budget = Budget.model_validate(c.budget) if c.budget is not None else None
        compiled = compile(base, Protocol.model_validate(c.protocol), budget=budget)
        return compiled, simulate(base, compiled.pir, Noise.normal(), seeds=EXPECTED_SEEDS)

    # --- rebase

    def rebase(self, branch: str, clock: Clock = utc_now) -> list[str]:
        """Replay the branch's intents on top of main. Compile errors surface as-is and the
        branch is left untouched."""
        if branch == "main":
            raise StoreError("S_MAIN", "main is reality; it cannot be rebased")
        main_head = self.head("main")
        intents: list[IntentCommit] = []
        for h, c in self.history(branch):
            if self.is_ancestor(h, main_head):
                break
            if isinstance(c, IntentCommit):
                intents.insert(0, c)
        new_head = main_head
        new_hashes = []
        for c in intents:
            budget = Budget.model_validate(c.budget) if c.budget is not None else None
            new_head = self._make_intent(new_head, branch, Protocol.model_validate(c.protocol), c.message, clock, budget)
            new_hashes.append(new_head)
        self.set_head(branch, new_head)
        return new_hashes

    # --- integrity

    def verify(self) -> list[str]:
        """Recompute every hash on every branch. Returns the problems found."""
        problems: list[str] = []
        seen: set[str] = set()
        for branch, head in self.branches().items():
            h: str | None = head
            while h is not None and h not in seen:
                seen.add(h)
                try:
                    data = self._read(h)
                except StoreError:
                    problems.append(f"{branch}: missing commit {h}")
                    break
                if "commit" not in data:
                    problems.append(f"{branch}: {h} is not a commit")
                    break
                c = _CommitEnvelope.model_validate(data).commit
                if commit_hash(c) != h:
                    problems.append(f"{branch}: commit {h} does not match its content (edited after the fact?)")
                for wh in self._world_hashes(c):
                    try:
                        w = self.get_world(wh)
                    except StoreError:
                        problems.append(f"{branch}: commit {h} references missing world {wh}")
                        continue
                    if w.hash() != wh:
                        problems.append(f"{branch}: world {wh} does not match its content")
                if isinstance(c, ObservationCommit) and c.branch != "main":
                    problems.append(f"{branch}: observation {h} is not on main")
                h = c.parent
        return problems

    @staticmethod
    def _world_hashes(c: RootCommit | IntentCommit | ObservationCommit) -> list[str]:
        if isinstance(c, RootCommit):
            return [c.world]
        if isinstance(c, ObservationCommit):
            return [c.observed_world]
        return [c.base_world, *(o.world for o in c.outcomes)]

    # --- checkout

    def checkout(self, branch: str, out_dir: Path) -> World:
        """Write the head world as the three YAML files."""
        world = self.working_world(branch)
        write_world(world, out_dir)
        return world


def write_world(world: World, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    d = world.to_dict()
    for name, key in [(INVENTORY_FILE, "inventory"), (DECK_FILE, "deck"), (HARDWARE_FILE, "hardware")]:
        (out_dir / name).write_text(yaml.safe_dump(d[key], sort_keys=False))
