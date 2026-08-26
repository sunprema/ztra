"""The world model: what's in the lab, where it sits, and what the robot can do.

Three files, one concern each:
- Inventory.yaml — reagents, vials, plates and what's in them
- Deck.yaml      — where everything sits, which tips are used, where each vial is
- Hardware.yaml  — robot, pipettes, labware catalog, sensors
"""

import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import ValidationError

from ztra.model import Strict
from ztra.world.deck import Deck
from ztra.world.hardware import Hardware
from ztra.world.inventory import Inventory

SCHEMA_VERSION = 1
INVENTORY_FILE = "Inventory.yaml"
DECK_FILE = "Deck.yaml"
HARDWARE_FILE = "Hardware.yaml"


class LoadError(Exception):
    """A file could not be read or does not match the schema."""

    def __init__(self, file: str, message: str) -> None:
        super().__init__(f"{file}: {message}")
        self.file = file
        self.message = message


M = TypeVar("M", bound=Strict)


def parse_yaml(model: type[M], file: str, text: str) -> M:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LoadError(file, f"not valid YAML: {e}") from e
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise LoadError(file, format_validation_error(e)) from e


def format_validation_error(e: ValidationError) -> str:
    """One line per problem, with the path into the document."""
    lines = []
    for err in e.errors():
        path = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"{path}: {err['msg']}")
    return "; ".join(lines)


class World(Strict):
    inventory: Inventory
    deck: Deck
    hardware: Hardware

    @staticmethod
    def load(directory: Path) -> "World":
        def read(name: str) -> str:
            try:
                return (directory / name).read_text()
            except OSError as e:
                raise LoadError(name, str(e)) from e

        return World.from_yaml(read(INVENTORY_FILE), read(DECK_FILE), read(HARDWARE_FILE))

    @staticmethod
    def from_yaml(inventory: str, deck: str, hardware: str) -> "World":
        """Same as load, but from strings. Handy for tests."""
        return World(
            inventory=parse_yaml(Inventory, INVENTORY_FILE, inventory),
            deck=parse_yaml(Deck, DECK_FILE, deck),
            hardware=parse_yaml(Hardware, HARDWARE_FILE, hardware),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def canonical_json(self) -> str:
        """Sorted keys, no whitespace. This is what we hash and what goes over the CLI."""
        return canonical(self.to_dict())

    def hash(self) -> str:
        """SHA-256 of the canonical form. This is the id a world snapshot is stored under."""
        return sha256_hex(self.canonical_json())

    def clone(self) -> "World":
        return self.model_copy(deep=True)

    def _repr_html_(self) -> str:
        """Jupyter shows the bench as a picture instead of a wall of fields."""
        from ztra.viz import world_html

        return world_html(self)


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


from ztra.world.validate import Issue, Severity, validate  # noqa: E402  (re-export)

__all__ = ["World", "LoadError", "Issue", "Severity", "validate", "SCHEMA_VERSION", "canonical", "sha256_hex"]
