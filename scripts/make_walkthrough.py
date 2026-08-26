"""Build and execute examples/walkthrough.ipynb. Run from the repo root:

    .venv/bin/python scripts/make_walkthrough.py

Needs nbformat, nbclient and ipykernel (dev tools, not package dependencies)."""

from pathlib import Path

import nbformat
from nbclient import NotebookClient

md = lambda s: nbformat.v4.new_markdown_cell(s)
code = lambda s: nbformat.v4.new_code_cell(s)

CELLS = [
    md(
        "# A walk through ztra\n\n"
        "This notebook runs the whole loop on the example lab in `examples/world`: look at the bench, "
        "check a protocol, watch it run step by step, see what a good error looks like, and compare a "
        "(pretend) real run against the prediction.\n\n"
        "Everything here uses ztra as a plain Python library — the same code the `ztra` CLI wraps."
    ),
    md("## The bench\n\nA world is three YAML files. Evaluating it in a cell draws it: the deck, each plate, the tip racks, and the vials with their fill levels (❄ marks a frozen vial). Hover a well for its contents."),
    code(
        "from pathlib import Path\n"
        "from ztra.world import World\n\n"
        'world = World.load(Path("world"))\n'
        "world"
    ),
    md("## Checking a protocol\n\nThe protocol dilutes an enzyme 1:10 into five wells. We compile it with an observation budget: a scale reading every three transfers, so a failed run can be localized later."),
    code(
        "from ztra.protocol import Protocol\n"
        "from ztra.compiler import compile\n"
        "from ztra.schedule import Budget\n\n"
        'protocol = Protocol.load(Path("protocols/enzyme_dilution.yaml"))\n'
        'budget = Budget.parse("sensor=scale_1,every=3")\n'
        "result = compile(world, protocol, budget=budget)\n"
        'print(len(result.pir), "checked steps,", len(result.outcomes), "predicted outcome(s)")\n'
        "result.outcomes[0].cost.to_dict()"
    ),
    md("## Watching it run\n\nA trace replays the lowered program one robot step at a time with ideal pipettes. Drag the slider (or press play) to watch the wells fill, the vials drain, and the tips get used up. The final frame is exactly the world the compiler predicted."),
    code(
        "from ztra.viz import trace, animate_html\n"
        "from IPython.display import HTML\n\n"
        "frames = trace(world, protocol, budget=budget)\n"
        "HTML(animate_html(frames, title=protocol.name))"
    ),
    md("## When it can't work\n\nThe same compiler refuses a protocol the lab cannot satisfy — before anything moves. This one loops until a vial runs dry; the error names the step, the vial, and what to do."),
    code(
        "from ztra.compiler_errors import CompileError\n\n"
        'bad = Protocol.load(Path("protocols/bad_loop_drains_vial.yaml"))\n'
        "try:\n"
        "    compile(world, bad)\n"
        "except CompileError as e:\n"
        "    err = e.to_dict()\n"
        '{k: err[k] for k in ("code", "physical_law", "resource", "expected", "actual", "hint") if k in err}'
    ),
    md(
        "## Prediction vs reality\n\n"
        "For the branching demo protocol, the simulator predicts what the scale should read (with the pipettes' "
        "real-world sloppiness folded in), and the diff engine compares that against telemetry from a run. Here the "
        "recorded run came up 8 mg light, so the branch took its `otherwise` arm and the deviation is classified."
    ),
    code(
        "from ztra.simulate import simulate, Noise\n"
        "from ztra.sensors import Telemetry\n"
        "from ztra.diff import diff\n"
        "from ztra.store import EXPECTED_SEEDS\n\n"
        'demo = Protocol.load(Path("protocols/demo.yaml"))\n'
        "compiled = compile(world, demo)\n"
        "sim = simulate(world, compiled.pir, Noise.normal(), seeds=EXPECTED_SEEDS)\n"
        'telemetry = Telemetry.load(Path("telemetry/demo_short_fill.yaml"))\n'
        "report, observed = diff(compiled, sim, telemetry, None)\n"
        "report"
    ),
    md(
        "## Where to go from here\n\n"
        "The [User Guide](../docs/USER_GUIDE.md) covers every command, [WORLD_MODEL.md](../docs/WORLD_MODEL.md) every field "
        "of the world files, and `ztra init` scaffolds a project of your own. The store, the runtime and the MCP server "
        "(so an agent can drive this loop) are described in the other documents under `docs/`."
    ),
]


def main() -> None:
    nb = nbformat.v4.new_notebook(cells=CELLS, metadata={"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}, "language_info": {"name": "python"}})
    NotebookClient(nb, resources={"metadata": {"path": "examples"}}).execute()
    out = Path("examples/walkthrough.ipynb")
    nbformat.write(nb, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
