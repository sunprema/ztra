"""World-model schema: the example world is valid, canonical form is stable, and
each semantic rule fires on a targeted mutation."""

import json

import pytest

from tests.conftest import EXAMPLES
from ztra.world import Issue, LoadError, Severity, World, validate
from ztra.world.coords import WellCoord
from ztra.world.deck import Link, Slot
from ztra.world.hardware import RobotModel
from ztra.world.inventory import Liquid


def codes(issues: list[Issue]) -> list[str]:
    return [i.code for i in issues]


def errors(w: World) -> list[Issue]:
    return [i for i in validate(w) if i.severity is Severity.error]


def test_coords() -> None:
    for s, r, c in [("A1", 0, 0), ("H12", 7, 11), ("P24", 15, 23)]:
        w = WellCoord.parse(s)
        assert w is not None and (w.row, w.col) == (r, c) and w.name == s
    for s in ["", "a1", "A0", "A01", "AA1", "1A", "A", "A 1", "A1.5"]:
        assert WellCoord.parse(s) is None, s
    assert WellCoord.parse("H12").within(8, 12)  # type: ignore[union-attr]
    assert not WellCoord.parse("I1").within(8, 12)  # type: ignore[union-attr]
    assert not WellCoord.parse("A13").within(8, 12)  # type: ignore[union-attr]


def test_example_worlds_are_clean(world: World, world_flex: World) -> None:
    assert validate(world) == []
    assert validate(world_flex) == []


def test_canonical_form_is_deterministic_and_round_trips(world: World) -> None:
    again = World.load(EXAMPLES / "world")
    assert world.hash() == again.hash()
    back = World.model_validate(json.loads(world.canonical_json()))
    assert back == world and back.hash() == world.hash()


def test_unknown_field_is_a_load_error() -> None:
    inv = (EXAMPLES / "world/Inventory.yaml").read_text().replace("volume_ul: 1000", "volume_ul: 1000\n    colour: blue")
    deck = (EXAMPLES / "world/Deck.yaml").read_text()
    hw = (EXAMPLES / "world/Hardware.yaml").read_text()
    with pytest.raises(LoadError) as e:
        World.from_yaml(inv, deck, hw)
    assert "Inventory.yaml" in str(e.value) and "colour" in str(e.value)


def test_unknown_reagent(world: World) -> None:
    world.inventory.vials["V_water"].reagent = "unobtainium"
    assert codes(errors(world)) == ["W_REAGENT_UNKNOWN"]


def test_well_overflow_and_bad_well_name(world: World) -> None:
    p = world.inventory.plates["P1"]
    p.wells["B2"] = [Liquid(reagent="water", volume_ul=400)]
    p.wells["Z9"] = []
    e = codes(errors(world))
    assert "W_WELL_OVERFLOW" in e and "W_WELL_INVALID" in e


def test_hazard_mix_in_recorded_state_is_a_warning(world: World) -> None:
    world.inventory.plates["P1"].wells["C1"] = [Liquid(reagent="hcl_1m", volume_ul=10), Liquid(reagent="naoh_1m", volume_ul=10)]
    issues = validate(world)
    assert codes(issues) == ["W_HAZARD_MIX"] and issues[0].severity is Severity.warning


def test_consumed_vial_invariants(world: World) -> None:
    world.inventory.vials["V_hcl"].consumed = True
    assert codes(errors(world)) == ["W_CONSUMED_MISMATCH"]
    world.inventory.vials["V_hcl"].consumed = False
    world.inventory.vials["V_hcl"].volume_ul = 0
    assert codes(validate(world)) == ["W_EMPTY_NOT_CONSUMED"]


def test_plate_must_be_96_well(world: World) -> None:
    world.hardware.labware["corning_96_wellplate_360ul_flat"].rows = 16
    assert "W_PLATE_NOT_96" in codes(errors(world))


def test_slot_names_follow_robot_model(world: World) -> None:
    world.deck.slots["A1"] = Slot()
    e = codes(errors(world))
    assert "W_SLOT_INVALID" in e and "W_SLOT_CONTENT" in e

    w = World.load(EXAMPLES / "world")
    w.hardware.robot.model = RobotModel.flex
    e2 = errors(w)
    assert sum(1 for i in e2 if i.code == "W_SLOT_INVALID") == 4
    assert "W_TRASH_SLOT" not in codes(e2)
    assert "W_PIPETTE_ROBOT_MISMATCH" in codes(e2), "an OT-2 pipette on a Flex"


def test_trash_rules(world: World) -> None:
    del world.deck.slots["12"]
    assert "W_TRASH_MISSING" in codes(errors(world))
    world.deck.slots["11"] = Slot(trash=True)
    assert "W_TRASH_SLOT" in codes(errors(world))


def test_entity_placed_twice_or_not_at_all(world: World) -> None:
    world.deck.slots["4"] = Slot(entity="P1")
    assert "W_ENTITY_DUPLICATE_SLOT" in codes(errors(world))
    w = World.load(EXAMPLES / "world")
    del w.deck.slots["3"]
    assert codes(validate(w)) == ["W_ENTITY_NOT_ON_DECK"]


def test_linker_rules(world: World) -> None:
    world.deck.linker["V_naoh"] = Link(rack="TR1", well="A1")  # collides with V_water
    assert "W_LINK_COLLISION" in codes(errors(world))
    w = World.load(EXAMPLES / "world")
    w.deck.linker["V_enzyme"] = Link(rack="TR1", well="E1")  # 4x6 rack
    assert "W_LINK_WELL_INVALID" in codes(errors(w))
    w = World.load(EXAMPLES / "world")
    del w.deck.linker["V_enzyme"]
    assert codes(validate(w)) == ["W_LINK_MISSING"]
    w.deck.linker["V_ghost"] = Link(rack="TR1", well="C1")
    assert "W_LINK_TARGET_UNKNOWN" in codes(errors(w))


def test_tip_rack_rules(world: World) -> None:
    world.deck.tip_racks["TIPS1"].used.append("A1")
    assert "W_TIP_USED_DUP" in codes(errors(world))
    w = World.load(EXAMPLES / "world")
    w.deck.tip_racks["TIPS1"].used.append("Q1")
    assert "W_TIP_USED_INVALID" in codes(errors(w))


def test_sensor_rules(world: World) -> None:
    world.hardware.sensors["scale_1"].observes.entity = "P9"
    assert "W_SENSOR_TARGET_UNKNOWN" in codes(errors(world))
    w = World.load(EXAMPLES / "world")
    w.hardware.sensors["camera_1"].observes.columns = [13]
    assert "W_SENSOR_COLUMN_INVALID" in codes(errors(w))
    w.hardware.sensors["camera_1"].sigma = 0
    assert "W_SENSOR_SIGMA" in codes(errors(w))


def test_pipette_rules(world: World) -> None:
    world.hardware.pipettes[0].min_ul = 400
    assert "W_PIPETTE_RANGE" in codes(errors(world))
    w = World.load(EXAMPLES / "world")
    w.hardware.pipettes.append(w.hardware.pipettes[0].model_copy())
    assert "W_PIPETTE_MOUNT_DUP" in codes(errors(w))


def test_vendor_knowledge_from_the_opentrons_docs(world: World, world_flex: World) -> None:
    world.hardware.pipettes[0].name = "p300_single_gen3"
    assert "W_PIPETTE_UNKNOWN_NAME" in codes(validate(world))
    w = World.load(EXAMPLES / "world")
    w.hardware.pipettes[0].max_ul = 250
    assert "W_PIPETTE_RANGE_MISMATCH" in codes(validate(w))
    w = World.load(EXAMPLES / "world")
    w.hardware.robot.api_level = "2.29"  # OT-2 stops at 2.28
    assert "W_API_LEVEL" in codes(errors(w))
    world_flex.hardware.robot.api_level = "2.15"  # no load_trash_bin before 2.16
    assert "W_API_LEVEL" in codes(errors(world_flex))
    wf = World.load(EXAMPLES / "world_flex")
    wf.hardware.pipettes[0].name = "flex_1channel_50"
    wf.hardware.pipettes[0].min_ul, wf.hardware.pipettes[0].max_ul = 1, 50
    assert "W_PIPETTE_TIP_TOO_BIG" in codes(validate(wf)), "1000 uL tips on a 50 uL Flex pipette"
