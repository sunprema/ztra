"""Hardware.yaml — the robot, its pipettes, the labware catalog, the sensors, and the safe limits."""

from enum import Enum

from pydantic import Field

from ztra.model import Strict


class RobotModel(str, Enum):
    ot2 = "ot2"
    flex = "flex"

    def valid_slot(self, slot: str) -> bool:
        """Is this a real slot name on this robot?"""
        if self is RobotModel.ot2:
            return slot.isdigit() and not slot.startswith("0") and 1 <= int(slot) <= 12
        return len(slot) == 2 and slot[0] in "ABCD" and slot[1] in "1234"

    @property
    def fixed_trash_slot(self) -> str | None:
        """OT-2 has its trash bolted to slot 12. Flex lets you pick."""
        return "12" if self is RobotModel.ot2 else None

    @property
    def api_levels(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Lowest and highest Protocol API level this robot runs (Opentrons docs, 2026)."""
        return ((2, 0), (2, 28)) if self is RobotModel.ot2 else ((2, 15), (2, 29))


# Vendor pipette names with their ranges, from the Opentrons docs. Used for warnings only.
KNOWN_PIPETTES: dict[str, tuple[RobotModel, float, float, int]] = {
    "p20_single_gen2": (RobotModel.ot2, 1, 20, 1),
    "p20_multi_gen2": (RobotModel.ot2, 1, 20, 8),
    "p300_single_gen2": (RobotModel.ot2, 20, 300, 1),
    "p300_multi_gen2": (RobotModel.ot2, 20, 300, 8),
    "p1000_single_gen2": (RobotModel.ot2, 100, 1000, 1),
    "flex_1channel_50": (RobotModel.flex, 1, 50, 1),
    "flex_1channel_1000": (RobotModel.flex, 5, 1000, 1),
    "flex_8channel_50": (RobotModel.flex, 1, 50, 8),
    "flex_8channel_1000": (RobotModel.flex, 5, 1000, 8),
    "flex_96channel_200": (RobotModel.flex, 1, 200, 96),
    "flex_96channel_1000": (RobotModel.flex, 5, 1000, 96),
}


class Mount(str, Enum):
    left = "left"
    right = "right"


class Accuracy(Strict):
    """How far a pipette can be off and still be working normally. Vendors publish these per
    volume; until measured on your robot these defaults are deliberately generous."""

    systematic_pct: float = 2.0  # a whole run can sit this far off, one way, as a percentage of each volume
    random_pct: float = 1.0  # dispense-to-dispense scatter, as a percentage of the volume
    random_ul: float = 0.5  # plus this much scatter in µL regardless of volume


class Pipette(Strict):
    name: str  # vendor name, e.g. p300_single_gen2 or flex_1channel_1000
    mount: Mount
    channels: int = 1
    min_ul: float
    max_ul: float
    tip_labware: list[str] = Field(default_factory=list)  # tip racks (catalog names) this pipette can use
    accuracy: Accuracy = Field(default_factory=Accuracy)


class LabwareKind(str, Enum):
    plate = "plate"
    tube_rack = "tube_rack"
    tip_rack = "tip_rack"


class LabwareDef(Strict):
    """Just enough about a piece of labware to check grids and volumes."""

    kind: LabwareKind
    rows: int
    cols: int
    well_max_ul: float | None = None
    tip_volume_ul: float | None = None
    height_mm: float  # overall height, for clearance checks later


class SensorKind(str, Enum):
    plate_mass = "plate_mass"  # a scale: total mass of one piece of labware
    well_volume = "well_volume"  # a camera or level sensor: volume in specific wells
    temperature = "temperature"


class Observes(Strict):
    entity: str
    wells: list[str] = Field(default_factory=list)
    columns: list[int] = Field(default_factory=list)  # 1-based; shorthand for every well in those columns


class Sensor(Strict):
    """What a sensor can see and how noisy it is. `sigma` is in `unit`."""

    kind: SensorKind
    observes: Observes
    sigma: float
    unit: str
    read_time_s: float = 0.0  # how long one reading takes


class Range(Strict):
    min: float
    max: float


class SafeEnvelope(Strict):
    temperature_c: Range | None = None
    max_flow_rate_ul_s: float | None = None


class Robot(Strict):
    vendor: str
    model: RobotModel
    api_level: str | None = None  # vendor API level the backend should target, e.g. "2.16"


class Hardware(Strict):
    version: int
    robot: Robot
    pipettes: list[Pipette] = Field(default_factory=list)
    labware: dict[str, LabwareDef] = Field(default_factory=dict)
    sensors: dict[str, Sensor] = Field(default_factory=dict)
    safe_envelope: SafeEnvelope = Field(default_factory=SafeEnvelope)

    def pipette_for(self, volume_ul: float, allow_split: bool) -> tuple[Pipette, int] | None:
        """Pick the smallest pipette that fits the volume. If nothing is big enough and
        splitting is allowed, use the largest one over several cycles. None if no pipette can do it."""
        eps = 1e-9
        if not (volume_ul > 0) or volume_ul == float("inf"):
            return None
        by_size = sorted(self.pipettes, key=lambda p: p.max_ul)
        for p in by_size:
            if p.min_ul <= volume_ul + eps and volume_ul <= p.max_ul + eps:
                return p, 1
        if not by_size:
            return None
        largest = by_size[-1]
        if allow_split and volume_ul > largest.max_ul:
            return largest, -(-int(volume_ul * 1e6) // int(largest.max_ul * 1e6))
        return None

    def pipette_ranges(self) -> str:
        """Human-readable list of pipette ranges, for error messages."""
        return ", ".join(sorted(f"{p.name}: {fmt(p.min_ul)}..{fmt(p.max_ul)} uL" for p in self.pipettes))


def fmt(v: float) -> str:
    """Print 50.0 as 50 and 12.5 as 12.5."""
    return str(int(v)) if float(v).is_integer() else str(v)
