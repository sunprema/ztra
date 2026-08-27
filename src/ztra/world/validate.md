# src/ztra/world/validate.py

Second-pass validation for a loaded `World` — everything pydantic's schema can't express, because it
spans multiple files or needs cross-referencing: dangling entity references, wells off their labware's
grid, a deck with no trash slot (or two), two vials claiming the same tube position, a Flex world
with an OT-2-only module, and so on.

The key design choice is that `Issue` uses the *same shape* as a compiler error (`code`, `path`,
`message`, `hint`) — see the `E_*` codes in ARCHITECTURE.md §4.3 vs. the `W_*` codes here — so an
agent that already knows how to act on a `CompileError` doesn't need a second protocol for "something
is wrong with the world I loaded." Issues are split into `error` (the world is internally
inconsistent — e.g. a consumed vial that still shows volume) vs. `warning` (plausible but worth a
second look — e.g. a pipette's declared range doesn't match its vendor-documented range).

Checks are grouped into four passes — `_versions`, `_hardware`, `_inventory`, `_deck` — run in that
order and merged into one deduplicated, sorted list. The `_deck` pass carries most of the
cross-referencing weight since Deck.yaml is where entities, slots, tube racks, tip racks, modules, and
the vial linker all have to agree with each other and with Inventory/Hardware: duplicate entity ids
across those four namespaces, exactly one trash slot (fixed to slot 12 on OT-2), no two entities in
one slot, and every vial either linked to a real rack position or flagged `W_LINK_MISSING` (a vial
without a deck address can never be reached by lowering).

`_Ctx.labware_of` is the one helper worth knowing about: since a "slot" can point at a plate, a tube
rack, or a tip rack, most checks need to resolve "what labware is at this entity" without caring which
of the three dicts it lives in — `labware_of` centralizes that lookup instead of every check
re-branching on entity kind.
