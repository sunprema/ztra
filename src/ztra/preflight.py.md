---
path: "src/ztra/preflight.py"
summary: "Preflight: totals a protocol's resource needs across every branch path before anything runs."
source_commit: 265513cb0646a77c6b0f3485c43d77b1117e0f21
desynced: false
---

# preflight.py

The compiler stops at the *first* impossible step and reports just that one failure. Preflight
answers a different, upfront question — "does the lab even have enough of everything for this
whole protocol?" — by walking every branch path to completion and tallying totals, so an agent
sees the full resource picture in one shot instead of fixing errors one at a time.

For each path it tracks: volume drawn per vial (and, summed up, per reagent — since a shortfall
on one vial might be covered by another vial of the same reagent), tips needed per pipette
(crediting tips a `replenish_tips` step would bring in), the peak volume ever reached in each
well (to catch an overflow that happens mid-protocol even if the well ends up empty), and any
vial that's aspirated from while still frozen. Because different branch paths can have
different worst-case demands, the final numbers reported are the max across all paths — the
worst case an agent should plan for.

The `attach()` function is the integration point with the compiler: when a `CompileError` is
one of the resource-related codes (`E_VOLUME`, `E_TIPS`, `E_OVERFLOW`, `E_CONSUMED`, `E_STATE`),
it runs a full preflight and folds the summary into the error response, so the agent sees not
just "this one step is short" but the whole shortfall across the protocol — useful because
fixing the reported step in isolation might just move the failure to the next vial draw.
