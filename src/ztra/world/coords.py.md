# src/ztra/world/coords.py

Turns well names ("A1", "H12") into row/column pairs and back, and expands the shorthand ranges
protocols are allowed to write ("A2..E2" for a column, "A2..A5" for a row).

`WellCoord.parse` is deliberately strict — no lowercase, no leading zeros, one letter only — because
this is the boundary where free-text strings from YAML become something the rest of the system can
trust; every other module that needs to check "is this well on the plate" goes through
`WellCoord.within` rather than re-deriving row/col math itself.

`expand_wells` only accepts a range that runs straight down one column or straight along one row
(mixing rows and columns, or a decreasing range, returns `None` rather than guessing an ordering) —
callers treat `None` as a validation failure to surface to the agent, not an exception to catch.
