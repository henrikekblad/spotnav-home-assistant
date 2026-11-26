"""The fuse rules as pure functions of one look: the live clamp of a grid charge (balanced and per phase), what a
hold's peak cover gives back, and the room a hold or a sale needs to start."""

from __future__ import annotations

import math
from typing import Any, Final

from ..commands import Phases
from ..gate import MIN_RATE_KW, phase_split_kw
from .const import (
    BATTERY_MAX_AGE_S,
    HEADROOM_MARGIN_A,
    PHASE_GRID_MARGIN_A,
    PHASE_PROBE_MAX_KW,
    PHASE_STEP_A,
    PHASE_STEP_KW,
    YIELD_STEP_KW,
    _AMP_SLACK,
)
from .state import ExecInputs


def _kw(amps: float, volts: float) -> float:
    """Amps per phase over three phases as kW (signed)."""
    return amps * math.sqrt(3.0) * volts / 1000.0


def grid_charge_cap_kw(
    inputs: ExecInputs, commanded_kw: float, *, yield_now: bool = True, covered_a: float | Phases = 0.0
) -> float | None:
    """The most the battery may charge from the grid now (kW), `None` when the headroom is unknown.

    What the battery already takes from the grid is counted back (it is in the measured import): at most its own
    charge, the rate commanded and the site's import, and nothing when the import is unknown, so the figure is never
    above the truth. The headroom keeps `HEADROOM_MARGIN_A` under the fuse, on as many phases as the plant spreads
    the charge over (`ExecInputs.charge_phases`: a charge not known to be balanced may land on the tightest one). With "car first" and a car held, the
    battery yields `YIELD_STEP_KW` below what it measurably takes from the grid now (not below its last command, which
    it may not have reached yet), one step per `RATE_UP_DWELL_S` (`yield_now`); between steps it keeps its rate.

    `covered_a`: what the battery's discharge now gives the most loaded phase (`cover_a`), counted out of the headroom
    on a change into the charge (from a sale, say): the headroom is measured while the battery still covers, and that
    cover ends with the change. Given per phase (`cover_phase_a`), each phase's own cover comes out of that phase's
    headroom where the plant's phases are shown to be the meter's (`ExecInputs.plant_phases_mapped`), else the
    largest cover out of the least headroom (`_room_after_cover_a`)."""
    if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
        return None
    battery = inputs.facts.power_kw
    own = measured = 0.0
    if inputs.grid_import_kw is not None and battery is not None:
        own = max(min(commanded_kw, battery, inputs.grid_import_kw), 0.0)
        measured = max(min(battery, inputs.grid_import_kw), 0.0)
    phases = min(max(inputs.charge_phases, 1), 3)
    paired = inputs.phase_headroom_a if inputs.plant_phases_mapped else None
    room_a = _room_after_cover_a(inputs.headroom_a, paired, covered_a) - HEADROOM_MARGIN_A
    cap = own + _kw(room_a, inputs.volts_between_phases) * phases / 3
    if inputs.car_first and inputs.car_held:
        cap = min(cap, measured - YIELD_STEP_KW if yield_now else commanded_kw)
    return cap


def _room_after_cover_a(headroom_a: float, phase_headroom_a: Phases | None, covered_a: float | Phases) -> float:
    """The least headroom on a phase once the battery's cover has ended (A, before the margin). One figure
    (`cover_a`) is taken off the least headroom: it may all be on that phase. Per phase (`cover_phase_a`), each
    phase's cover comes off its own headroom where every phase's headroom is given, and never more than the least
    headroom less the least cover. The caller gives the headroom per phase only where the plant's phase A, B and C
    are shown to be the meter's L1, L2 and L3 (`ExecInputs.plant_phases_mapped`); without it (or with a phase's
    headroom unknown) the largest cover comes off the least headroom, which holds whichever phase is which."""
    if not isinstance(covered_a, tuple):
        return headroom_a - max(covered_a, 0.0)
    cover = [max(value, 0.0) for value in covered_a]
    if not _finite3(phase_headroom_a) or phase_headroom_a is None:
        return headroom_a - max(cover)
    paired = min(room - part for room, part in zip(phase_headroom_a, cover, strict=True))
    return min(paired, headroom_a - min(cover))


def house_net_kw(inputs: ExecInputs) -> float | None:
    """The house's net load now (kW): the site's import less what the battery charges (negative: the sun gives
    more than the house takes); `None` when either is unknown."""
    grid, battery = inputs.grid_import_kw, inputs.facts.power_kw
    if grid is None or battery is None or not math.isfinite(grid) or not math.isfinite(battery):
        return None
    return grid - battery


def _finite3(values: Any) -> bool:
    return values is not None and len(values) == 3 and all(
        value is not None and math.isfinite(value) for value in values
    )


def _target_caps(inputs: ExecInputs) -> Phases:
    """For `grid` targets: the most each phase may charge by its HA-down cap (kW). The target that stands if Home
    Assistant stops is the load behind the point plus the charge, and the grid's phase is then what is beside the
    point plus that target, whatever the load behind does meanwhile: so the cap bounds the target, and the charge's
    share of it is the cap less the load behind (a negative load, a source behind the point, is not counted as
    room). 0 on every phase without the load behind."""
    caps = inputs.phase_caps_kw if _finite3(inputs.phase_caps_kw) else (0.0, 0.0, 0.0)
    behind = inputs.phase_behind_kw
    if not _finite3(behind) or behind is None:
        return (0.0, 0.0, 0.0)
    out = [max(cap - max(load, 0.0), 0.0) for cap, load in zip(caps, behind, strict=True)]  # type: ignore[arg-type]
    return (out[0], out[1], out[2])


def _phase_room_kw(inputs: ExecInputs, commanded: Phases | None, *, grid: bool = False) -> Phases | None:
    """The most each phase may charge now (kW): its HA-down cap (`ExecInputs.phase_caps_kw`, 0 without one) and its
    live room, what the plant already takes on that phase (at most what was commanded on it and what the plant's own
    power per phase shows, nothing when that is unknown, so never above the truth) plus the phase's headroom less
    `HEADROOM_MARGIN_A`, less what the plant puts out on the phase now: the headroom is measured under that output (a
    sale's, the cover the plant gives in its own mode, the sun's), and a target makes the phase's power the charge
    instead. Without the plant's power per phase, all the battery discharges now is counted on each phase. `None`
    when a phase's headroom is unknown.

    `grid`: the targets are the grid's (`PhaseControl.target_kind`), where the phase's cap may be the fuse itself
    (`gate.grid_charge_phase_caps_kw`), so the live room is reckoned exactly and is the one bound. A target of the
    load behind the point plus a charge `c` puts the phase at its import now, plus what the plant puts out on it
    now (which it then no longer does), plus `c`: the room is the headroom less `PHASE_GRID_MARGIN_A` less the
    plant's own output now (signed: a plant that charges now has that back). No room without the plant's power per
    phase. The HA-down cap bounds the whole target, which is what stands if Home Assistant stops
    (`_target_caps`)."""
    if not _finite3(inputs.phase_headroom_a) or inputs.volts_between_phases <= 0:
        return None
    assert inputs.phase_headroom_a is not None
    volts = inputs.volts_between_phases / math.sqrt(3.0)
    caps = _target_caps(inputs) if grid else (
        inputs.phase_caps_kw if _finite3(inputs.phase_caps_kw) else (0.0, 0.0, 0.0)
    )
    plant = inputs.plant_phase_kw if _finite3(inputs.plant_phase_kw) else None
    power = inputs.facts.power_kw
    discharge = max(-power, 0.0) if power is not None and math.isfinite(power) else 0.0
    room: list[float] = []
    for index in range(3):
        if grid:
            if plant is None:
                room.append(0.0)
                continue
            exact = (inputs.phase_headroom_a[index] - PHASE_GRID_MARGIN_A) * volts / 1000.0 - plant[index]
            room.append(max(min(caps[index], exact), 0.0))  # type: ignore[index]
            continue
        own = 0.0
        if plant is not None and commanded is not None:
            own = max(min(commanded[index], -plant[index]), 0.0)
        output = max(plant[index], 0.0) if plant is not None else discharge
        live = own + (inputs.phase_headroom_a[index] - HEADROOM_MARGIN_A) * volts / 1000.0 - output
        room.append(max(min(caps[index], live), 0.0))  # type: ignore[index]
    return (room[0], room[1], room[2])


def _floored(values: Phases | list[float], step_kw: float = PHASE_STEP_KW) -> Phases:
    """Each phase rounded toward zero to `step_kw`, then to `PHASE_STEP_KW` (never above what was allowed)."""
    slack = 1e-6 if step_kw <= PHASE_STEP_KW else _AMP_SLACK
    out = [math.floor(abs(value) / step_kw + slack) * step_kw for value in values]
    out = [math.copysign(math.floor(size / PHASE_STEP_KW + 1e-6) * PHASE_STEP_KW, value)
           for size, value in zip(out, values, strict=True)]
    return (round(out[0], 3) + 0.0, round(out[1], 3) + 0.0, round(out[2], 3) + 0.0)


def phase_charge_kw(
    inputs: ExecInputs, commanded: Phases | None, *, yield_now: bool = True, live: bool = True, grid: bool = False
) -> tuple[Phases | None, str | None]:
    """The charge to command on L1, L2 and L3 now (kW), or why none can run.

    Each phase at most its room (`_phase_room_kw`: the HA-down cap and the live headroom); the sum at most the
    plan's power and the battery's own limit, spread as evenly as the rooms allow (`gate.phase_split_kw`). With "car
    first" and a car held the sum yields as the balanced charge does (`grid_charge_cap_kw`). `live` false: the caps
    alone, for what the charge would be with the house quiet. The verification's probe
    (`ExecInputs.phase_probe_kw`) is taken as it is: a charge on a phase still within that phase's room, in whole
    amps, a discharge at most `PHASE_PROBE_MAX_KW`."""
    room = _phase_room_kw(inputs, commanded, grid=grid)
    if room is None:
        return None, "headroom_unknown"
    if not live:
        caps = _target_caps(inputs) if grid else (
            inputs.phase_caps_kw if _finite3(inputs.phase_caps_kw) else (0.0, 0.0, 0.0)
        )
        room = (max(caps[0], 0.0), max(caps[1], 0.0), max(caps[2], 0.0))  # type: ignore[index]
    # Whole amps on each phase: a room that wobbles by a fraction of an amp changes nothing.
    amp_kw = PHASE_STEP_A * inputs.volts_between_phases / math.sqrt(3.0) / 1000.0
    probe = inputs.phase_probe_kw
    if probe is not None:
        if not _finite3(probe):
            return None, "probe_unknown"
        return _floored([
            math.floor(min(value, room[index]) / amp_kw + _AMP_SLACK) * amp_kw if value > 0
            else max(value, -PHASE_PROBE_MAX_KW)
            for index, value in enumerate(probe)
        ]), None
    room = (*(math.floor(value / amp_kw + _AMP_SLACK) * amp_kw for value in room),)  # type: ignore[assignment]
    total = min(abs(inputs.rate_kw), sum(room))
    limit = inputs.facts.max_charge_kw
    if limit is not None:
        total = min(total, limit)
    if live and inputs.car_first and inputs.car_held:
        battery, grid = inputs.facts.power_kw, inputs.grid_import_kw
        measured = max(min(battery, grid), 0.0) if battery is not None and grid is not None else 0.0
        held = 0.0 if commanded is None else sum(max(value, 0.0) for value in commanded)
        total = min(total, measured - YIELD_STEP_KW if yield_now else held)
    rates = _floored(list(phase_split_kw(total, room)), amp_kw)
    if sum(rates) < MIN_RATE_KW:
        return None, "fuse_headroom"
    return rates, None


def cover_a(inputs: ExecInputs, *, sale: bool = False) -> float:
    """What the battery's discharge now may give the most loaded phase (A): what comes back on it once the battery
    stops covering. All of it on one phase (kW·1000 / the phase voltage): a SigenStor covers each phase on its own,
    and the battery's power per phase is not known. A running sale (`sale`), or any discharge of a plant that puts
    out the same on each phase (`ExecInputs.output_balanced`: a three-phase Solis), on a plant that spreads its power
    evenly over three phases (`ExecInputs.charge_phases`) gives each phase a third, unless the plant's own power per
    phase (`ExecInputs.plant_phase_kw`) shows more on one: that phase's output is then counted, at most the discharge (the
    sun's share of it is counted too, so the figure is never under the truth). 0 when the battery does not
    discharge."""
    power = inputs.facts.power_kw
    if power is None or not math.isfinite(power) or power >= 0 or inputs.volts_between_phases <= 0:
        return 0.0
    kw = -power
    if (sale or inputs.output_balanced) and inputs.charge_phases >= 3:
        share = kw / 3.0
        plant = inputs.plant_phase_kw
        if plant is not None and _finite3(plant):
            share = max(share, min(kw, max(plant)))
        kw = share
    return kw * 1000.0 * math.sqrt(3.0) / inputs.volts_between_phases


def cover_phase_a(inputs: ExecInputs) -> Phases | None:
    """What ends on each phase when a grid charge starts (A on the plant's A, B and C), where the plant's own power
    per phase (`ExecInputs.plant_phase_kw`) is read. `None` when it is not read: then `cover_a`.

    The battery discharges: each phase's whole output. The reading has the sun's power in it too, which is counted
    as well: a grid charge may take the sun into the battery instead of the house, so the figure is never under what
    comes back on the phase (and may then be above the whole discharge). In its own mode a SigenStor covers each
    phase's own load (on the owner's plant the grid read 0 on every phase with the house unbalanced and the output
    on L3 matched the heat pump there), so a third a phase is never assumed.

    The battery idle or charging: what the plant puts out is the sun's, which the plant in its own mode may put
    where the house's load is (13 A of house on L1 all met by the sun reads 0 A there). Under the charge the plant's
    power is taken to be the same on each of its phases, so a phase loses what it has above the plant's average
    over the three: that part is its cover, and nothing where it is at or under the average. The same sun on every
    phase is no cover: the charge takes a third of what it changes off each phase, which the cap already counts."""
    power = inputs.facts.power_kw
    plant = inputs.plant_phase_kw
    if (
        power is None or not math.isfinite(power) or inputs.volts_between_phases <= 0
        or plant is None or not _finite3(plant)
    ):
        return None
    scale = 1000.0 * math.sqrt(3.0) / inputs.volts_between_phases
    base = 0.0 if power < 0 else sum(plant) / 3.0
    out = [max(value - base, 0.0) * scale for value in plant]
    return (out[0], out[1], out[2])


def hold_headroom_ok(inputs: ExecInputs, sale_kw: float = 0.0, *, from_sale: bool = False) -> bool:
    """Whether a hold may start (or a lifted one hold again, or a hold be changed into): the headroom keeps
    `HEADROOM_MARGIN_A` once the battery stops covering what it discharges now (`cover_a`; `from_sale`: the
    discharge now is a running sale's).

    A sale (`sale_kw`, its rate) uses the same rule less the sale's own share on each phase: spread evenly over a
    plant's three phases it still gives that back on the loaded one. A plant on fewer phases gets no share (the
    phase it puts out on is not known), except on a single-phase site (`ExecInputs.site_phases`), where the sale
    is all on L1 as the cover is."""
    if inputs.headroom_a is None or inputs.volts_between_phases <= 0:
        return False
    return (
        inputs.headroom_a - _own_import_a(inputs) - uncovered_a(inputs, sale_kw, from_sale=from_sale)
        >= HEADROOM_MARGIN_A
    )


def _own_import_a(inputs: ExecInputs) -> float:
    """What the plant may import by itself on the most loaded phase now (A, `ExecInputs.own_import_a`): never below 0,
    anything not a number (or not known) as more than any fuse (`OWN_IMPORT_UNKNOWN_A`, finite for the change log)."""
    own = inputs.own_import_a
    if isinstance(own, bool) or not isinstance(own, (int, float)) or math.isnan(own):
        return OWN_IMPORT_UNKNOWN_A
    return min(max(float(own), 0.0), OWN_IMPORT_UNKNOWN_A)


#: A plant's own import not known (A): more than any fuse, so nothing starts on it.
OWN_IMPORT_UNKNOWN_A: Final = 1000.0


def uncovered_a(inputs: ExecInputs, sale_kw: float = 0.0, *, from_sale: bool = False) -> float:
    """What the start rule (`hold_headroom_ok`) counts as coming back on the most loaded phase (A): the cover now
    (`cover_a`) less a sale's own share on each phase at `sale_kw`: a third on a plant on three phases, all of it on a
    single-phase site (`ExecInputs.site_phases`: the sale and the cover are both on L1), none otherwise."""
    if inputs.volts_between_phases <= 0:
        return 0.0
    extra_a = cover_a(inputs, sale=from_sale)
    if sale_kw > 0 and inputs.site_phases == 1:
        extra_a = max(extra_a - sale_kw * 1000.0 * math.sqrt(3.0) / inputs.volts_between_phases, 0.0)
    elif sale_kw > 0 and inputs.charge_phases >= 3:
        extra_a = max(extra_a - sale_kw * 1000.0 / (math.sqrt(3.0) * inputs.volts_between_phases), 0.0)
    return extra_a


def _import_headroom_a(inputs: ExecInputs) -> float | None:
    """The least headroom on a phase that imports now (A): a sale's hand-back for the fuse. The export side is the
    export cap's (`gate.export_discharge_cap_kw`, recomputed each look), which lets a sale and the sun fill a phase
    to where its headroom reads about `RESTORE_HEADROOM_A`: judged there too, a sale at its cap would be handed back
    and started again every `REARM_S`. `None` when no phase imports; the least headroom on any phase when the
    meter's sign per phase is not known. On a single-phase site (`ExecInputs.site_phases`) the one phase is L1: its
    headroom while the site imports (or its grid power is not known), `None` while it exports."""
    if inputs.site_phases == 1:
        grid = inputs.grid_import_kw
        if grid is None or not math.isfinite(grid) or grid >= 0.0:
            return inputs.headroom_a
        return None
    signed, headroom = inputs.grid_phase_kw, inputs.phase_headroom_a
    if signed is None or headroom is None:
        return inputs.headroom_a
    importing = [room for power, room in zip(signed, headroom, strict=True) if power >= 0.0]
    return min(importing) if importing else None


def _fresh(inputs: ExecInputs) -> bool:
    age = inputs.battery_age_s
    return inputs.facts.power_kw is not None and age is not None and age <= BATTERY_MAX_AGE_S
