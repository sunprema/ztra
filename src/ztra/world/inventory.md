# src/ztra/world/inventory.py

The schema for `Inventory.yaml` (reagents, vials, plates) plus the mixture math that describes what a
well actually contains once liquids have been combined.

`incompatible()` encodes the two hazard pairs the compiler must never let meet in one vessel
(acid/base, oxidizer/flammable) — this is `E_HAZARD` in the compiler and `W_HAZARD_MIX` in
`validate.py`; both read this same table so the rule can't drift between "should this be allowed" and
"is what's already recorded a problem".

The mixture model (resolved 2026-08-26, see ARCHITECTURE.md §8) is: liquids are volume-additive, a
well is homogeneous (every aspiration draws the same proportions of everything in it), and diluting a
labelled stock scales its declared concentration by volume fraction — nothing reacts, and hazards that
must not meet are refused earlier, by the compiler, so `composition()` never has to model a reaction.

`parse_concentration` reads free-text stock labels like `"10 U/uL"` or `"0.9%"` into a `Concentration`
(value + unit, unit carried through unchanged — no unit conversion happens anywhere in this system).
`composition()` uses that to report each component's fraction, dilution ratio, and diluted
concentration, largest share first; `describe_mixture()` turns that into the one-line summary used in
`world/summary.py` and diagnostics, e.g. `"water 90% + enzyme_x 10% (1 U/uL, 1:10)"`.
