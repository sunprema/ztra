"""The runtime: takes an intent from the store and makes it happen, one segment at a time.

It never dispatches without approval, never dispatches the same intent twice (the store's
fast-forward rule plus a run journal that survives a crash), pauses for readings, decides
branches from those readings, and records what happened — including a run that stopped
halfway, whose world is what the completed steps would have produced."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field

from ztra.compiler import PathCondition, paths
from ztra.driver import Driver, DriverFault
from ztra.lower import Decide, ObserveL, Pause, Program
from ztra.model import Strict
from ztra.pir import ObserveOp, Origin, Transform
from ztra.simulate import evaluate, nominal_world
from ztra.store import Clock, IntentCommit, Store, StoreError, utc_now
from ztra.telemetry import EStop, TelemetryService

Approver = Callable[[IntentCommit, list[tuple[str, str]]], bool]


class RunResult(Strict):
    status: Literal["completed", "aborted", "refused"]
    intent: str
    observation: str | None = None
    segments: list[int] = Field(default_factory=list)
    decisions: list[bool] = Field(default_factory=list)
    readings: int = 0
    report: dict[str, Any] | None = None
    reason: dict[str, Any] | None = None
    log: list[str] = Field(default_factory=list)


class _Hooks:
    def __init__(self, telemetry: TelemetryService, done: list[tuple[int, int]], segment: int) -> None:
        self.telemetry = telemetry
        self.done = done
        self.segment = segment

    def on_observe(self, op: ObserveL, op_index: int) -> None:
        self.telemetry.read(op.sensor, op.label)

    def on_pause(self, op: Pause, op_index: int) -> None:
        pass  # a real driver waits for a person here; the fake one just carries on

    def on_op_done(self, op_index: int) -> None:
        self.done.append((self.segment, op_index))


class Runtime:
    def __init__(self, store: Store, driver: Driver, telemetry: TelemetryService, approve: Approver, clock: Clock = utc_now) -> None:
        self.store = store
        self.driver = driver
        self.telemetry = telemetry
        self.approve = approve
        self.clock = clock

    def run(self, branch: str, message: str | None = None) -> RunResult:
        s = self.store
        head = s.head(branch)
        c = s.get_commit(head)
        if not isinstance(c, IntentCommit):
            return self._refuse(head, StoreError("S_NOTHING_TO_EXECUTE", f"branch '{branch}' has no unexecuted intent at its head"))
        if not s.is_fast_forward_of_main(branch):
            return self._refuse(head, StoreError("S_NOT_FAST_FORWARD", f"branch '{branch}' is not on top of main; reality moved on", "rebase first"))
        journal = s.root / "runs" / f"{head}.json"
        if journal.exists() and '"dispatched"' in journal.read_text():
            return self._refuse(head, StoreError("S_RUN_IN_PROGRESS", f"intent {head[:12]} was dispatched and never recorded; a run may be underway or have crashed", f"check the robot, then delete {journal} to allow a new run"))
        files = s.vendor_files(head)
        program = s.program(head)
        if not self.approve(c, files):
            return self._refuse(head, StoreError("S_NOT_APPROVED", "the approver said no; nothing was dispatched"))

        journal.parent.mkdir(exist_ok=True)
        journal.write_text(f'{{"status": "dispatched", "started": "{self.clock()}"}}\n')
        base = s.get_world(c.base_world)
        done: list[tuple[int, int]] = []
        segments: list[int] = []
        decisions: list[bool] = []
        log: list[str] = []
        seg = 0
        try:
            while True:
                segment = program.segments[seg]
                segments.append(seg)
                log += self.driver.run_segment(base, seg, segment, files[seg][1], _Hooks(self.telemetry, done, seg))
                nxt = segment.next
                if not isinstance(nxt, Decide):
                    break
                reading = next((r for r in self.telemetry.readings if r.label == nxt.observation), None)
                if reading is None:
                    raise DriverFault("R_NO_READING", f"segment {seg} ended on a decision about '{nxt.observation}' but no such reading was taken")
                holds = evaluate(PathCondition(nxt.observation, nxt.condition, True), reading.values)
                if holds is None:
                    raise DriverFault("R_BAD_READING", f"reading '{nxt.observation}' has no metric '{nxt.condition.metric}'")
                decisions.append(holds)
                log.append(f"[runtime] {nxt.observation}: {nxt.condition} => {holds}; continuing with segment {nxt.then if holds else nxt.otherwise}")
                seg = nxt.then if holds else nxt.otherwise
        except (DriverFault, EStop) as e:
            partial = self._partial_world(c, program, segments, done, decisions)
            obs = s.execute_aborted(branch, partial, self.telemetry.telemetry(), e.to_dict(), message, self.clock)
            journal.write_text('{"status": "recorded", "outcome": "aborted"}\n')
            log.append(f"[runtime] aborted: {e}")
            return RunResult(status="aborted", intent=head, observation=obs, segments=segments, decisions=decisions, readings=len(self.telemetry.readings), reason=e.to_dict(), log=log)

        obs = s.execute(branch, telemetry=self.telemetry.telemetry(), message=message, clock=self.clock)
        journal.write_text('{"status": "recorded", "outcome": "completed"}\n')
        oc = s.get_commit(obs)
        report = getattr(oc, "report", None)
        summary = {k: report[k] for k in ("classification", "counts", "can_localize", "unaccounted")} if report else None
        return RunResult(status="completed", intent=head, observation=obs, segments=segments, decisions=decisions, readings=len(self.telemetry.readings), report=summary, log=log)

    @staticmethod
    def _refuse(head: str, e: StoreError) -> RunResult:
        return RunResult(status="refused", intent=head, reason=e.to_dict())

    def _partial_world(self, c: IntentCommit, program: Program, segments: list[int], done: list[tuple[int, int]], decisions: list[bool]) -> Any:
        """The world the completed steps would have produced. A protocol step counts as done
        only when every robot op it lowered to has run."""
        compiled, _ = self.store._replay(c)
        all_paths = paths(compiled.pir)
        chosen = next((ops for conds, ops in all_paths if [x.holds for x in conds][: len(decisions)] == decisions), all_paths[0][1])
        total: dict[str, int] = {}
        finished: dict[str, int] = {}
        for si in segments:
            for oi, op in enumerate(program.segments[si].ops):
                key = op.origin.model_dump_json()
                total[key] = total.get(key, 0) + 1
                if (si, oi) in done:
                    finished[key] = finished.get(key, 0) + 1
        completed: list[Transform | ObserveOp] = [op for op in chosen if finished.get(_key(op.origin), 0) == total.get(_key(op.origin), -1)]
        return nominal_world(self.store.get_world(c.base_world), completed)


def _key(origin: Origin) -> str:
    return origin.model_dump_json()
