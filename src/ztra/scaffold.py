"""`ztra init`: start a new project. Writes a small, valid world (three commented
YAML files) and a first protocol that compiles against it, so a new user edits
working files instead of starting from a blank page."""

from __future__ import annotations

from pathlib import Path

INVENTORY = '''\
# Inventory.yaml — what exists in the lab: your liquids, the vials that hold
# them, and plates with whatever is already in their wells.
#
# After editing, check your work:   ztra world validate world
# Field-by-field reference:         docs/WORLD_MODEL.md
version: 1

reagents:
  # One entry per liquid. The hazard class is how the compiler refuses
  # dangerous mixes (acid with base, oxidizer with flammable).
  # Classes: inert | acid | base | oxidizer | flammable | toxic | biohazard
  water:
    hazard: inert
  sample:
    hazard: inert
    concentration: "10 mg/mL"   # free text, for your own records

vials:
  # Vials are tracked to the microliter: a protocol that would take more than
  # a vial holds is refused before the robot moves. Ids are yours to choose.
  V_water:
    reagent: water
    volume_ul: 1000
  V_sample:
    reagent: sample
    volume_ul: 200
    state: frozen               # frozen vials must be thawed before use

plates:
  # List only wells that already contain something; unlisted wells are empty.
  P1:
    labware: corning_96_wellplate_360ul_flat   # must be in Hardware.yaml's catalog
    wells: {}
  # Reservoirs (troughs) live here too, addressed like plates: { plate: WASTE, well: A1 }.
  # A waste reservoir only receives liquid; the compiler refuses to draw from it.
  WASTE:
    labware: nest_1_reservoir_195ml
    waste: true
'''

DECK = '''\
# Deck.yaml — where everything sits on the robot's deck, plus the "linker":
# the table that gives each vial a physical address the robot can reach.
#
# Slot names depend on the robot model in Hardware.yaml:
#   OT-2: "1".."12", trash fixed in slot 12
#   Flex: "A1".."D4", exactly one trash slot, anywhere
version: 1

slots:
  "1":  { entity: P1 }      # the plate from Inventory.yaml
  "2":  { entity: TR1 }     # the tube rack holding the vials
  "3":  { entity: TIPS1 }   # pipette tips
  "4":  { entity: WASTE }   # liquid waste (a reservoir)
  "12": { trash: true }     # where used tips go

tube_racks:
  TR1:
    labware: opentrons_24_tuberack_nest_1.5ml_snapcap

tip_racks:
  # Tips are tracked like reagents: a protocol that would run out of fresh
  # tips fails at compile time. List positions already used under `used`.
  TIPS1:
    labware: opentrons_96_tiprack_300ul
    used: []

linker:
  # Vial id -> tube-rack position. Every vial needs an entry here, or the
  # robot has no way to find it.
  V_water:  { rack: TR1, well: A1 }
  V_sample: { rack: TR1, well: A2 }
'''

HARDWARE = '''\
# Hardware.yaml — the robot, its pipettes, the labware catalog (well sizes and
# heights, used for overflow and clearance checks), and your sensors.
version: 1

robot:
  vendor: opentrons
  model: ot2            # or: flex
  api_level: "2.16"

pipettes:
  - name: p300_single_gen2
    mount: right
    channels: 1
    min_ul: 20          # transfers below 20 uL are a compile error;
    max_ul: 300         # transfers above 300 uL are split into several
    tip_labware: [opentrons_96_tiprack_300ul]
    # How accurate the pipette really is. The simulator folds this into every
    # expected reading, so a normally imprecise run is not flagged as a failure.
    accuracy: { systematic_pct: 2.0, random_pct: 1.0, random_ul: 0.5 }

labware:
  # Everything on the deck needs an entry here.
  corning_96_wellplate_360ul_flat:
    kind: plate
    rows: 8
    cols: 12
    well_max_ul: 360
    height_mm: 14.2
    well_depth_mm: 10.67
    well_diameter_mm: 6.86
  opentrons_24_tuberack_nest_1.5ml_snapcap:
    kind: tube_rack
    rows: 4
    cols: 6
    well_max_ul: 1500
    height_mm: 43.0
    well_depth_mm: 37.9
    well_diameter_mm: 10.2
  opentrons_96_tiprack_300ul:
    kind: tip_rack
    rows: 8
    cols: 12
    tip_volume_ul: 300
    height_mm: 64.7
  nest_1_reservoir_195ml:   # a trough; reservoirs may have any grid (this one is 1x1)
    kind: reservoir
    rows: 1
    cols: 1
    well_max_ul: 195000
    height_mm: 31.4
    well_depth_mm: 25.0
    well_diameter_mm: 71.2

sensors:
  # What you can measure, and how noisy it is (sigma). The report after a run
  # only trusts what a sensor actually covered; the rest is UNOBSERVED.
  scale_1:
    kind: plate_mass
    observes: { entity: P1 }
    sigma: 0.5          # placeholder — measure your own scale and update
    unit: mg
    read_time_s: 5

safe_envelope:
  temperature_c: { min: 4, max: 40 }
  max_flow_rate_ul_s: 300
'''

PROTOCOL = '''\
# A first protocol: thaw the sample, dilute it 1:10 into three wells, and
# weigh the plate at the end. `$w` stands for the current well of the loop.
#
# Check it:                ztra compile world protocols/first_protocol.yaml
# Enough stock for it?     ztra preflight world protocols/first_protocol.yaml
# Predict the readings:    ztra simulate world protocols/first_protocol.yaml
version: 1
name: first_protocol
steps:
  - op: thaw
    vial: V_sample

  - op: for_wells
    wells: [A1..C1]
    as: w
    body:
      - { op: transfer, from: { vial: V_water },  to: { plate: P1, well: $w }, volume_ul: 180 }
      - { op: transfer, from: { vial: V_sample }, to: { plate: P1, well: $w }, volume_ul: 20 }
      - { op: mix, at: { plate: P1, well: $w }, volume_ul: 100, repetitions: 3 }

  - op: observe
    sensor: scale_1
    label: final_mass
'''

FILES: dict[str, str] = {
    "world/Inventory.yaml": INVENTORY,
    "world/Deck.yaml": DECK,
    "world/Hardware.yaml": HARDWARE,
    "protocols/first_protocol.yaml": PROTOCOL,
}

NEXT_STEPS = [
    "edit world/*.yaml to match your bench (docs/WORLD_MODEL.md explains every field)",
    "ztra world validate world",
    "ztra compile world protocols/first_protocol.yaml",
    "ztra store init world",
]


class ScaffoldError(Exception):
    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        return {"code": "I_EXISTS", "message": self.message, "hint": self.hint}


def scaffold(root: Path) -> list[str]:
    """Write the starter files under root; refuse to touch anything that exists."""
    existing = [rel for rel in FILES if (root / rel).exists()]
    if existing:
        raise ScaffoldError(
            f"already scaffolded: {', '.join(sorted(existing))} exist",
            "run init in an empty directory, or delete the files you want regenerated",
        )
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return list(FILES)
