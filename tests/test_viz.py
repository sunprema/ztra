"""Notebook views: the SVG/HTML renderers produce what they claim, and an accurate
replay ends exactly on the world the compiler predicted."""

from pathlib import Path

from ztra.compiler import compile
from ztra.protocol import Protocol
from ztra.scaffold import scaffold
from ztra.viz import animate_html, deck_svg, plate_svg, tip_rack_svg, trace, vials_html, world_html
from ztra.world import World


def project(tmp_path: Path) -> tuple[World, Protocol]:
    scaffold(tmp_path)
    return World.load(tmp_path / "world"), Protocol.load(tmp_path / "protocols" / "first_protocol.yaml")


def test_world_renders(tmp_path: Path) -> None:
    world, _ = project(tmp_path)
    page = world_html(world)
    assert "<svg" in page and "P1" in page and "V_water" in page and "TIPS1" in page
    assert world._repr_html_() == page
    assert 'title>A1' in plate_svg(world, "P1")  # hoverable wells
    assert "trash" in deck_svg(world)
    assert "fresh" in tip_rack_svg(world, "TIPS1")
    assert "❄" in vials_html(world)  # V_sample starts frozen


def test_trace_matches_the_prediction(tmp_path: Path) -> None:
    world, protocol = project(tmp_path)
    frames = trace(world, protocol)
    assert frames[0].description == "start"
    assert frames[0].world.hash() == world.hash()
    assert len(frames) > 10  # thaw + 3 wells x (2 transfers + mix) + observe, each several ops
    predicted = compile(world, protocol).outcomes[0].world
    assert frames[-1].world.hash() == predicted.hash()
    # the film shows the wells filling up
    a1 = [f.world.inventory.plates["P1"].wells.get("A1", []) for f in frames]
    assert sum(l.volume_ul for l in a1[0]) == 0
    assert sum(l.volume_ul for l in a1[-1]) == 200


def test_animation_is_self_contained(tmp_path: Path) -> None:
    world, protocol = project(tmp_path)
    page = animate_html(trace(world, protocol), title="first protocol")
    assert page.count('class="ztra') >= len(trace(world, protocol)) - 1
    assert "<script>" in page and "first protocol" in page
