---
path: "src/ztra/backend/__init__.py"
summary: "Docstring-only contract: every backend module turns one PIR-L segment into vendor-runnable output, one file per segment."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: false
---

Docstring only: states the contract every backend module must meet — turn one PIR-L segment into something a vendor can run, one output file per segment, run in segment order by the runtime. `opentrons.py` is the only implementation today; a SiLA2 backend would live alongside it under this same contract.
