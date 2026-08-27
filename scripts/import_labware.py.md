---
path: "scripts/import_labware.py"
summary: "Prints Hardware.yaml labware entries for Opentrons load names, run inside a vendor venv."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: false
---

A one-off developer script, not part of the `ztra` package: turns an Opentrons labware definition (looked up by load name via `opentrons_shared_data`, which is only installed in a vendor venv) into the YAML block `Hardware.yaml`'s `labware:` catalog expects — kind, grid size, capacity, height. Saves hand-transcribing numbers out of the vendor's labware JSON when adding support for a new plate, tube rack, or tip rack. Must be run with a vendor venv's Python (see the module docstring for the exact invocation); output is meant to be pasted, not consumed programmatically.
