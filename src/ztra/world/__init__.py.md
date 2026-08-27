# src/ztra/world/\_\_init\_\_.py

Ties the three world-model files (Inventory, Deck, Hardware) into one `World` object and gives it
the operations everything downstream needs: load from disk or from strings, dump back to a dict,
and hash.

The hash is the load-bearing bit. `canonical_json()` serializes with sorted keys and no whitespace
specifically so that `World.hash()` is stable across processes and platforms — this is the id a
world snapshot is stored under in the Store (see `ztra.store`), so any instability here would break
content addressing.

`World.clone()` is a deep copy, used whenever the compiler or simulator needs to try a step against
a private copy of the world without mutating the original — this is what makes the compiler's
"abstract interpretation" approach safe to run speculatively down multiple branches.

`_repr_html_` is a Jupyter affordance: when a `World` is the last expression in a notebook cell, it
renders as a picture of the bench (via `ztra.viz`) instead of a dump of nested pydantic fields.

Re-exports `Issue`, `Severity`, `validate` from `ztra.world.validate` so callers can do
`from ztra.world import validate` without knowing the world model is split across five files.
