"""Each charger's car in the common shape (`planning/flexible_load.py`, plan `plans/v3_flexible_loads_design.md`
step 1): energy by the departure (the plan's, else the settings'), the charging current between the charger's floor
and the current set (and the phases where the sun may switch them), the car's minimum charge level and the person's
own Start or Stop as hard rules, kWh short as what bending it costs, and the choices the import target already has
(`AutoSettings.if_not_ready`). Descriptions only, from what the charger, its settings and the car's stored
properties hold; nothing is written and nothing planned.
"""

from __future__ import annotations

from typing import Any

from ..const import CONF_CHARGER_ENTRY_IDS, DEFAULT_MIN_CURRENT_A
from ..core.parse import aware_instant
from ..planning.auto_settings import (
    DRIVER_MANUAL_KWH,
    DRIVER_TARGET_SOC,
    IF_NOT_READY_HOLD,
    IF_NOT_READY_RELEASE,
    AutoSettings,
)
from ..planning.flexible_load import (
    CHOICE_ACCEPT_PEAK,
    CHOICE_LATER,
    CHOICE_TAKES_WHAT_FITS,
    GIVE_KWH_SHORT,
    GOAL_ENERGY_BY,
    HANDLE_CONTINUOUS,
    HANDLE_STEPS,
    KIND_CAR,
    Choice,
    FlexibleLoad,
    Give,
    Goal,
    Handle,
    rule,
    values,
)
from .controller import ChargingPlan

#: What each choice sets among the existing settings (`AutoSettings.if_not_ready`, the departure).
SETS_IF_NOT_READY_HOLD = f"if_not_ready:{IF_NOT_READY_HOLD}"
SETS_IF_NOT_READY_RELEASE = f"if_not_ready:{IF_NOT_READY_RELEASE}"
SETS_DEPARTURE = "departure"


def _goal(settings: AutoSettings, plan: ChargingPlan | None) -> Goal:
    """The plan's energy and departure where it has them, else what the settings ask for."""
    departure = settings.departure.strftime("%H:%M") if settings.departure_enabled else None
    if plan is not None:
        return Goal(
            GOAL_ENERGY_BY,
            by=aware_instant(plan.departure),
            values=values(
                source="plan",
                kwh=plan.energy_kwh,
                target_soc_pct=plan.target_soc_percent,
                fill_to_limit=plan.to_vehicle_limit,
                departure=departure,
                strategy=settings.strategy,
            ),
        )
    return Goal(
        GOAL_ENERGY_BY,
        values=values(
            source="settings",
            kwh=settings.requested_kwh if settings.driver == DRIVER_MANUAL_KWH and not settings.fill_to_limit else None,
            target_soc_pct=settings.target.target_percent if settings.driver == DRIVER_TARGET_SOC else None,
            fill_to_limit=settings.driver == DRIVER_MANUAL_KWH and settings.fill_to_limit,
            departure=departure,
            strategy=settings.strategy,
        ),
    )


def car_load(
    site_id: str,
    charger_entry_id: str,
    settings: AutoSettings,
    plan: ChargingPlan | None,
    *,
    name: str | None = None,
    min_current_a: float = DEFAULT_MIN_CURRENT_A,
    current_a: float | None = None,
    phases: int = 3,
    phase_switching: bool = False,
    switch_only: bool = False,
    min_soc_pct: float | None = None,
    controlled: bool = True,
    power_tariff: bool = False,
    import_target: bool = False,
) -> FlexibleLoad:
    """One charger's car. `min_current_a` the charger's floor (under 6 A for a charger given a power),
    `current_a` the current set; `phase_switching` whether the sun may switch its phases; `min_soc_pct` the car's
    own minimum charge level (`None`: none set); `power_tariff` and `import_target` the site's ceilings."""
    handles = [
        Handle(
            HANDLE_CONTINUOUS,
            unit="A",
            low=None if switch_only else min_current_a,
            high=current_a,
            values=values(
                phases=phases,
                power_given=min_current_a < DEFAULT_MIN_CURRENT_A,
                switch_only=switch_only,
            ),
        )
    ]
    if phase_switching:
        handles.append(Handle(HANDLE_STEPS, unit="phases", steps=(1.0, 3.0)))
    pause = settings.pause
    choices = [
        Choice(CHOICE_TAKES_WHAT_FITS, "car_takes_what_fits", SETS_IF_NOT_READY_HOLD, settings.if_not_ready == IF_NOT_READY_HOLD),
        Choice(CHOICE_LATER, "car_later", SETS_DEPARTURE),
    ]
    if power_tariff or import_target:
        choices.append(
            Choice(
                CHOICE_ACCEPT_PEAK,
                "car_accept_peak",
                SETS_IF_NOT_READY_RELEASE,
                settings.if_not_ready == IF_NOT_READY_RELEASE,
            )
        )
    return FlexibleLoad(
        id=charger_entry_id,
        kind=KIND_CAR,
        site=site_id,
        name=name,
        controlled=controlled,
        goal=_goal(settings, plan),
        handles=tuple(handles),
        hard_rules=(
            rule("min_charge_level", pct=min_soc_pct),
            rule(
                "person_start_stop",
                action=pause.action if pause.manual else None,
                scope=pause.scope if pause.manual else None,
            ),
        ),
        give=Give(GIVE_KWH_SHORT),
        choices=tuple(choices),
    )


def site_car_loads(site: Any, *, power_tariff: bool, import_target: bool) -> list[FlexibleLoad]:
    """Each member charger's car from what its controller, its settings and the car's stored properties hold."""
    # (imported here: these reach the site and the charger's runtime, which import this package)
    from ..planning.phases import charging_phases  # noqa: PLC0415
    from ..runtime import controller_for, domain_data  # noqa: PLC0415
    from ..vehicles.vehicle_discovery import charger_vehicle_ids, resolve_target_vehicle  # noqa: PLC0415
    from ..vehicles.vehicle_properties import stored_properties  # noqa: PLC0415

    hass = site.hass
    store = domain_data(hass).auto_store
    found: list[FlexibleLoad] = []
    for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        controller = controller_for(hass, charger_entry_id)
        if controller is None:
            continue
        settings = AutoSettings() if store is None else store.settings(charger_entry_id)
        vehicle_id, _ = resolve_target_vehicle(hass, settings.target.vehicle_id, charger_vehicle_ids(hass, charger_entry_id))
        floor = None if vehicle_id is None else stored_properties(hass, vehicle_id).min_percent
        entry = hass.config_entries.async_get_entry(charger_entry_id)
        found.append(
            car_load(
                site.entry_id,
                charger_entry_id,
                settings,
                controller.plan,
                name=None if entry is None else entry.title,
                min_current_a=site.charger_min_current_a(charger_entry_id),
                current_a=controller.configured_charging_current_a(),
                phases=charging_phases(hass, charger_entry_id).phases,
                phase_switching=controller.phase_switch_allowed(),
                switch_only=controller.is_switch_only,
                min_soc_pct=None if floor is None else float(floor),
                controlled=controller.is_commandable or controller.is_switch_only,
                power_tariff=power_tariff,
                import_target=import_target,
            )
        )
    return found
