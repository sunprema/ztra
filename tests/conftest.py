import os
from pathlib import Path

import pytest

from ztra.world import World

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def world() -> World:
    return World.load(EXAMPLES / "world")


@pytest.fixture
def world_flex() -> World:
    return World.load(EXAMPLES / "world_flex")


def sim_binary(env: str) -> str | None:
    return os.environ.get(env)
