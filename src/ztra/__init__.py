"""ztra core.

- world    — the world model and its validator
- protocol — the protocol AST
- pir      — PIR-H, the compiler's intermediate representation
- compiler — turns a protocol into PIR-H and predicted outcomes
- lower    — turns PIR-H into PIR-L segments with real deck addresses and tips
- backend  — turns PIR-L into vendor code (Opentrons Python)
"""
