"""The MCP server: tools are listed, return JSON, and refuse with structured errors."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from tests.conftest import EXAMPLES  # noqa: E402
from ztra.mcp_server import TOPICS, server  # noqa: E402

WORLD = str(EXAMPLES / "world")
DEMO = str(EXAMPLES / "protocols/demo.yaml")
TEL = str(EXAMPLES / "telemetry/demo_short_fill.yaml")


def call(tool: str, **args: Any) -> Any:
    r = asyncio.run(server.call_tool(tool, args))
    assert not getattr(r, "is_error", False), r
    sc = getattr(r, "structured_content", None)
    if isinstance(sc, dict) and "result" in sc and len(sc) == 1:
        return sc["result"]
    if sc is not None:
        return sc
    text = r.content[0].text  # type: ignore[union-attr]
    try:
        return json.loads(text)
    except ValueError:
        return text


def test_tools_and_resources_are_registered() -> None:
    tools = {t.name for t in asyncio.run(server.list_tools())}
    for name in ["reference", "world_summary", "world_validate", "compile_protocol", "simulate_protocol", "lower_protocol", "diff_run", "store_init", "store_branch", "store_log", "store_show", "store_world", "store_checkout", "store_commit", "store_files", "store_execute", "store_rebase", "store_verify"]:
        assert name in tools, name
    uris = {str(r.uri) for r in asyncio.run(server.list_resources())}
    assert {f"ztra://docs/{t}" for t in TOPICS} <= uris
    text = call("reference", topic="protocol")
    assert "E_VOLUME" in text and "if_observed" in text
    assert "unknown topic" in call("reference", topic="nope")


def test_world_tools() -> None:
    s = call("world_summary", world_dir=WORLD)
    assert s["ok"] and s["vials"]["V_hcl"]["volume_ul"] == 200 and s["tips_free"] == {"TIPS1": 94}
    assert s["plates"]["P1"]["wells_ul"] == {"A1": 50.0} and s["errors"] == []
    v = call("world_validate", world_dir=WORLD)
    assert v["ok"] and v["valid"] and v["issues"] == []
    bad = call("world_summary", world_dir="/nowhere")
    assert not bad["ok"] and bad["error"]["code"] == "LOAD_ERROR"


def test_compile_simulate_lower_and_errors_are_structured() -> None:
    c = call("compile_protocol", world_dir=WORLD, protocol_path=DEMO)
    assert c["ok"] and len(c["outcomes"]) == 2 and c["outcomes"][0]["cost"]["tips_used"] == 6 and "world" not in c["outcomes"][0]
    bad = call("compile_protocol", world_dir=WORLD, protocol_yaml="version: 1\nsteps:\n  - { op: transfer, from: { vial: V_enzyme }, to: { plate: P1, well: A2 }, volume_ul: 25 }\n")
    assert not bad["ok"] and bad["error"]["code"] == "E_STATE" and "thaw" in bad["error"]["hint"]
    garbage = call("compile_protocol", world_dir=WORLD, protocol_yaml="version: 1\nsteps:\n  - { op: teleport }\n")
    assert not garbage["ok"] and garbage["error"]["code"] == "LOAD_ERROR"
    sim = call("simulate_protocol", world_dir=WORLD, protocol_path=DEMO, seeds=5, dispense_drift=0.02, budget="sensor=camera_1,every=3")
    assert sim["ok"] and sim["seeds"] == 5 and [r["label"] for r in sim["outcomes"][0]["readings"]][:2] == ["auto_1", "after_fill"]
    low = call("lower_protocol", world_dir=WORLD, protocol_path=DEMO)
    assert low["ok"] and len(low["program"]["segments"]) == 3 and "segment_2.py" in low["files"]
    d = call("diff_run", world_dir=WORLD, protocol_path=DEMO, telemetry_path=TEL)
    assert d["ok"] and d["outcome"] == 1 and d["classification"] == "ok", "an 8 mg shortfall is within a healthy pipette's accuracy"
    nb = call("compile_protocol", world_dir=WORLD, protocol_path=DEMO, budget="every=3")
    assert not nb["ok"] and nb["error"]["code"] == "LOAD_ERROR"


def test_store_loop_through_the_server(tmp_path: Path) -> None:
    repo = str(tmp_path / ".ztra")
    assert call("store_init", repo=repo, world_dir=WORLD)["ok"]
    assert call("store_branch", repo=repo, name="hyp")["ok"]
    c = call("store_commit", repo=repo, branch="hyp", protocol_path=DEMO, message="fill and check", budget="sensor=scale_1")
    assert c["ok"] and c["segments"] == 3 and len(c["outcomes"]) == 2 and len(c["outcomes"][0]["expected_readings"]) == 2
    w = call("store_world", repo=repo, branch="hyp")
    assert not w["ok"] and w["error"]["code"] == "S_UNRESOLVED"
    f = call("store_files", repo=repo, hash=c["hash"])
    assert f["ok"] and sorted(f["files"]) == ["segment_0.py", "segment_1.py", "segment_2.py"]
    e = call("store_execute", repo=repo, branch="hyp", telemetry_path=TEL, message="ran it")
    assert e["ok"] and e["outcome"] == 1 and e["report"]["classification"] == "ok"
    log = call("store_log", repo=repo)
    assert [x["kind"] for x in log["commits"]] == ["observation", "intent", "root"]
    assert call("store_world", repo=repo)["ok"]
    assert call("store_verify", repo=repo)["intact"]
    assert call("store_checkout", repo=repo, branch="main", out_dir=str(tmp_path / "co"))["ok"]
    assert (tmp_path / "co" / "Deck.yaml").exists()
    stale = call("store_execute", repo=repo, branch="hyp", outcome=0)
    assert not stale["ok"] and stale["error"]["code"] == "S_NOTHING_TO_EXECUTE"
