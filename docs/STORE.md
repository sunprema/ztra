# The Store (v1)

An append-only, hash-chained history of what we meant to do (intents) and what actually happened
(observations), with branches for hypotheses. This is the component behind the "Vertical Git" idea; it borrows
git's words but not its merge.

```
ztra store init <world_dir>                    # .ztra/ here, root of main = that world
ztra store branch <name> [--from main]
ztra store commit <branch> <protocol> [-m msg] # compile + lower against the head; record an intent
ztra store log [branch]     ztra store show <hash>     ztra store verify
ztra store checkout <branch> <out_dir>         # the head world as Inventory/Deck/Hardware.yaml
ztra store files <intent_hash> [--out <dir>]   # the vendor files for an intent
ztra store execute <branch> [--observed <world_dir>] [--outcome N] [--telemetry <json>]
ztra store rebase <branch>
```
All take `--repo <dir>` (default `./.ztra`). Output is JSON; refusals are `{ok: false, error: {code, message, hint}}`.

## Commits

| kind | holds | where |
|---|---|---|
| `root` | the starting world | first commit of `main` |
| `intent` | the protocol document, the world it was compiled against, the predicted **outcome(s)** (branch conditions + world hash), and the lowered program — exactly what would be sent | any branch |
| `observation` | which intent ran, which outcome happened, the **observed** world, the telemetry and the world-diff report | **only `main`** |

Every object is a JSON file named by the SHA-256 of its canonical form: `.ztra/objects/<hash>.json` for commits
and world snapshots, `.ztra/refs/<branch>` for heads. A commit's hash covers its parent, so editing anything
earlier breaks every hash after it; `verify` recomputes them all.

## Rules

- **Only `main` is real.** A branch is a plan. Resources consumed on a branch are consumed only in that branch's
  prediction.
- **Execute is a fast-forward.** `execute <branch>` requires the branch to sit on top of `main`'s head. It moves
  `main` to the branch head and appends an observation. With `--telemetry`, the diff engine decides which outcome
  happened and estimates the observed world from the readings ([SIMULATION.md](SIMULATION.md)); with `--observed` you
  supply it; with neither, the chosen predicted outcome stands in and the commit says so. The branch is moved along too.
- **Rebase is recompile.** If `main` moved, `rebase <branch>` replays the branch's intents (their protocol
  documents) on top of the new `main`. A protocol that no longer fits reality fails with an ordinary
  `CompileError` and the branch is left untouched. **There is no merge.**
- **You cannot plan past an unresolved reading.** An intent whose protocol branches on an observation has
  several outcomes. Until it is executed (and `--outcome N` says which happened), nothing can be committed on
  top of it (`S_UNRESOLVED`).

## Error codes

`S_EXISTS`, `S_NOT_A_STORE`, `S_NO_BRANCH`, `S_BRANCH_EXISTS`, `S_BAD_NAME`, `S_UNRESOLVED`, `S_NOTHING_TO_EXECUTE`,
`S_NOT_FAST_FORWARD`, `S_OUTCOME_REQUIRED`, `S_BAD_OUTCOME`, `S_MAIN`, `S_MISSING_OBJECT`, `S_NOT_A_COMMIT`,
`S_NOT_A_WORLD`, `S_NOT_AN_INTENT`. Compile errors from `commit`/`rebase` come through unchanged (`E_*`).

## Not in v1

- No garbage collection of unreachable objects, no locking for concurrent writers.
- The runtime (step 7) will call `execute` with real telemetry; today a person does.
