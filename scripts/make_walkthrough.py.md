---
path: "scripts/make_walkthrough.py"
summary: "Builds and executes examples/walkthrough.ipynb from a hardcoded cell list, so the notebook stays real output rather than hand-edited prose."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: false
---

Generates `examples/walkthrough.ipynb` from a hardcoded list of markdown/code cells (`CELLS`) and executes it in-place with `nbclient`, so the checked-in notebook is always the *actual* output of running ztra's example world, not hand-edited prose that can drift from the real API. This is why the file was regenerated in the most recent commit (`265513c`, "regenerated against the current example world") — whenever the example protocols or world files change, this script is the way to bring the notebook back in sync, rather than editing the `.ipynb` directly.

The notebook itself is ztra's own guided tour: load the example world (renders as a picture via `viz.py`), compile a protocol with an observation budget, watch it run step by step with `trace`/`animate_html`, see what a real compile error looks like, then compare a simulated prediction against recorded telemetry through the diff engine — i.e. the whole workflow loop from ARCHITECTURE.md §5, run as a live Python library rather than through the CLI.
