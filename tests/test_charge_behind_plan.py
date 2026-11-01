"""A charge that falls behind its plan: the need is planned again during the plug-in and more is bought.

The plan counts on the charger's current times its phases (`planner.energy_per_slot_kwh`), or on the power the car
was learned to draw (`planning/learned_power.py`); a car that draws less (a cold battery, a limit in the car) falls
behind. Once a minute while a window is open, the energy delivered since it opened is compared with what the plan
expected by now (`planning/plan_watch.py`): 0.5 kWh or more behind, on two checks a minute or more apart, plans the
rest again, at most every five minutes, and a window that delivered less than 80 % of its share plans again at its
end. A shortfall SpotNav made itself (load balancing, a phase switch, a lower current) is planned again at the
window's end only. Each calculation counts from what was delivered (the estimate from the energy register for a
target, the counted energy for a manual need), so a car that drew half the plan's power gets more quarter-hours
bought, through the plan, with no reading from the car at all.

Before this rule nothing watched for it: the plan was made again only by chance, at the area's next hourly price poll
(`AreaPriceSnapshot.meaningful_key` moves with `next_attempt`), a new reading of the car's level, the car ending the
charge, a plug-in or a settings write.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning import plan_watch as plan_watch_module
from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC, TargetSocIntent
from custom_components.spotnav.planning.plan_watch import BehindCheck
from custom_components.spotnav.planning.planner import power_kw
from custom_components.spotnav.runtime import charger_data, preview_for
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY

from .relay import serve
from .test_dashboard_api import NOW
from .world import CAPACITY, REGISTER, charger_and_car, controller_of

pytestmark = pytest.mark.usefixtures("offline_relay")

#: What the plan counts on: 16 A on three phases.
PLANNED_KW = power_kw(16, 3)
#: What the car draws: half of it.
DRAWN_KW = PLANNED_KW / 2
#: 70 % to 80 % of the car's battery, from the wall.
TARGET_NEED_KWH = 0.10 * CAPACITY / CHARGE_EFFICIENCY
MANUAL_NEED_KWH = 8.0
ATTRIBUTES = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
STEP = timedelta(minutes=5)


def _record_starts_and_stops(hass: HomeAssistant) -> list[tuple[str, Any]]:
    """Every start and stop the charger receives, with the switch following it as a real one would."""
    calls: list[tuple[str, Any]] = []

    @callback
    def on_call(event: Any) -> None:
        service = event.data["service"]
        if service not in ("turn_on", "turn_off"):
            return
        targets = event.data.get("service_data", {}).get("entity_id", [])
        for entity_id in [targets] if isinstance(targets, str) else targets:
            calls.append((service, dt_util.utcnow()))
            hass.states.async_set(entity_id, "on" if service == "turn_on" else "off")

    hass.bus.async_listen("call_service", on_call)
    return calls


def _record_checks(preview: Any) -> list[BehindCheck]:
    """Every check the plan watch noted, in order."""
    checks: list[BehindCheck] = []
    watch = preview._watch  # noqa: SLF001
    check = watch.check

    def recording(now: datetime, facts: Any) -> str | None:
        result = check(now, facts)
        if watch.last_check is not None and watch.last_check.at == now:
            checks.append(watch.last_check)
        return result

    watch.check = recording
    return checks


def _plan_end(preview: Any) -> datetime | None:
    """Where the proposal's last period ends: later when more was bought after the slots already planned."""
    proposal = preview.snapshot().proposal
    if proposal is None or not proposal.has_plan or not proposal.periods:
        return None
    return proposal.periods[-1][1]


async def _charge(
    hass: HomeAssistant, frozen: Any, steps: int, register: float, kw: float, preview: Any, told: dict | None = None
) -> tuple[float, list[tuple[datetime, datetime | None]]]:
    """`steps` five-minute steps, the car drawing `kw` while the switch is on; the plan's end after each, and in
    `told` the plans told by then."""
    ends: list[tuple[datetime, datetime | None]] = []
    for _step in range(steps):
        frozen.tick(STEP)
        if hass.states.get("switch.wallbox").state == "on":
            register += kw * STEP.total_seconds() / 3600
        hass.states.async_set(REGISTER, f"{register:.3f}", ATTRIBUTES)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        ends.append((dt_util.utcnow(), _plan_end(preview)))
        if told is not None:
            told[dt_util.utcnow()] = charger_data(hass, preview.entry_id).notifier.plan_notice["seq"]
    return register, ends


@pytest.mark.parametrize(
    ("driver", "need_kwh"),
    [(DRIVER_TARGET_SOC, TARGET_NEED_KWH), (DRIVER_MANUAL_KWH, MANUAL_NEED_KWH)],
    ids=["target-estimated", "manual-kwh"],
)
async def test_a_car_drawing_half_the_planned_power_gets_more_quarter_hours_bought(
    hass: HomeAssistant, transport: Any, driver: str, need_kwh: float
) -> None:
    serve(transport, rising=True)
    calls = _record_starts_and_stops(hass)
    extra: dict[str, Any] = {"amps": 16, "phases": 3}
    if driver == DRIVER_MANUAL_KWH:
        extra.update(driver=DRIVER_MANUAL_KWH, requested_kwh=MANUAL_NEED_KWH, target=TargetSocIntent())
    with freeze_time(NOW) as frozen:
        charger, _, soc_entity = await charger_and_car(hass, soc_percent="70", **extra)
        controller = controller_of(hass, charger.entry_id)
        preview = preview_for(hass, charger.entry_id)
        checks = _record_checks(preview)
        told: dict[datetime, int] = {}
        plan = controller.plan
        assert plan is not None and plan.auto_owned
        first_end = plan.windows[-1][1]
        planned = sum((end - start).total_seconds() for start, end in plan.windows) / 3600 * PLANNED_KW
        assert need_kwh <= planned < need_kwh + PLANNED_KW / 4, "the plan bought just enough at the planned power"
        reading = hass.states.get(soc_entity)

        register, ends = await _charge(hass, frozen, 48, 1000.0, DRAWN_KW, preview, told)

        assert hass.states.get(soc_entity) == reading, "the car never reported its level"
        starts = [at for service, at in calls if service == "turn_on"]
        assert starts[0] == dt_util.parse_datetime("2026-09-22T06:00:00+00:00")
        behind = next(check for check in checks if check.decision == plan_watch_module.DECISION_BEHIND)
        assert behind.expected_kwh - behind.delivered_kwh >= plan_watch_module.BEHIND_KWH
        replans = [check.at for check in checks if check.decision == plan_watch_module.DECISION_REPLAN]
        assert replans and replans[0] - behind.at <= timedelta(minutes=5), "planned again once the shortfall held"
        grown = next(at for at, end in ends if end is not None and end > first_end)
        assert grown == replans[0], "and the replan bought more quarter-hours at once"
        assert all(later - earlier >= timedelta(minutes=5) for earlier, later in zip(replans, replans[1:]))
        assert register - 1000.0 >= need_kwh, "the need was delivered in the end"
        assert controller.plan is None and calls[-1][0] == "turn_off", "and the charge ended"
        snapshot = preview_for(hass, charger.entry_id).snapshot()
        assert snapshot.state == "nothing_to_charge"
        # A plan made again while a window runs is told to nobody (a plan installed after one ran out may be, as
        # before, on a charger that cannot report a plug-in).
        assert all(told[at - STEP] == told[at] == told[at + STEP] for at in replans if at - STEP in told), told


async def test_a_car_on_plan_is_not_planned_again(hass: HomeAssistant, transport: Any) -> None:
    serve(transport, rising=True)
    _record_starts_and_stops(hass)
    with freeze_time(NOW) as frozen:
        charger, _, _ = await charger_and_car(hass, soc_percent="70", amps=16, phases=3)
        preview = preview_for(hass, charger.entry_id)
        checks = _record_checks(preview)
        await _charge(hass, frozen, 12, 1000.0, PLANNED_KW, preview)
    kinds = {check.decision for check in checks}
    assert plan_watch_module.DECISION_ON_PLAN in kinds
    assert not kinds & {plan_watch_module.DECISION_REPLAN, plan_watch_module.DECISION_END_REPLAN}


class _HoldingSite:
    """A site whose load balancing holds the charger at 6 A."""

    def load_balancing_limit(self, _charger_entry_id: str) -> tuple[float | None, str | None]:
        return 6.0, None


async def test_a_shortfall_from_load_balancing_is_planned_again_at_the_window_end_only(
    hass: HomeAssistant, transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(transport, rising=True)
    _record_starts_and_stops(hass)
    monkeypatch.setattr(
        "custom_components.spotnav.planning.auto_controller.site_controller_for_charger",
        lambda _hass, _entry_id: _HoldingSite(),
    )
    with freeze_time(NOW) as frozen:
        charger, _, _ = await charger_and_car(hass, soc_percent="70", amps=16, phases=3)
        controller = controller_of(hass, charger.entry_id)
        preview = preview_for(hass, charger.entry_id)
        checks = _record_checks(preview)
        first_end = controller.plan.windows[-1][1]
        _register, ends = await _charge(hass, frozen, 24, 1000.0, power_kw(6, 3), preview)

    kinds = [check.decision for check in checks]
    assert plan_watch_module.DECISION_HELD in kinds, "the shortfall was seen and put down to load balancing"
    assert plan_watch_module.DECISION_REPLAN not in kinds, "and never planned again while a window ran"
    window_ends = [check for check in checks if check.decision == plan_watch_module.DECISION_END_REPLAN]
    assert window_ends, "but planned again after a window that fell short"
    assert all(check.window_end <= check.at <= check.window_end + STEP for check in window_ends), "just after it"
    after_end = dict(ends)[window_ends[0].at]
    assert after_end is not None and after_end > first_end, "and more is bought than the first plan had"
