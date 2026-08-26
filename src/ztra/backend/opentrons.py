"""Opentrons backend: PIR-L segment → Python Protocol API v2 file.
Works for OT-2 (opentrons < 9) and Flex (opentrons >= 9); only deck names,
tip racks and the trash differ, and those all come from the world model.

The generated file also declares the starting liquids (define_liquid / load_liquid,
API 2.14+) so the Opentrons app shows the initial deck state."""

from __future__ import annotations

import re

from ztra.lower import Aspirate, Decide, Dispense, DropTip, MixOp, ObserveL, Pause, PickUpTip, Program, Segment
from ztra.world import World
from ztra.world.hardware import RobotModel, fmt
from ztra.world.inventory import total_ul


def emit_program(world: World, program: Program) -> list[tuple[str, str]]:
    """Python for every segment, in segment order. File names are segment_N.py."""
    return [(f"segment_{i}.py", emit_segment(world, program, i, seg)) for i, seg in enumerate(program.segments)]


def emit_segment(world: World, program: Program, index: int, seg: Segment) -> str:
    hw = world.hardware
    robot = "OT-2" if hw.robot.model is RobotModel.ot2 else "Flex"
    api = hw.robot.api_level or "2.16"
    lines: list[str] = [f"# ztra segment {index} of {len(program.segments)}. Generated; do not edit."]
    if isinstance(seg.next, Decide):
        n = seg.next
        lines.append(f"# ends: runtime decides on '{n.observation}' ({n.condition}) -> segment {n.then} if true, segment {n.otherwise} if false")
    else:
        lines.append("# ends: halt")
    lines += ["", 'metadata = {"protocolName": "ztra segment %d"}' % index, f'requirements = {{"robotType": "{robot}", "apiLevel": "{api}"}}', "", "", "def run(ctx):"]

    # Load everything that sits in a slot.
    for slot, content in sorted(world.deck.slots.items(), key=lambda kv: _slot_order(kv[0])):
        if content.entity is not None:
            lines.append(f'    {_var(content.entity)} = ctx.load_labware("{_labware_name(world, content.entity)}", "{slot}")')
    if hw.robot.model is RobotModel.flex:
        trash = world.deck.trash_slot()
        if trash is not None:
            lines.append(f'    ctx.load_trash_bin("{trash}")')
    for p in hw.pipettes:
        racks = [_var(rid) for rid, r in world.deck.tip_racks.items() if r.labware in p.tip_labware and world.deck.slot_of(rid) is not None]
        lines.append(f'    {_pip(p.name)} = ctx.load_instrument("{p.name}", "{p.mount.value}", tip_racks=[{", ".join(racks)}])')

    liquid_lines, declared = _liquids(world)
    lines += liquid_lines
    lines += _empties(world, seg, declared, api)

    if not seg.ops:
        lines.append('    ctx.comment("nothing to do in this segment")')
    last_origin = None
    for op in seg.ops:
        if op.origin != last_origin:
            iters = f" iteration {op.origin.iterations}" if op.origin.iterations else ""
            lines.append(f"    # protocol step {op.origin.step_path}{iters}")
            last_origin = op.origin
        if isinstance(op, PickUpTip):
            lines.append(f'    {_pip(op.pipette)}.pick_up_tip({_var(op.rack)}["{op.well}"])')
        elif isinstance(op, Aspirate):
            lines.append(f'    {_pip(op.pipette)}.aspirate({fmt(op.volume_ul)}, {_var(op.labware)}["{op.well}"])')
        elif isinstance(op, Dispense):
            lines.append(f'    {_pip(op.pipette)}.dispense({fmt(op.volume_ul)}, {_var(op.labware)}["{op.well}"])')
        elif isinstance(op, MixOp):
            lines.append(f'    {_pip(op.pipette)}.mix({op.repetitions}, {fmt(op.volume_ul)}, {_var(op.labware)}["{op.well}"])')
        elif isinstance(op, DropTip):
            lines.append(f"    {_pip(op.pipette)}.drop_tip()")
        elif isinstance(op, Pause):
            lines.append(f"    ctx.pause({_py_str(op.message)})")
        elif isinstance(op, ObserveL):
            lines.append(f"    ctx.pause({_py_str(f'OBSERVE {op.label}: waiting for {op.sensor}')})")
    return "\n".join(lines) + "\n"


def _liquids(world: World) -> tuple[list[str], set[tuple[str, str]]]:
    """Tell the app what is where at the start: one liquid per reagent, volumes per well.
    Also returns which (entity, well) addresses were declared."""
    used: dict[str, str] = {}
    loads: list[str] = []
    declared: set[tuple[str, str]] = set()
    placed = world.deck.placed()

    def liquid_var(reagent: str) -> str:
        if reagent not in used:
            used[reagent] = f"liq_{_var(reagent)}"
        return used[reagent]

    for pid, plate in world.inventory.plates.items():
        if pid not in placed:
            continue
        for well, contents in plate.wells.items():
            if not contents:
                continue
            # the vendor API takes one liquid per well, so a mixture is named as one
            names = sorted({l.reagent for l in contents})
            label = names[0] if len(names) == 1 else "+".join(names)
            loads.append(f'    {_var(pid)}["{well}"].load_liquid({liquid_var(label)}, {fmt(total_ul(contents))})')
            declared.add((pid, well))
    for vid, link in world.deck.linker.items():
        vial = world.inventory.vials.get(vid)
        if vial is None or vial.volume_ul <= 0 or link.rack not in placed:
            continue
        loads.append(f'    {_var(link.rack)}["{link.well}"].load_liquid({liquid_var(vial.reagent)}, {fmt(vial.volume_ul)})')
        declared.add((link.rack, link.well))
    # description/display_color must be given explicitly before API 2.20
    defs = [f'    {var} = ctx.define_liquid(name="{reagent}", description=None, display_color=None)' for reagent, var in used.items()]
    return defs + loads, declared


def _empties(world: World, seg: Segment, declared: set[tuple[str, str]], api: str) -> list[str]:
    """Declare every well this segment dispenses into as empty (load_empty, API 2.22+),
    so the vendor engine can track liquid in the destinations too."""
    if not _api_at_least(api, (2, 22)):
        return []
    targets: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set(declared)
    for op in seg.ops:
        if isinstance(op, (Dispense, MixOp)) and (op.labware, op.well) not in seen:
            targets.setdefault(op.labware, []).append(op.well)
            seen.add((op.labware, op.well))
    return [f'    {_var(entity)}.load_empty([{", ".join(f"{_var(entity)}[\"{w}\"]" for w in wells)}])' for entity, wells in targets.items()]


def _api_at_least(api: str, minimum: tuple[int, int]) -> bool:
    parts = api.split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return False
    return (int(parts[0]), int(parts[1])) >= minimum


def _labware_name(world: World, entity: str) -> str:
    if entity in world.inventory.plates:
        return world.inventory.plates[entity].labware
    if entity in world.deck.tube_racks:
        return world.deck.tube_racks[entity].labware
    if entity in world.deck.tip_racks:
        return world.deck.tip_racks[entity].labware
    return entity


def _slot_order(name: str) -> tuple[int, str]:
    return (int(name), "") if name.isdigit() else (0, name)


def _var(identifier: str) -> str:
    """Make an id safe as a Python name."""
    v = re.sub(r"[^A-Za-z0-9]", "_", identifier)
    return f"_{v}" if v[:1].isdigit() else v


def _pip(name: str) -> str:
    return f"pip_{_var(name)}"


def _py_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
