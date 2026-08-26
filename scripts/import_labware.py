"""Print Hardware.yaml labware entries for Opentrons load names.

Run this inside a vendor venv (it needs opentrons_shared_data), then paste the
output under `labware:` in Hardware.yaml:

    ot-venv/bin/python scripts/import_labware.py corning_96_wellplate_360ul_flat opentrons_96_tiprack_300ul
"""

import sys

from opentrons_shared_data import labware


def main(names: list[str]) -> None:
    for name in names:
        d = labware.load_definition(name, 1)
        rows = len(d["ordering"][0])
        cols = len(d["ordering"])
        a1 = d["wells"]["A1"]
        is_tips = d["parameters"].get("isTiprack", False)
        kind = "tip_rack" if is_tips else ("tube_rack" if "tuberack" in name else "plate")
        print(f"  {name}:")
        print(f"    kind: {kind}")
        print(f"    rows: {rows}")
        print(f"    cols: {cols}")
        if is_tips:
            print(f"    tip_volume_ul: {a1['totalLiquidVolume']}")
        else:
            print(f"    well_max_ul: {a1['totalLiquidVolume']}")
        print(f"    height_mm: {d['dimensions']['zDimension']}")


if __name__ == "__main__":
    main(sys.argv[1:])
