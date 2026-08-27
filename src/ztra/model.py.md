---
path: "src/ztra/model.py"
summary: "Defines Strict, the pydantic base class with extra=\"forbid\" that every schema model inherits."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: false
---

# src/ztra/model.py

One line of substance: `Strict`, the pydantic base class every schema model in the codebase inherits
from, with `extra="forbid"`. An unrecognized field in a YAML world or protocol file is a validation
error instead of being silently dropped — load-bearing for NFR-4.1's determinism guarantee (a typo'd
field name should never change behavior by disappearing) and for keeping the schema the actual source
of truth for what's allowed.
