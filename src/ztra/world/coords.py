"""Well names like A1 or H12. Rows are letters from the top, columns are numbers from the left."""

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class WellCoord:
    row: int  # 0-based, A = 0
    col: int  # 0-based, 1 = 0

    @staticmethod
    def parse(name: str) -> "WellCoord | None":
        """Reads "A1", "H12", "P24". Rejects anything else: lowercase, leading zeros, two letters."""
        if len(name) < 2 or not ("A" <= name[0] <= "Z"):
            return None
        rest = name[1:]
        if not rest.isdigit() or rest.startswith("0"):
            return None
        col = int(rest)
        if col == 0 or col > 255:
            return None
        return WellCoord(ord(name[0]) - ord("A"), col - 1)

    @property
    def name(self) -> str:
        return f"{chr(ord('A') + self.row)}{self.col + 1}"

    def within(self, rows: int, cols: int) -> bool:
        """Does this well exist on a grid of this size?"""
        return self.row < rows and self.col < cols


def expand_wells(items: list[str]) -> list[str] | None:
    """Turn a list like ["A2..E2", "H2"] into well names. A range runs down one column
    (A2..E2) or along one row (A2..A5), inclusive. None if any item is malformed."""
    out: list[str] = []
    for item in items:
        if ".." not in item:
            if WellCoord.parse(item) is None:
                return None
            out.append(item)
            continue
        first, _, last = item.partition("..")
        a, b = WellCoord.parse(first), WellCoord.parse(last)
        if a is None or b is None:
            return None
        if a.col == b.col and a.row <= b.row:
            out += [WellCoord(r, a.col).name for r in range(a.row, b.row + 1)]
        elif a.row == b.row and a.col <= b.col:
            out += [WellCoord(a.row, c).name for c in range(a.col, b.col + 1)]
        else:
            return None
    return out
