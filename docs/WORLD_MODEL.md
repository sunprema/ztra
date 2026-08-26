# World Model Schema (v1)

The World Model is the versioned Source of Truth for the physical environment (FR-1.1). It is three YAML
files in one directory; the pydantic models in `src/ztra/world/` are the schema and the YAML is their
serialized form. Unknown fields are rejected on load. A complete, valid example lives in [`examples/world/`](../examples/world/),
and `ztra init <dir>` scaffolds a commented starter world to edit.

```
ztra world validate <dir>   # semantic checks → JSON issues (exit 1 on errors, 2 on load failure)
ztra world dump <dir>       # canonical JSON (the wire form on the CLI boundary, IF-2.3)
ztra world hash <dir>       # SHA-256 of the canonical form (the id a snapshot is stored under)
```

| File | Concern | Answers |
|---|---|---|
| `Inventory.yaml` | what exists | reagents, vials, plates and their contents |
| `Deck.yaml` | where it is | slots, tube racks, tip racks, linker table |
| `Hardware.yaml` | what can act / observe | robot, pipettes, labware catalog, sensors, safe envelope |

All three carry `version: 1`. Ids (`P1`, `TR1`, `TIPS1`, `V_water`) are global: a plate, tube rack and tip rack
may not share an id.

---

## Inventory.yaml

```yaml
version: 1
reagents:
  <reagent_id>:
    hazard: inert | acid | base | oxidizer | flammable | toxic | biohazard   # CON-3
    concentration: "1 M"        # optional, free text (not interpreted in v0.1)
    msds: "msds/hcl.pdf"        # optional reference
    density_mg_per_ul: 1.0      # optional, default 1.0; used for expected scale readings
vials:
  <vial_id>:
    reagent: <reagent_id>
    volume_ul: 200
    state: thawed | frozen      # optional, default thawed
    freeze_thaw_cycles: 0       # optional, entropy tracking
    consumed: false             # optional; one-way (FR-1.3)
plates:
  <plate_id>:
    labware: <labware_id>       # must be kind: plate in Hardware.labware
    wells:                      # sparse; unlisted wells are empty
      A1:
        - { reagent: <reagent_id>, volume_ul: 50 }
```

Incompatible hazard pairs (rejected by the compiler as `E_HAZARD`; warned about here if already recorded):
`acid × base`, `oxidizer × flammable`.

## Deck.yaml

```yaml
version: 1
slots:                          # slot name depends on robot.model:
  "1":  { entity: P1 }          #   ot2  → "1".."12", trash fixed at "12"
  "2":  { entity: TR1 }         #   flex → "A1".."D4", trash anywhere (exactly one)
  "3":  { entity: TIPS1 }
  "12": { trash: true }
tube_racks:
  <rack_id>:
    labware: <labware_id>       # kind: tube_rack
tip_racks:
  <tiprack_id>:
    labware: <labware_id>       # kind: tip_rack
    used: [A1, B1]              # occupancy; tips are linear (CON-4)
linker:                         # vial → physical address (CON-6)
  <vial_id>: { rack: <rack_id>, well: A1 }
```

Plates are addressed by their slot; vials by the linker. A vial without a linker entry is legal (it may be in
the fridge) but any protocol that touches it fails lowering.

## Hardware.yaml

```yaml
version: 1
robot:
  vendor: opentrons
  model: ot2 | flex
  api_level: "2.16"             # optional
pipettes:
  - name: p300_single_gen2      # vendor instrument name
    mount: left | right         # one pipette per mount
    channels: 1                 # optional, default 1
    min_ul: 20
    max_ul: 300
    tip_labware: [<labware_id>] # tip racks this pipette can use
    accuracy:                   # optional; how far off a healthy pipette may be (vendor spec, or measured)
      systematic_pct: 2.0       #   one bias per run, % of each volume
      random_pct: 1.0           #   scatter per dispense, % of volume
      random_ul: 0.5            #   plus scatter in µL
labware:                        # the catalog; CON-2 comes from well_max_ul
  <labware_id>:
    kind: plate | tube_rack | tip_rack
    rows: 8
    cols: 12
    well_max_ul: 360            # required for plate / tube_rack
    tip_volume_ul: 300          # required for tip_rack
    height_mm: 14.2             # for static clearance checks (FR-2.3)
sensors:                        # the sensor model (CON-5)
  <sensor_id>:
    kind: plate_mass | well_volume | temperature
    observes:
      entity: <plate|rack|tiprack id>
      wells: [A1, B1]           # optional (well_volume)
      columns: [1]              # optional shorthand, 1-based
    sigma: 0.5                  # noise, in `unit`; must be > 0
    unit: mg
    read_time_s: 5              # optional; feeds the verification budget (FR-2.7)
safe_envelope:                  # NFR-3.1
  temperature_c: { min: 4, max: 40 }
  max_flow_rate_ul_s: 300
```

---

## Validation issues

Each issue has `severity`, `code`, `file`, `path`, `message`, `hint` (the FR-2.4 shape). Errors make the
world unusable; warnings describe a legal but probably unintended state.

| Code | Sev | Rule |
|---|---|---|
| `W_VERSION` | E | file `version` is not the supported schema version |
| `W_REAGENT_UNKNOWN` | E | vial or well references an undefined reagent |
| `W_DENSITY` | E | `density_mg_per_ul` ≤ 0 |
| `W_VOLUME_NEGATIVE` | E | a volume < 0 |
| `W_CONSUMED_MISMATCH` | E | `consumed: true` with volume > 0 |
| `W_EMPTY_NOT_CONSUMED` | W | volume 0 but not marked consumed |
| `W_LABWARE_UNKNOWN` | E | plate / rack / tip rack references labware not in the catalog |
| `W_LABWARE_KIND` | E | labware kind does not match its role |
| `W_LABWARE_GRID` / `W_LABWARE_HEIGHT` / `W_LABWARE_CAPACITY` / `W_LABWARE_TIP_VOLUME` | E | malformed labware definition |
| `W_PLATE_NOT_96` | E | plate is not 8×12 (CON-1) |
| `W_WELL_INVALID` | E | well name outside the labware grid |
| `W_WELL_OVERFLOW` | E | recorded contents exceed `well_max_ul` (CON-2) |
| `W_HAZARD_MIX` | W | a well already holds incompatible hazard classes |
| `W_PIPETTE_RANGE` / `W_PIPETTE_MOUNT_DUP` / `W_PIPETTE_TIP_UNKNOWN` / `W_PIPETTE_TIP_KIND` | E | malformed pipette |
| `W_PIPETTE_NO_TIPS` | W | pipette lists no compatible tip labware |
| `W_PIPETTE_ACCURACY` | E/W | negative accuracy values (E); implausibly loose ones (W) |
| `W_PIPETTE_UNKNOWN_NAME` / `W_PIPETTE_RANGE_MISMATCH` / `W_PIPETTE_CHANNELS_MISMATCH` | W | pipette name or range differs from the Opentrons docs |
| `W_PIPETTE_ROBOT_MISMATCH` | E | pipette belongs to the other robot |
| `W_PIPETTE_TIP_TOO_BIG` | W | Flex tips larger than the pipette's capacity |
| `W_API_LEVEL` | E | `robot.api_level` outside the robot's range, or Flex below 2.16 |
| `W_SENSOR_SIGMA` / `W_SENSOR_TARGET_UNKNOWN` / `W_SENSOR_WELL_INVALID` / `W_SENSOR_COLUMN_INVALID` | E | malformed sensor |
| `W_SENSOR_NO_WELLS` | W | `well_volume` sensor observes nothing |
| `W_ENVELOPE_RANGE` | E | safe envelope min ≥ max |
| `W_ENTITY_ID_DUP` | E | same id used for a plate and a rack, etc. |
| `W_SLOT_INVALID` | E | slot name not valid for the robot model |
| `W_SLOT_CONTENT` | E | slot has neither / both of `entity`, `trash` |
| `W_SLOT_ENTITY_UNKNOWN` | E | slot references an undefined entity |
| `W_ENTITY_DUPLICATE_SLOT` | E | entity placed in two slots |
| `W_ENTITY_NOT_ON_DECK` | W | entity defined but not placed |
| `W_TRASH_MISSING` / `W_TRASH_MULTIPLE` / `W_TRASH_SLOT` | E | trash rules (OT-2: exactly one, at slot 12) |
| `W_TIP_USED_INVALID` / `W_TIP_USED_DUP` | E | bad tip occupancy list |
| `W_TIP_LABWARE_UNUSABLE` | W | no pipette can use this tip rack |
| `W_LINK_TARGET_UNKNOWN` / `W_LINK_RACK_UNKNOWN` / `W_LINK_WELL_INVALID` / `W_LINK_COLLISION` | E | malformed linker entry |
| `W_LINK_MISSING` | W | vial has no deck address (CON-6) |

## Not in v1 (deliberately)

- Concentration semantics and mixture chemistry — `concentration` is free text until the mixture model exists (ARCHITECTURE §8).
- Multi-plate sensors, sensor placement geometry.
- Labware geometry beyond `height_mm` (well depth, offsets) — added when static clearance checks are implemented.

## Filling the labware catalog

Catalog keys are Opentrons load names. To add one, run inside a vendor venv and paste the output under `labware:`:

```
<vendor-venv>/bin/python scripts/import_labware.py corning_96_wellplate_360ul_flat opentrons_96_tiprack_300ul
```
