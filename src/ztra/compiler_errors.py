"""The error a compile pass raises. Shaped so an agent can act on it."""

from __future__ import annotations

from typing import Any

from ztra.pir import Origin


class CompileError(Exception):
    """A protocol step that cannot happen. Shaped so an agent can act on it."""

    def __init__(
        self,
        code: str,
        physical_law: str,
        resource: str,
        expected: str,
        actual: str,
        hint: str,
        *,
        origin: Origin | None = None,
        branch_path: list[str] | None = None,
        coordinate: str | None = None,
        chain_of_thought: list[str] | None = None,
    ) -> None:
        super().__init__(f"{code}: {physical_law} ({resource})")
        self.code = code
        self.step_path = list(origin.step_path) if origin else []
        self.iterations = list(origin.iterations) if origin else []
        self.branch_path = branch_path or []  # which branch decisions lead here; empty if none
        self.physical_law = physical_law
        self.resource = resource
        self.coordinate = coordinate
        self.expected = expected
        self.actual = actual
        self.hint = hint
        self.chain_of_thought = chain_of_thought or []

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "step_path": self.step_path}
        if self.iterations:
            d["iterations"] = self.iterations
        if self.branch_path:
            d["branch_path"] = self.branch_path
        d.update(physical_law=self.physical_law, resource=self.resource)
        if self.coordinate is not None:
            d["coordinate"] = self.coordinate
        d.update(expected=self.expected, actual=self.actual, hint=self.hint, chain_of_thought=self.chain_of_thought)
        return d
