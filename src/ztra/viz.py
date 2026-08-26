"""Pictures of the world for notebooks: plates, deck, vials and diffs as inline
SVG/HTML with no dependencies, plus a step-by-step replay of a protocol that can
be scrubbed like a film. Evaluating a World (or a diff) in a Jupyter cell shows
these automatically."""

from __future__ import annotations

import html
import itertools
from dataclasses import dataclass

from ztra.diff import Verdict, WorldDiff
from ztra.lower import Aspirate, Delay, Dispense, DropTip, Magnet, MixOp, ObserveL, Pause, PickUpTip, PirL, ReturnTip, lower
from ztra.protocol import Protocol
from ztra.schedule import Budget
from ztra.world import World
from ztra.world.coords import WellCoord
from ztra.world.hardware import LabwareKind
from ztra.world.inventory import ThermalState, describe_mixture, total_ul

PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#B279A2", "#E45756", "#72B7B2", "#EECA3B", "#9D755D"]
VERDICT_COLORS = {Verdict.verified: "#54A24B", Verdict.deviated: "#E45756", Verdict.unobserved: "#C9C9C9"}
FONT = "font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 12px; color: #333"
CELL = 26

_ids = itertools.count()


def reagent_colors(world: World) -> dict[str, str]:
    """A stable color per reagent, assigned in name order."""
    return {name: PALETTE[i % len(PALETTE)] for i, name in enumerate(sorted(world.inventory.reagents))}


def _grid_header(rows: int, cols: int) -> tuple[list[str], int, int] :
    """Row/column labels for a well grid; returns the svg parts and the drawing offsets."""
    ox, oy = 18, 16
    parts = []
    for c in range(cols):
        parts.append(f'<text x="{ox + c * CELL + CELL // 2}" y="11" text-anchor="middle" fill="#999" font-size="9">{c + 1}</text>')
    for r in range(rows):
        parts.append(f'<text x="9" y="{oy + r * CELL + CELL // 2 + 3}" text-anchor="middle" fill="#999" font-size="9">{chr(ord("A") + r)}</text>')
    return parts, ox, oy


def plate_svg(world: World, plate_id: str) -> str:
    """One plate as a well grid; each well's circle grows with its volume and takes
    the color of whatever reagent dominates it. Hover a well for its contents.
    A reservoir is drawn as troughs that fill from the bottom."""
    plate = world.inventory.plates[plate_id]
    d = world.hardware.labware[plate.labware]
    cap = d.well_max_ul or 360.0
    colors = reagent_colors(world)
    if d.kind is LabwareKind.reservoir:
        return _reservoir_svg(world, plate_id, cap, colors)
    parts, ox, oy = _grid_header(d.rows, d.cols)
    rmax = CELL / 2 - 3
    for r in range(d.rows):
        for c in range(d.cols):
            name = WellCoord(r, c).name
            cx, cy = ox + c * CELL + CELL // 2, oy + r * CELL + CELL // 2
            contents = plate.wells.get(name, [])
            vol = total_ul(contents)
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{rmax}" fill="none" stroke="#CCC"><title>{name}: empty</title></circle>')
            if vol > 0:
                dominant = max(contents, key=lambda l: l.volume_ul).reagent
                radius = max(2.5, rmax * min(1.0, (vol / cap) ** 0.5))
                tip = f"{vol:g} uL: " + html.escape(describe_mixture(contents, world.inventory.reagents))
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="{radius:.1f}" fill="{colors.get(dominant, "#888")}"><title>{name}: {tip}</title></circle>')
    w, h = ox + d.cols * CELL + 4, oy + d.rows * CELL + 4
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'


def _reservoir_svg(world: World, plate_id: str, cap: float, colors: dict[str, str]) -> str:
    plate = world.inventory.plates[plate_id]
    d = world.hardware.labware[plate.labware]
    tw, th, gap = (max(26, 300 // d.cols), 60, 4)
    parts = []
    for c in range(d.cols):
        name = WellCoord(0, c).name
        x, y = 4 + c * (tw + gap), 4
        contents = plate.wells.get(name, [])
        vol = total_ul(contents)
        parts.append(f'<rect x="{x}" y="{y}" width="{tw}" height="{th}" rx="3" fill="#F7F7F7" stroke="#CCC"><title>{name}: empty</title></rect>')
        if vol > 0:
            dominant = max(contents, key=lambda l: l.volume_ul).reagent
            h = max(2.0, th * min(1.0, vol / cap))
            tip = f"{vol:g} uL: " + html.escape(describe_mixture(contents, world.inventory.reagents))
            parts.append(f'<rect x="{x}" y="{y + th - h:.1f}" width="{tw}" height="{h:.1f}" rx="3" fill="{colors.get(dominant, "#888")}"><title>{name}: {tip}</title></rect>')
        parts.append(f'<text x="{x + tw / 2}" y="{y + th + 12}" text-anchor="middle" fill="#999" font-size="9">{name}</text>')
    label = " (waste)" if plate.waste else ""
    w, h = 8 + d.cols * (tw + gap) - gap, th + 22
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg"><title>{html.escape(plate_id)}{label}</title>{"".join(parts)}</svg>'


def tip_rack_svg(world: World, rack_id: str) -> str:
    """A tip rack: solid dots are fresh tips, hollow ones were used."""
    rack = world.deck.tip_racks[rack_id]
    d = world.hardware.labware[rack.labware]
    used = set(rack.used)
    parts, ox, oy = _grid_header(d.rows, d.cols)
    for r in range(d.rows):
        for c in range(d.cols):
            name = WellCoord(r, c).name
            cx, cy = ox + c * CELL + CELL // 2, oy + r * CELL + CELL // 2
            if name in used:
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="4" fill="none" stroke="#BBB"><title>{name}: used</title></circle>')
            else:
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="4.5" fill="#8A8F98"><title>{name}: fresh</title></circle>')
    w, h = ox + d.cols * CELL + 4, oy + d.rows * CELL + 4
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'


def deck_svg(world: World) -> str:
    """The robot deck as labeled slots."""
    model = world.hardware.robot.model.value
    if model == "ot2":
        grid = [["10", "11", "12"], ["7", "8", "9"], ["4", "5", "6"], ["1", "2", "3"]]
    else:
        grid = [[f"{row}{col}" for col in range(1, 5)] for row in "ABCD"]
    bw, bh, gap = 86, 56, 6
    parts = []
    for r, row in enumerate(grid):
        for c, slot in enumerate(row):
            x, y = 4 + c * (bw + gap), 4 + r * (bh + gap)
            s = world.deck.slots.get(slot)
            label = "trash" if (s and s.trash) else (s.entity if s and s.entity else "")
            fill = "#F4F4F4" if s is None else ("#E8E0DA" if s.trash else "#EAF1F8")
            for mid, m in world.deck.modules.items():
                if m.slot == slot:
                    label = f"{mid} ▸ {m.holds}" if m.holds else mid
                    fill = "#F3E9F5" if m.engaged else "#EEE9F0"
            parts.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="6" fill="{fill}" stroke="#CCC"/>')
            parts.append(f'<text x="{x + 6}" y="{y + 15}" fill="#999" font-size="10">{slot}</text>')
            if label:
                parts.append(f'<text x="{x + bw / 2}" y="{y + bh / 2 + 9}" text-anchor="middle" fill="#333" font-size="12">{html.escape(label)}</text>')
    w = 8 + len(grid[0]) * (bw + gap) - gap
    h = 8 + len(grid) * (bh + gap) - gap
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="{FONT}">{"".join(parts)}</svg>'


def vials_html(world: World) -> str:
    """One level bar per vial, frozen and consumed states marked."""
    colors = reagent_colors(world)
    caps: dict[str, float] = {}
    for vid, link in world.deck.linker.items():
        rack = world.deck.tube_racks.get(link.rack)
        d = world.hardware.labware.get(rack.labware) if rack else None
        if d and d.well_max_ul:
            caps[vid] = d.well_max_ul
    rows = []
    for vid, v in sorted(world.inventory.vials.items()):
        cap = caps.get(vid, max(v.volume_ul, 1000.0))
        frac = min(1.0, v.volume_ul / cap) if cap else 0.0
        color = "#BBB" if v.consumed else colors.get(v.reagent, "#888")
        mark = " ❄" if v.state is ThermalState.frozen else (" — consumed" if v.consumed else "")
        rows.append(
            f'<div style="margin: 2px 0"><span style="display:inline-block;width:110px">{html.escape(vid)}{mark}</span>'
            f'<span style="display:inline-block;width:120px;height:10px;background:#EEE;border-radius:5px;vertical-align:middle">'
            f'<span style="display:block;width:{frac * 100:.0f}%;height:10px;background:{color};border-radius:5px"></span></span>'
            f' <span style="color:#777">{v.volume_ul:g} / {cap:g} uL</span></div>'
        )
    return "".join(rows) or '<div style="color:#999">no vials</div>'


def legend_html(world: World) -> str:
    colors = reagent_colors(world)
    dots = " &nbsp; ".join(
        f'<span style="color:{c}">●</span> {html.escape(name)}' for name, c in sorted(colors.items())
    )
    return f'<div style="margin-top:6px;color:#555">{dots}</div>'


def world_html(world: World) -> str:
    """The whole bench on one card: deck, plates, vials, tips."""
    hw = world.hardware
    head = f"{hw.robot.vendor.value if hasattr(hw.robot.vendor, 'value') else hw.robot.vendor} {hw.robot.model.value} · world {world.hash()[:12]}"
    panels = [f'<div><div style="color:#777;margin-bottom:4px">deck</div>{deck_svg(world)}</div>']
    for pid in sorted(world.inventory.plates):
        panels.append(f'<div><div style="color:#777;margin-bottom:4px">{html.escape(pid)}</div>{plate_svg(world, pid)}</div>')
    for tid in sorted(world.deck.tip_racks):
        panels.append(f'<div><div style="color:#777;margin-bottom:4px">{html.escape(tid)} (tips)</div>{tip_rack_svg(world, tid)}</div>')
    panels.append(f'<div><div style="color:#777;margin-bottom:4px">vials</div>{vials_html(world)}</div>')
    return (
        f'<div style="{FONT}"><div style="margin-bottom:6px;color:#555">{head}</div>'
        f'<div style="display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start">{"".join(panels)}</div>'
        f"{legend_html(world)}</div>"
    )


def diff_html(diff: WorldDiff, world: World | None = None) -> str:
    """A diff report: the summary, every entry with its verdict, and (when the world
    is given) plates colored by verdict per well."""
    head = (
        f"outcome {diff.outcome} · <b>{diff.classification}</b> · "
        + " · ".join(f"{k.lower()}: {v}" for k, v in sorted(diff.counts.items()))
        + (" · can localize" if diff.can_localize else " · cannot localize")
    )
    rows = []
    for e in diff.entries:
        c = VERDICT_COLORS[e.verdict]
        obs = "—" if e.observed is None else f"{e.observed:g}"
        delta = "—" if e.delta is None else f"{e.delta:+g}"
        rows.append(
            f'<tr><td>{html.escape(e.label)}</td><td>{html.escape(e.entity)}</td><td>{html.escape(e.metric)}</td>'
            f'<td style="text-align:right">{e.predicted:g}</td><td style="text-align:right">{obs}</td>'
            f'<td style="text-align:right">{delta}</td><td style="text-align:right">{e.sigma:g}</td>'
            f'<td><span style="color:{c}">●</span> {e.verdict.value}</td></tr>'
        )
    table = (
        '<table style="border-collapse:collapse;margin-top:6px"><tr>'
        + "".join(f'<th style="text-align:left;padding:2px 10px 2px 0;color:#777">{h}</th>' for h in ["reading", "entity", "metric", "predicted", "observed", "delta", "sigma", "verdict"])
        + "</tr>"
        + "".join(rows)
        + "</table>"
    )
    plates = ""
    if world is not None:
        verdicts: dict[str, dict[str, Verdict]] = {}
        for e in diff.entries:
            if e.entity in world.inventory.plates and WellCoord.parse(e.metric) is not None:
                verdicts.setdefault(e.entity, {})[e.metric] = e.verdict
        panels = [f'<div><div style="color:#777;margin-bottom:4px">{html.escape(pid)}</div>{_verdict_plate_svg(world, pid, v)}</div>' for pid, v in sorted(verdicts.items())]
        if panels:
            plates = f'<div style="display:flex;flex-wrap:wrap;gap:20px;margin-top:8px">{"".join(panels)}</div>'
    notes = "".join(f'<div style="color:#777">note: {html.escape(n)}</div>' for n in diff.notes)
    unaccounted = "".join(f'<div style="color:#777">unaccounted: {html.escape(k)} {v:+g}</div>' for k, v in diff.unaccounted.items())
    return f'<div style="{FONT}"><div>{head}</div>{table}{plates}{notes}{unaccounted}</div>'


def _verdict_plate_svg(world: World, plate_id: str, verdicts: dict[str, Verdict]) -> str:
    plate = world.inventory.plates[plate_id]
    d = world.hardware.labware[plate.labware]
    parts, ox, oy = _grid_header(d.rows, d.cols)
    for r in range(d.rows):
        for c in range(d.cols):
            name = WellCoord(r, c).name
            cx, cy = ox + c * CELL + CELL // 2, oy + r * CELL + CELL // 2
            v = verdicts.get(name)
            if v is None:
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="{CELL // 2 - 3}" fill="#F1F1F1" stroke="#DDD"><title>{name}: not covered by a sensor</title></circle>')
            else:
                parts.append(f'<circle cx="{cx}" cy="{cy}" r="{CELL // 2 - 3}" fill="{VERDICT_COLORS[v]}"><title>{name}: {v.value}</title></circle>')
    w, h = ox + d.cols * CELL + 4, oy + d.rows * CELL + 4
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'


@dataclass
class Frame:
    """One moment of a replay: the world right after `description` happened."""

    step: int
    segment: int
    description: str
    world: World

    def _repr_html_(self) -> str:
        return f'<div style="{FONT};margin-bottom:4px">step {self.step}: {html.escape(self.description)}</div>' + world_html(self.world)


def describe_op(world: World, op: PirL) -> str:
    """A plain sentence for one lowered op, naming vials rather than rack positions."""

    def place(labware: str, well: str) -> str:
        for vid, link in world.deck.linker.items():
            if link.rack == labware and link.well == well:
                return vid
        return f"{labware} {well}"

    if isinstance(op, PickUpTip):
        return f"pick up tip {op.well} from {op.rack}"
    if isinstance(op, Aspirate):
        gap = f" + {op.air_gap_ul:g} uL air" if op.air_gap_ul else ""
        return f"aspirate {op.volume_ul:g} uL from {place(op.labware, op.well)}{_how(op.at, op.offset_mm, op.side_mm, op.rate_ul_s)}{gap}"
    if isinstance(op, Dispense):
        blow = ", blow out" if op.blow_out else ""
        return f"dispense {op.volume_ul:g} uL into {place(op.labware, op.well)}{_how(op.at, op.offset_mm, op.side_mm, op.rate_ul_s)}{blow}"
    if isinstance(op, MixOp):
        return f"mix {op.repetitions} x {op.volume_ul:g} uL at {place(op.labware, op.well)}"
    if isinstance(op, DropTip):
        return "drop tip into trash"
    if isinstance(op, ReturnTip):
        return f"return tip to {op.rack} {op.well}"
    if isinstance(op, Pause):
        return op.message
    if isinstance(op, ObserveL):
        return f"read {op.sensor} ({op.label})"
    if isinstance(op, Delay):
        return f"wait {op.seconds:g} s"
    if isinstance(op, Magnet):
        return f"engage magnet {op.module} at {op.height_mm:g} mm" if op.engaged and op.height_mm is not None else f"disengage magnet {op.module}"
    return op.op


def _how(at: str | None, offset_mm: float | None, side_mm: float, rate_ul_s: float | None) -> str:
    bits = []
    if at is not None or offset_mm is not None:
        bits.append(f"{offset_mm if offset_mm is not None else 1:g} mm from the {at or 'bottom'}")
    if side_mm:
        bits.append(f"{side_mm:+g} mm sideways")
    if rate_ul_s is not None:
        bits.append(f"{rate_ul_s:g} uL/s")
    return f" ({', '.join(bits)})" if bits else ""


def trace(world: World, protocol: Protocol, budget: Budget | None = None, decisions: list[bool] | None = None) -> list[Frame]:
    """Replay a protocol with ideal pipettes and film every step: compile, lower, then
    apply one lowered op at a time. `decisions` picks the arm at each branch (first
    arm by default). Frame 0 is the world before anything happens."""
    from ztra.compiler import compile
    from ztra.drivers.fake import FakeDriver

    program = lower(world, compile(world, protocol, budget=budget).pir)
    driver = FakeDriver(world, accurate=True)
    frames = [Frame(0, 0, "start", world.clone())]

    class _Capture:
        segment = 0
        ops: list[PirL] = []

        def on_observe(self, op: ObserveL, op_index: int) -> None: ...

        def on_pause(self, op: Pause, op_index: int) -> None: ...

        def on_op_done(self, op_index: int) -> None:
            frames.append(Frame(len(frames), self.segment, describe_op(driver.physical, self.ops[op_index]), driver.physical.clone()))

    capture = _Capture()
    for index in program.walk(list(decisions or [])):
        capture.segment = index
        capture.ops = program.segments[index].ops
        driver.run_segment(driver.physical, index, program.segments[index], "", capture)
    return frames


def animate_html(frames: list[Frame], title: str = "") -> str:
    """All frames in one self-contained snippet with a scrubber and a play button.
    Works live in Jupyter; a static render (like GitHub) shows the final frame."""
    uid = f"ztra{next(_ids)}"
    divs = []
    for i, f in enumerate(frames):
        shown = "block" if i == len(frames) - 1 else "none"
        label = f"step {f.step} / {len(frames) - 1}: {html.escape(f.description)}"
        divs.append(f'<div class="{uid}f" style="display:{shown}"><div style="margin:4px 0;color:#333">{label}</div>{world_html(f.world)}</div>')
    head = f'<div style="margin-bottom:4px;color:#555">{html.escape(title)}</div>' if title else ""
    n = len(frames)
    return (
        f'<div style="{FONT}">{head}'
        f'<div><button onclick="{uid}play()">▶ play</button> '
        f'<input id="{uid}s" type="range" min="0" max="{n - 1}" value="{n - 1}" style="width:300px;vertical-align:middle" oninput="{uid}show(+this.value)"></div>'
        f"{''.join(divs)}"
        f"<script>"
        f"function {uid}show(i){{var f=document.getElementsByClassName('{uid}f');for(var j=0;j<f.length;j++)f[j].style.display=j==i?'block':'none';document.getElementById('{uid}s').value=i;}}"
        f"function {uid}play(){{var i=0;var t=setInterval(function(){{ {uid}show(i); if(++i>={n})clearInterval(t);}},400);}}"
        f"</script></div>"
    )
