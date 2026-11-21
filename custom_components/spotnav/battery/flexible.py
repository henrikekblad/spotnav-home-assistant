"""The site's home battery in the common shape (`planning/flexible_load.py`, plan `plans/v3_flexible_loads_design.md`
step 1): store and give back, a continuous power (per phase where the plant runs its phases), its floor, the fuse
rules and the day's write caps, and kr as what bending it costs. No choices: the battery is planned by itself.
Descriptions only, from the facts and options the battery's control already holds; nothing is written.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..const import CONF_MAIN_FUSE_A
from ..planning.flexible_load import (
    GIVE_KR,
    GOAL_STORE,
    HANDLE_CONTINUOUS,
    KIND_HOME_BATTERY,
    FlexibleLoad,
    Give,
    Goal,
    Handle,
    rule,
    values,
)
from .commands import SETTING_WRITES, BrandControl, control_for
from .executor import HEADROOM_MARGIN_A
from .facts import BatteryFacts
from .gate import Options


def _fuse_a(config: Mapping[str, Any]) -> float | None:
    fuse_a = config.get(CONF_MAIN_FUSE_A)
    return float(fuse_a) if isinstance(fuse_a, (int, float)) and not isinstance(fuse_a, bool) else None


def battery_load(
    site_id: str,
    facts: BatteryFacts,
    options: Options,
    *,
    main_fuse_a: float | None,
    phased: bool,
    control: BrandControl | None,
) -> FlexibleLoad | None:
    """The home battery, `None` where the site has none. `phased`: the plant runs its grid charge per phase now;
    `control` the brand's control (its write caps), `None` for a brand SpotNav writes nothing to."""
    if facts.battery_entity_id is None:
        return None
    caps = {} if control is None else dict(control.write_caps)
    return FlexibleLoad(
        id=facts.battery_entity_id,
        kind=KIND_HOME_BATTERY,
        site=site_id,
        name=facts.label,
        controlled=options.enabled and not options.shadow_only and control is not None,
        goal=Goal(
            GOAL_STORE,
            values=values(
                usable_kwh=facts.capacity_kwh,
                floor_soc_pct=facts.reserve_soc_pct,
                mode=options.mode,
                allow_sell=options.allow_sell,
            ),
        ),
        handles=(
            Handle(
                HANDLE_CONTINUOUS,
                unit="kW",
                low=None if facts.max_discharge_kw is None else -facts.max_discharge_kw,
                high=facts.max_charge_kw,
                values=values(per_phase=phased, phases_read=facts.plant_phase_entities is not None),
            ),
        ),
        hard_rules=(
            rule("floor_soc", pct=facts.reserve_soc_pct),
            rule(
                "fuse",
                main_fuse_a=main_fuse_a,
                margin_a=HEADROOM_MARGIN_A,
                accept_ha_down=options.accept_ha_down,
                import_limit_acts=options.import_limit_acts,
            ),
            rule(
                "write_cap",
                setting_per_day=caps.get("setting", SETTING_WRITES),
                setpoint_per_day=caps.get("setpoint"),
            ),
        ),
        give=Give(GIVE_KR),
        choices=(),
    )


def site_battery_load(site: Any) -> FlexibleLoad | None:
    """The site's home battery from what its shadow and control hold now (see the module)."""
    battery = site.battery_control
    facts: BatteryFacts = site.battery_shadow.facts
    executor = battery.executor
    return battery_load(
        site.entry_id,
        facts,
        battery.options(),
        main_fuse_a=_fuse_a(site.config),
        phased=bool(executor is not None and executor.control.phased),
        control=control_for(facts.brand),
    )
