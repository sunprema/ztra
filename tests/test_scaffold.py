"""`ztra init` writes a world that validates cleanly and a protocol that compiles,
and refuses to overwrite an existing project."""

from pathlib import Path

import pytest

from ztra.compiler import compile
from ztra.protocol import Protocol
from ztra.scaffold import FILES, ScaffoldError, scaffold
from ztra.world import World, validate


def test_scaffold_is_a_working_project(tmp_path: Path) -> None:
    created = scaffold(tmp_path)
    assert sorted(created) == sorted(FILES)

    world = World.load(tmp_path / "world")
    assert validate(world) == []  # no errors, and no warnings either

    protocol = Protocol.load(tmp_path / "protocols" / "first_protocol.yaml")
    result = compile(world, protocol)
    assert result.pir


def test_scaffold_refuses_to_overwrite(tmp_path: Path) -> None:
    scaffold(tmp_path)
    with pytest.raises(ScaffoldError):
        scaffold(tmp_path)
    marker = tmp_path / "world" / "Deck.yaml"
    before = marker.read_text()
    with pytest.raises(ScaffoldError):
        scaffold(tmp_path)
    assert marker.read_text() == before
