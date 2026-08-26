"""Pre-flight: the whole budget in one pass, and it rides along on resource errors."""

import asyncio
import json
from typing import Any

import pytest

from tests.conftest import EXAMPLES
from ztra.compiler_errors import CompileError
from ztra.preflight import attach, preflight
from ztra.protocol import Protocol
from ztra.schedule import Budget
from ztra.world import World

T = "version: 1\nsteps:\n"
EIGHT = T + """  - { op: thaw, vial: V_enzyme }
  - op: for_wells
    wells: [A2..H2]
    as: w
    body:
      - { op: transfer, from: { vial: V_water },  to: { plate: P1, well: $w }, volume_ul: 180 }
      - { op: transfer, from: { vial: V_enzyme }, to: { plate: P1, well: $w }, volume_ul: 20 }
"""


def test_u8_shortfalls_all_at_once(world: World) -> None:
    r = preflight(world, Protocol.from_yaml(EIGHT), Budget(sensor="scale_1", every=3))
    assert not r.feasible and r.paths == 1
    assert r.vials["V_water"].model_dump() == {"needed": 1440.0, "available": 1000.0, "shortfall": 440.0, "unit": "uL"}
    assert r.vials["V_enzyme"].shortfall == 10.0
    assert r.reagents["water"].shortfall == 440.0
    assert r.tips["p300_single_gen2"].model_dump() == {"needed": 16, "available": 94, "shortfall": 0, "unit": "tips"}
    assert r.wells_over_capacity == [] and r.frozen_used == []
    assert len(r.summary) == 2 and any("short by 440 uL" in line and "no other water vial" in line for line in r.summary)


def test_ok_case_reports_margins(world: World) -> None:
    r = preflight(world, Protocol.load(EXAMPLES / "protocols/enzyme_dilution.yaml"))
    assert r.feasible and r.summary == ["enough of everything — V_enzyme: 100/150 uL, V_water: 900/1000 uL, p300_single_gen2: 15/94 tips"]


def test_tips_capacity_and_frozen(world: World) -> None:
    rack = world.deck.tip_racks["TIPS1"]
    rack.used = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"][:90]
    y = T + """  - op: for_wells
    wells: [A3..H3]
    body:
      - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: $well }, volume_ul: 100 }
  - { op: transfer, from: { vial: V_enzyme }, to: { plate: P1, well: A1 }, volume_ul: 20 }
  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A1 }, volume_ul: 150 }
  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A1 }, volume_ul: 150 }
"""
    r = preflight(world, Protocol.from_yaml(y))
    assert r.tips["p300_single_gen2"].shortfall == 5  # 11 needed, 6 free
    assert r.wells_over_capacity[0].model_dump() == {"plate": "P1", "well": "A1", "peak_ul": 370.0, "capacity_ul": 360.0}
    assert r.frozen_used == ["V_enzyme"]
    assert r.vials["V_water"].shortfall == 100.0  # 800 + 300 > 1000
    assert len(r.summary) == 4


def test_worst_case_across_paths(world: World) -> None:
    y = T + """  - { op: observe, sensor: scale_1, label: a }
  - op: if_observed
    observation: a
    condition: { metric: mass_mg, cmp: gt, value: 100 }
    then:      [ { op: transfer, from: { vial: V_hcl }, to: { plate: P1, well: D1 }, volume_ul: 190 } ]
    otherwise: [ { op: transfer, from: { vial: V_hcl }, to: { plate: P1, well: D1 }, volume_ul: 20 } ]
  - { op: transfer, from: { vial: V_hcl }, to: { plate: P1, well: D2 }, volume_ul: 20 }
"""
    r = preflight(world, Protocol.from_yaml(y))
    assert r.paths == 2 and r.vials["V_hcl"].needed == 210.0 and r.vials["V_hcl"].shortfall == 10.0


def test_structural_errors_still_raise(world: World) -> None:
    with pytest.raises(CompileError) as e:
        preflight(world, Protocol.from_yaml(T + "  - { op: mix, at: { plate: P1, well: $w }, volume_ul: 50 }\n"))
    assert e.value.code == "E_UNBOUND_VARIABLE"


def test_resource_errors_carry_the_preflight(world: World) -> None:
    from ztra.compiler import compile

    p = Protocol.from_yaml(EIGHT)
    with pytest.raises(CompileError) as e:
        compile(world, p)
    d = attach(e.value.to_dict(), world, p, None)
    assert d["code"] == "E_VOLUME" and any("short by 440 uL" in line for line in d["preflight"])
    frozen = Protocol.from_yaml(T + "  - { op: transfer, from: { vial: V_water }, to: { plate: P1, well: A2 }, volume_ul: 100 }\n  - { op: mix, at: { plate: P1, well: Q9 }, volume_ul: 50 }\n")
    with pytest.raises(CompileError) as e:
        compile(world, frozen)
    assert "preflight" not in attach(e.value.to_dict(), world, frozen, None), "coordinate errors are not about stock"


def test_mcp_tool_and_error_attachment() -> None:
    pytest.importorskip("mcp")
    from ztra.mcp_server import server

    def call(tool: str, **args: Any) -> Any:
        r = asyncio.run(server.call_tool(tool, args))
        sc = getattr(r, "structured_content", None)
        return sc if sc is not None else json.loads(r.content[0].text)  # type: ignore[union-attr]

    pf = call("preflight_protocol", world_dir=str(EXAMPLES / "world"), protocol_yaml=EIGHT)
    assert pf["ok"] and not pf["feasible"] and pf["vials"]["V_water"]["shortfall"] == 440.0  # ok = the tool ran
    c = call("compile_protocol", world_dir=str(EXAMPLES / "world"), protocol_yaml=EIGHT)
    assert not c["ok"] and c["error"]["code"] == "E_VOLUME" and any("440" in line for line in c["error"]["preflight"])
