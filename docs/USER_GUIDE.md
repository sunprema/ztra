# User Guide: the ztra commands

Everything lives under one command, `ztra`, with subcommands grouped below by the stage of work they belong to. A few conventions hold everywhere:

- **Everything prints JSON.** The output is meant to be equally readable by you and by an agent driving the tool.
- **Exit codes mean something.** `0` is success. `1` means ztra understood the input but refused — the JSON contains an `error` object with a stable `code`, a plain-language `message`, and a `hint` about what to change. `2` means an input file could not be loaded at all.
- **Paths, not magic.** Commands take a world directory and a protocol file explicitly; only the `store` and `run` commands assume a default location (`./.ztra`).

The typical journey runs top to bottom through this guide: scaffold a project, describe your lab, check a protocol, predict what it should do, record it in history, run it, and compare.

---

## Starting a project

Like `git init` or `cargo new`, this is how a new experiment begins.

| Command | What it does |
|---|---|
| `ztra init [dir]` | Creates a small working project: a commented starter world (`world/*.yaml`) and a first protocol that already compiles. Refuses to overwrite existing files. |

The output ends with a `next` list — the commands to run once you've edited the files to match your bench.

## Describing your lab

The `world/` directory is three YAML files: `Inventory.yaml` (what exists), `Deck.yaml` (where it sits), `Hardware.yaml` (what can act and observe). [WORLD_MODEL.md](WORLD_MODEL.md) documents every field. These commands read a world directory and tell you about it.

| Command | What it does |
|---|---|
| `ztra world validate <dir>` | Checks that the description makes sense as a lab: no dangling references, no wells off the grid, no two vials in one tube position, a trash slot exists, and so on. Each issue says where it is and how to fix it. Exit 1 if there are errors. |
| `ztra world summary <dir>` | A compact JSON overview of the lab in one object: robot, pipettes, vials and their volumes, filled wells, tips left, sensors, and any validation problems. Good for a quick "what am I working with?" without reading three YAML files. |
| `ztra world dump <dir>` | The whole world as canonical JSON, keys sorted. |
| `ztra world hash <dir>` | The SHA-256 of that canonical form — the fingerprint a snapshot is stored under. Two worlds with the same hash are the same world. |

Note that loading is strict everywhere: a misspelled or invented field in any YAML file is rejected on load (exit 2), before validation even starts.

## Checking a protocol

A protocol is a YAML recipe (see [PROTOCOL.md](PROTOCOL.md)). Before anything moves, these commands tell you whether it can work — against the *current* state of your world.

| Command | What it does |
|---|---|
| `ztra preflight <world> <protocol>` | The shopping-list check, all at once: how much of each reagent, how many tips, and how much well capacity the whole protocol needs, next to what the lab actually has. Exit 1 if anything falls short. Run this first — it saves compile round trips. |
| `ztra compile <world> <protocol>` | The full step-by-step check. Walks the recipe against a copy of the world, tracking every microliter, and either produces the checked program with its predicted outcome(s) and costs, or a structured error naming the exact step, resource, expected vs actual, and a hint. Exit 1 on error. |
| `ztra lower <world> <protocol>` | Translates the checked program into concrete robot steps — pick up tip, aspirate, dispense, drop tip — and generates the vendor files (Opentrons Python) that would actually run. `--out <dir>` writes them to disk. |

Two flags worth knowing on `compile`: `--no-worlds` trims the large predicted-world snapshots from the output (hashes and costs stay), and `--budget` is described in its own section below.

If a protocol branches on a sensor reading, the compiler checks *every* path and the output contains one predicted outcome per path. That's why several commands take an `--outcome N` to pick one.

## Predicting the outcome

| Command | What it does |
|---|---|
| `ztra simulate <world> <protocol>` | Runs the protocol many times with realistic noise and reports what each sensor should read at every checkpoint, with the spread across runs. This is the prediction the real run is later compared against. |

By default the noise comes from each pipette's `accuracy` spec in `Hardware.yaml`. You can shape it: `--seeds N` (how many runs), `--drift` and `--jitter` (extra systematic and per-dispense error), `--failure-rate` (chance a transfer silently fails), and `--ideal-pipettes` to switch the accuracy spec off entirely.

## Keeping history

The store is ztra's version history — git-like vocabulary, but purpose-built. Branches hold *intents* (plans with predicted outcomes); only `main` records what actually happened. All store commands take `--repo <dir>` and default to `./.ztra`.

| Command | What it does |
|---|---|
| `ztra store init <world>` | Creates the store with your world as the root of `main`. Done once, when the world matches your bench. |
| `ztra store branch <name>` | Starts a branch for a hypothesis (`--from main` by default). |
| `ztra store commit <branch> <protocol>` | Compiles and lowers the protocol against the branch's current world and records the intent — steps, robot program, predicted outcomes — on the branch. `-m` adds a message. |
| `ztra store log [branch]` | The history of a branch, plus the list of all branches. |
| `ztra store show <hash>` | One commit in full. |
| `ztra store checkout <branch> <out_dir>` | Writes the branch's current world out as YAML files. |
| `ztra store files <intent_hash>` | The generated vendor files for an intent, printed or written with `--out <dir>` — this is what a person takes to the robot. |
| `ztra store execute <branch>` | Records that the branch's intent was carried out. Given `--telemetry <yaml>` (the sensor readings from the run), it diffs them against the prediction and appends an observation commit to `main` with the *observed* world — never the predicted one. |
| `ztra store rebase <branch>` | Replays a branch whose base is stale onto the new `main` — a recompile against current reality. If someone used up a reagent in the meantime, that surfaces here as an ordinary compile error. |
| `ztra store verify` | Recomputes the whole hash chain and reports any tampering or corruption. |

Two rules the store enforces, because reality demands them: a branch can only be executed if it starts from the current `main` (otherwise rebase first), and there is no merge — the physical world has one history.

## Running and comparing

| Command | What it does |
|---|---|
| `ztra run <branch> --yes` | Executes the branch's committed intent on the built-in **fake robot** and records the result in the store. Without `--yes` nothing runs — the approval gate is deliberate. `--fault SEG:OP:clog` injects failures (a clogged tip, an open door) to see how a run degrades and what the report says. |
| `ztra diff <world> <protocol> <telemetry.yaml>` | The standalone comparison: given sensor readings from a run, reports per checkpoint whether reality matched the prediction — `verified`, `deviated`, or `unobserved` — and whether a deviation looks systematic (everything a little off: calibration) or localized (one spot wrong: something failed). `--out <dir>` also writes ztra's best estimate of the observed world. |

Since no hardware is connected yet, `ztra run` is the whole loop end to end: it drives the fake driver, collects simulated sensor readings, diffs them, and lands the observation on `main`. With a real robot, the flow is instead `store files` → run them on the machine → `store execute --telemetry`.

## Automatic checkpoints: the `--budget` flag

You only know what you measured. Rather than sprinkling `observe` steps through a protocol by hand, give `compile`, `lower`, `simulate`, `diff`, or `store commit` an observation budget:

```bash
ztra compile world protocol.yaml --budget "sensor=scale_1,every=3"
```

This inserts a reading on the named sensor after every 3 transfers (and one at the end — add `end=false` to skip it). More checkpoints make a run slower but let the diff say *where* it went wrong, not just that it did.

## For agents: the MCP server

`ztra-mcp` exposes all of the above as tools over MCP (stdio), so an agent like Claude can drive the whole loop — including the structured refusals, which are written to be recovered from. The repo's `.mcp.json` shows the wiring; [MCP.md](MCP.md) lists the tools.
