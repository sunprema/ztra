# schedule.py

Inserts `OBSERVE` ops into PIR-H after compilation, according to a `Budget` the agent (or a
policy) chooses. This exists because how finely a run is observed is a trade-off the compiler
itself shouldn't hardcode: more checkpoints mean slower runs but let the diff engine localize a
deviation to a specific step rather than reporting "something went wrong somewhere between the
last two readings."

A `Budget` names a sensor and, optionally, how often to read it (`every` N transfers/mixes) and
whether to add one final reading `at_end` (on by default). `Budget.parse()` reads this from a
flat CLI string like `sensor=scale_1,every=3,end=false` rather than requiring a YAML file for
something this small.

`schedule()` walks the PIR-H (recursing into both arms of any `Branch`, since scheduling has to
apply uniformly regardless of which path a run actually takes) and counts qualifying transfer/
mix ops as it goes, inserting an `ObserveOp` every `every`-th one. Labels are auto-generated as
`<prefix>_1`, `<prefix>_2`, ... so each inserted checkpoint has a unique name the protocol's own
`if_observed` steps (or a later diff report) can reference.
