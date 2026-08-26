# ztra

**ztra** lets an AI agent run experiments on a lab robot without anyone having to just trust it.

Right now it covers one job: moving small amounts of liquid around 96-well plates with a pipetting robot (an Opentrons). That sounds narrow, but it's the bread and butter of biology labs, and it's exactly the kind of repetitive, precise work you'd want to hand to software.

## The problem

Say you ask an AI to set up an experiment: "dilute this enzyme 1:10 across a column of the plate." The AI can write the robot script easily. The hard part is everything around it:

- Is there actually enough enzyme left in the vial?
- Is it thawed, or still sitting frozen from last week?
- Are there enough fresh pipette tips in the rack?
- Will step 14 try to squeeze 250 µL into a well that only holds 200?
- If two chemicals shouldn't be mixed, will anything stop the robot from mixing them?

A robot script doesn't know any of this. It just moves the arm. If the vial is empty, the robot happily pipettes air, and you find out hours later when the results make no sense. Worse, unlike software, you can't undo a physical mistake — the reagent is gone, the plate is ruined, and sometimes the mistake is genuinely dangerous.

Software engineers solved a version of this problem long ago: we don't let code touch production without version control, compilers that catch errors, tests that run before deployment, and logs that show what actually happened. Labs mostly don't have that. ztra is an attempt to build it.

## What ztra actually does

**It keeps a description of the lab, and treats it as the source of truth.** Plain YAML files describe what's on the bench: which reagents are in which vials, how much is left, what's frozen, where every plate and tip rack sits, and what the robot and its sensors can do. This description is versioned like code — every change is a commit, and history can't be quietly rewritten.

**It checks an experiment before anything moves.** An experiment is written as a small, structured recipe (thaw this, transfer 50 µL from here to there, repeat 8 times...). Before the robot does anything, ztra walks through the whole recipe step by step against its copy of the lab, tracking every drop. If step 11 would run the water vial dry, you get an error that says exactly that — which step, which vial, how much was left — while everything is still just data on a screen. It also checks well capacities, frozen reagents, chemical compatibility, pipette limits, and tip supply.

**It predicts what should happen.** A simulator runs the recipe many times with realistic noise (pipettes aren't perfect) and predicts what the sensors — a scale, a camera — should read at checkpoints during the run.

**Experiments run like a git workflow.** You branch, write a recipe, and commit the *intent*. Executing is only allowed if your branch is based on the lab's current state; if someone else used up a reagent since you planned, you have to re-check against reality first. There is deliberately no merge — the physical world has only one history.

**Afterwards, it compares prediction to reality.** During the run, sensor readings come back and ztra reports, per checkpoint: verified, deviated, or simply not observed. It's honest about the limits — if nothing measured a particular well, the report says "unobserved" rather than pretending everything is fine.

The AI agent sits on top of all this. It can plan, compile, fix its own mistakes (the error messages are written to be understood by both humans and machines), simulate, and propose — but the checks between the agent and the robot are not negotiable, and a human approves before anything physical happens.

## Status

This is version 0.1, and it's early. Everything above works today, but against Opentrons' official simulator and a fake robot driver — no hardware is connected yet. The design is built so that swapping the fake driver for a real one doesn't change anything else.

## Trying it out

You'll need Python 3.12+.

```bash
pip install -e ".[dev]"

# check an experiment against the example lab
ztra compile examples/world examples/protocols/enzyme_dilution.yaml

# see it fail usefully
ztra compile examples/world examples/protocols/bad_loop_drains_vial.yaml
```

`ztra --help` shows the rest: `preflight` (do we have enough stock?), `simulate`, `store` (the version history), `run`, and `diff`. There is also an MCP server (`ztra-mcp`) that exposes the whole loop as tools, so agents like Claude can drive it directly.

## Reading further

The `docs/` folder has the details, starting with [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the overall design and [REQUIREMENTS.md](docs/REQUIREMENTS.md) for what v0.1 is meant to do. [PROTOTYPE_FINDINGS.md](docs/PROTOTYPE_FINDINGS.md) records the experiments that shaped the design — including the mistakes.

---

*If you can't version it, you can't automate it.*
