"""The store: the U4 scenario end to end, plus the rules around unresolved outcomes,
integrity and checkout."""

import json
from pathlib import Path

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler import CompileError
from ztra.protocol import Protocol
from ztra.sensors import Telemetry
from ztra.store import IntentCommit, ObservationCommit, RootCommit, Store, StoreError, commit_hash
from ztra.world import World

CLOCK = lambda: "2026-08-25T12:00:00+00:00"  # noqa: E731

T = "version: 1\nsteps:\n"


def proto(vial: str, well: str, vol: float) -> Protocol:
    return Protocol.from_yaml(T + f"  - {{ op: transfer, from: {{ vial: {vial} }}, to: {{ plate: P1, well: {well} }}, volume_ul: {vol} }}\n")


@pytest.fixture
def store(tmp_path: Path, world: World) -> Store:
    return Store.init(tmp_path / ".ztra", world, CLOCK)


def test_init_and_open(tmp_path: Path, world: World) -> None:
    s = Store.init(tmp_path / ".ztra", world, CLOCK)
    assert s.branches() == {"main": s.head("main")}
    root = s.get_commit(s.head("main"))
    assert isinstance(root, RootCommit) and root.world == world.hash()
    assert s.working_world("main") == world
    again = Store.open(tmp_path / ".ztra")
    assert again.head("main") == s.head("main")
    with pytest.raises(StoreError) as e:
        Store.init(tmp_path / ".ztra", world)
    assert e.value.code == "S_EXISTS"
    with pytest.raises(StoreError) as e:
        Store.open(tmp_path / "nowhere")
    assert e.value.code == "S_NOT_A_STORE"


def test_u4_scenario(store: Store) -> None:
    s = store
    assert s.working_world("main").inventory.vials["V_hcl"].volume_ul == 200

    s.branch("hyp-A")
    s.branch("hyp-B")
    a = s.commit_intent("hyp-A", proto("V_hcl", "A2", 150), "use most of the acid", CLOCK)
    b = s.commit_intent("hyp-B", proto("V_hcl", "B2", 120), None, CLOCK)
    # both compile: consumption is only virtual on a branch
    assert s.working_world("hyp-A").inventory.vials["V_hcl"].volume_ul == 50
    assert s.working_world("hyp-B").inventory.vials["V_hcl"].volume_ul == 80
    assert s.working_world("main").inventory.vials["V_hcl"].volume_ul == 200

    # execute A; reality dispensed a little short
    observed = s.working_world("hyp-A")
    observed.inventory.vials["V_hcl"].volume_ul = 53
    obs = s.execute("hyp-A", observed, clock=CLOCK)
    c = s.get_commit(obs)
    assert isinstance(c, ObservationCommit) and c.executed == a and c.parent == a
    assert s.head("main") == obs and s.head("hyp-A") == obs
    assert s.working_world("main").inventory.vials["V_hcl"].volume_ul == 53, "main adopts the observed world, not the predicted one"
    kinds = [c.kind for _, c in s.history("main")]
    assert kinds == ["observation", "intent", "root"]

    # B is stale: cannot execute, must rebase, and the rebase fails honestly
    with pytest.raises(StoreError) as e:
        s.execute("hyp-B", clock=CLOCK)
    assert e.value.code == "S_NOT_FAST_FORWARD"
    with pytest.raises(CompileError) as ce:
        s.rebase("hyp-B", CLOCK)
    assert ce.value.code == "E_VOLUME" and ce.value.resource == "V_hcl"
    assert s.head("hyp-B") == b, "a failed rebase leaves the branch alone"

    # a smaller plan rebases fine
    s.branch("hyp-C", "hyp-B")
    s.set_head("hyp-C", s.head("main"))
    c2 = s.commit_intent("hyp-C", proto("V_hcl", "B2", 20), clock=CLOCK)
    assert s.is_fast_forward_of_main("hyp-C")
    s.branch("hyp-D")
    d = s.commit_intent("hyp-D", proto("V_hcl", "C2", 30), clock=CLOCK)
    s.execute("hyp-D", clock=CLOCK)
    assert not s.is_fast_forward_of_main("hyp-C")
    replayed = s.rebase("hyp-C", CLOCK)
    assert len(replayed) == 1 and replayed[0] != c2 and s.is_fast_forward_of_main("hyp-C")
    assert s.working_world("hyp-C").inventory.vials["V_hcl"].volume_ul == 53 - 30 - 20
    assert d != replayed[0]


def test_execute_needs_an_intent_and_records_assumption(store: Store) -> None:
    with pytest.raises(StoreError) as e:
        store.execute("main")
    assert e.value.code == "S_NOTHING_TO_EXECUTE"
    h = store.commit_intent("main", proto("V_water", "A2", 50), clock=CLOCK)
    obs = store.execute("main", clock=CLOCK)
    c = store.get_commit(obs)
    assert isinstance(c, ObservationCommit) and c.executed == h
    assert "assumed" in c.telemetry["observed_world"]
    assert store.working_world("main").inventory.vials["V_water"].volume_ul == 950


def test_cannot_plan_past_an_unresolved_reading(store: Store, world: World) -> None:
    demo = Protocol.load(EXAMPLES / "protocols/demo.yaml")
    h = store.commit_intent("main", demo, clock=CLOCK)
    c = store.get_commit(h)
    assert isinstance(c, IntentCommit) and len(c.outcomes) == 2 and c.segments == 3
    with pytest.raises(StoreError) as e:
        store.commit_intent("main", proto("V_water", "A2", 50), clock=CLOCK)
    assert e.value.code == "S_UNRESOLVED"
    with pytest.raises(StoreError) as e:
        store.execute("main", clock=CLOCK)
    assert e.value.code == "S_OUTCOME_REQUIRED"
    with pytest.raises(StoreError) as e:
        store.execute("main", outcome=5, clock=CLOCK)
    assert e.value.code == "S_BAD_OUTCOME"
    obs = store.execute("main", outcome=1, clock=CLOCK)
    assert store.get_commit(obs).chosen_outcome == 1  # type: ignore[union-attr]
    assert store.working_world("main").hash() == c.outcomes[1].world
    store.commit_intent("main", proto("V_water", "A2", 50), clock=CLOCK)  # resolved now


def test_intent_carries_everything_needed_to_dispatch(store: Store) -> None:
    h = store.commit_intent("main", proto("V_water", "A2", 50), clock=CLOCK)
    files = store.vendor_files(h)
    assert [n for n, _ in files] == ["segment_0.py"]
    assert 'pip_p300_single_gen2.aspirate(50, TR1["A1"])' in files[0][1]
    assert store.program(h).walk([]) == [0]


def test_tampering_is_detected(store: Store) -> None:
    h = store.commit_intent("main", proto("V_water", "A2", 50), "honest", clock=CLOCK)
    store.execute("main", clock=CLOCK)
    assert store.verify() == []
    path = store.objects / f"{h}.json"
    data = json.loads(path.read_text())
    data["commit"]["message"] = "edited after the fact"
    path.write_text(json.dumps(data))
    problems = store.verify()
    assert len(problems) == 1 and "edited" in problems[0]
    # and a world snapshot that no longer matches its name
    wpath = store.objects / f"{store.working_world('main').hash()}.json"
    wdata = json.loads(wpath.read_text())
    wdata["world"]["inventory"]["vials"]["V_water"]["volume_ul"] = 999
    wpath.write_text(json.dumps(wdata))
    assert any("world" in p for p in store.verify())


def test_hashes_are_content_addressed_and_stable(store: Store) -> None:
    h1 = store.commit_intent("main", proto("V_water", "A2", 50), clock=CLOCK)
    c = store.get_commit(h1)
    assert commit_hash(c) == h1 and len(h1) == 64
    assert len(store.working_world("main").hash()) == 64


def test_checkout_round_trips(store: Store, tmp_path: Path) -> None:
    store.commit_intent("main", proto("V_water", "A2", 50), clock=CLOCK)
    out = tmp_path / "checkout"
    w = store.checkout("main", out)
    assert sorted(p.name for p in out.iterdir()) == ["Deck.yaml", "Hardware.yaml", "Inventory.yaml"]
    assert World.load(out) == w


def test_branch_rules(store: Store) -> None:
    with pytest.raises(StoreError):
        store.branch("main")
    with pytest.raises(StoreError):
        store.branch("bad/name")
    with pytest.raises(StoreError) as e:
        store.head("ghost")
    assert e.value.code == "S_NO_BRANCH"
    with pytest.raises(StoreError) as e:
        store.rebase("main")
    assert e.value.code == "S_MAIN"
