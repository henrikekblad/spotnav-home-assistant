"""Cross-language contract fixtures for the dashboard (`tests/fixtures/dashboard/`).

The fixtures are read with no copy by `frontend/test/contract-fixtures.test.ts` (and by the Android
app's contract tests), covering the axes that matter: `cheapest`/`solar`/`hybrid` selected; a charger
with no site, a direct-mode site and a derived-mode site; an admin and a read-only caller; hybrid with
a forecast source configured and without one; the three price-wait states; a target charge. Every
fixture is the real `capture_dashboard` + `serialize_dashboard` output for a real charger (and, where
named, a real site) set up through Home Assistant's own config-entry system -- never a hand-built
payload. The control states are written by `tests/test_dashboard_control.py`, the two plan goldens by
`tests/test_dashboard_api.py`, and the target-charge ones by `tests/test_vehicle_properties.py`.

Set `SPOTNAV_WRITE_FIXTURES=1` to (re)write the committed files; the default run only compares, so a
payload change cannot slip past this test without the fixture moving with it.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution import hybrid_execution
from custom_components.spotnav.planning.auto_settings import (
    STRATEGY_CHEAPEST,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
)
from custom_components.spotnav.const import (
    CONF_GRID_POWER_SOURCE,
    CONF_MEASURED_CURRENT_SOURCE,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.planning.hybrid_forecast import ForecastReadResult
from custom_components.spotnav.site.measurement_source import (
    PhaseMeasurementSource,
    source_to_dict,
)
from custom_components.spotnav.site.site_capacity import PHASES
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController
from tests.helpers import make_entry, make_site_entry
from tests.test_dashboard_api import LATE, NOW
from tests.world import go_auto
from tests.relay import serve
from tests.relay import cheap_night_day
from tests.relay import TODAY
from tests.relay import SE4
from .world import controller_of
from custom_components.spotnav.runtime import domain_data

pytestmark = pytest.mark.usefixtures("offline_relay")

DASHBOARD_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "dashboard"

VOLTAGE_V: Final = 230.0


def _forecast_entry(hass: HomeAssistant, *, entry_id: str, title: str) -> MockConfigEntry:
    """One config entry of a fake Energy-dashboard-capable domain -- see
    `tests.test_hybrid_config_flow`'s identical `_forecast_entry`."""
    entry = MockConfigEntry(domain="roof", entry_id=entry_id, title=title)
    entry.add_to_hass(hass)
    return entry


def _derived_entities(hass: HomeAssistant, prefix: str, watts: float) -> dict[str, dict[str, str]]:
    """The three derived-mode power/reactive/voltage entities per phase, all live -- the same
    shape `tests.test_solar_execution.set_derived_site_entities` builds."""
    derived_entities = {
        phase: {
            "power": f"sensor.{prefix}_power_{phase.lower()}",
            "reactive_power": f"sensor.{prefix}_reactive_{phase.lower()}",
            "voltage": f"sensor.{prefix}_voltage_{phase.lower()}",
        }
        for phase in PHASES
    }
    for phase in PHASES:
        hass.states.async_set(
            derived_entities[phase]["power"], str(watts), {"unit_of_measurement": "W"}
        )
        hass.states.async_set(
            derived_entities[phase]["reactive_power"], "0", {"unit_of_measurement": "var"}
        )
        hass.states.async_set(
            derived_entities[phase]["voltage"], str(VOLTAGE_V), {"unit_of_measurement": "V"}
        )
    return derived_entities


async def _setup_charger(hass: HomeAssistant, *, entry_id: str) -> MockConfigEntry:
    charge_control = f"switch.{entry_id}"
    hass.states.async_set(charge_control, "off")
    charger = make_entry(
        hass,
        entry_id=entry_id,
        charge_control=charge_control,
        current_limit=None,
        webhook_id=f"webhook-{entry_id}",
        title=entry_id,
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    return charger


async def _setup_derived_site(
    hass: HomeAssistant, *, site_entry_id: str, charger_entry_id: str, **site_kwargs: Any
) -> MockConfigEntry:
    """A real, healthy derived-mode site listing one charger -- the minimum wiring
    `tests.test_solar_execution.solar_setup` builds, trimmed to what a dashboard capture reads."""
    prefix = charger_entry_id
    derived_entities = _derived_entities(hass, site_entry_id, 0.0)
    for phase in PHASES:
        hass.states.async_set(
            f"sensor.{prefix}_{phase.lower()}", "0", {"unit_of_measurement": "A"}
        )
    site = make_site_entry(
        hass,
        entry_id=site_entry_id,
        charger_entry_ids=[charger_entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
        phase_wiring={
            charger_entry_id: {
                "phases": 3,
                "phase": None,
                "min_current_a": 6.0,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="separate_entities",
                        entity_ids={p: f"sensor.{prefix}_{p.lower()}" for p in PHASES},
                    )
                ),
            }
        },
        **site_kwargs,
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    # One explicit recompute: a real, healthy reading (no surplus, nothing drawn) rather than
    # the "nothing observed yet" defaults a site that has never ticked would report -- the same
    # explicit-tick technique `tests.test_solar_execution.tick_site` uses, for the same reason (no
    # sleep, no dependence on the 30 s timer).
    controller: SiteCapacityController = controller_of(hass, site.entry_id)
    controller._recompute()  # noqa: SLF001 - the reviewed explicit-tick technique
    await hass.async_block_till_done()
    return site


async def _payload(
    hass: HomeAssistant, entry: MockConfigEntry, *, can_act: bool, forecast_domains: frozenset[str] = frozenset()
) -> dict[str, Any]:
    capture = dashboard_api.capture_dashboard(hass, entry, forecast_domains=forecast_domains)
    return dashboard_api.serialize_dashboard(capture, can_act=can_act)


async def _state_cheapest_no_site(hass: HomeAssistant) -> dict[str, Any]:
    """`cheapest` selected, no site at all: `strategy_state` and `site` are both `null`."""
    charger = await _setup_charger(hass, entry_id="cheapest_no_site")
    await go_auto(hass, charger.entry_id, strategy=STRATEGY_CHEAPEST)
    return await _payload(hass, charger, can_act=True)


async def _state_charger_states_its_maximum(hass: HomeAssistant) -> dict[str, Any]:
    """The charger's own current-limit number states `max: 16`: `current_range` reports 16 A and
    says where it came from (every other fixture has no such entity and reads the 32 A default)."""
    hass.states.async_set(
        "number.limited_charger_limit",
        "10",
        {"unit_of_measurement": "A", "min": 6, "max": 16},
    )
    hass.states.async_set("switch.limited_charger", "off")
    charger = make_entry(
        hass,
        entry_id="limited_charger",
        charge_control="switch.limited_charger",
        current_limit="number.limited_charger_limit",
        webhook_id="webhook-limited_charger",
        title="limited_charger",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    await go_auto(hass, charger.entry_id, strategy=STRATEGY_CHEAPEST)
    return await _payload(hass, charger, can_act=True)


async def _direct_site_charger(hass: HomeAssistant, *, entry_id: str) -> MockConfigEntry:
    charger = await _setup_charger(hass, entry_id=entry_id)
    site = make_site_entry(hass, entry_id=f"{entry_id}_site", charger_entry_ids=[charger.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    await go_auto(hass, charger.entry_id, strategy=STRATEGY_CHEAPEST)
    return charger


async def _state_cheapest_direct_site_admin(hass: HomeAssistant) -> dict[str, Any]:
    """`cheapest` selected, a direct-mode site: `strategy_state` is `null` (cheapest has nothing
    of its own to report); `site` is populated, `writable: true` for an admin caller."""
    charger = await _direct_site_charger(hass, entry_id="cheapest_direct_admin")
    return await _payload(hass, charger, can_act=True)


async def _state_cheapest_direct_site_read_only(hass: HomeAssistant) -> dict[str, Any]:
    """The identical state, read by a non-admin: `site.writable` (and `control.can_act`) flip to
    `false`, and nothing else about the capture changes."""
    charger = await _direct_site_charger(hass, entry_id="cheapest_direct_reader")
    return await _payload(hass, charger, can_act=False)


async def _set_strategy(hass: HomeAssistant, entry_id: str, strategy: str) -> None:
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(entry_id, mutate=lambda s: replace(s, strategy=strategy))


async def _state_solar_derived_site(hass: HomeAssistant) -> dict[str, Any]:
    """`solar` selected, a healthy derived-mode site: `strategy_state.solar` is the real,
    ticked `solar_surplus_snapshot` row for this charger (no surplus configured, so `state`
    reads `off` rather than the placeholder `unknown`).

    The charger's strategy is set to `solar` *before* the site is set up -- the site's own
    setup is what binds `SolarExecutionCoordinator` to each member charger
    (`async_rebind_solar_execution`), and it reads each charger's *current* strategy to decide
    whether to bind one at all (see `tests.test_solar_execution.solar_setup`'s own comment on
    this exact ordering).
    """
    charger = await _setup_charger(hass, entry_id="solar_derived")
    await _set_strategy(hass, charger.entry_id, STRATEGY_SOLAR)
    await _setup_derived_site(hass, site_entry_id="solar_derived_site", charger_entry_id=charger.entry_id)
    return await _payload(hass, charger, can_act=True)


async def _state_solar_direct_site_with_total(hass: HomeAssistant) -> dict[str, Any]:
    """`solar` selected on a direct-mode site (per-phase current only) that carries the meter's total grid
    power: `solar` and `hybrid` are offered, and `strategy_state.solar` is the real, ticked row.
    Strategy before site, for the ordering reason `_state_solar_derived_site` documents."""
    charger = await _setup_charger(hass, entry_id="solar_direct")
    await _set_strategy(hass, charger.entry_id, STRATEGY_SOLAR)
    prefix = charger.entry_id
    for phase in PHASES:
        hass.states.async_set(f"sensor.{prefix}_{phase.lower()}", "0", {"unit_of_measurement": "A"})
        hass.states.async_set(
            f"sensor.solar_direct_site_{phase.lower()}", "3", {"unit_of_measurement": "A"}
        )
    hass.states.async_set("sensor.solar_direct_grid_power", "0", {"unit_of_measurement": "W"})
    site = make_site_entry(
        hass,
        entry_id="solar_direct_site",
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                "min_current_a": 6.0,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="separate_entities",
                        entity_ids={p: f"sensor.{prefix}_{p.lower()}" for p in PHASES},
                    )
                ),
            }
        },
        extra_data={CONF_GRID_POWER_SOURCE: {"power": "sensor.solar_direct_grid_power"}},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    controller: SiteCapacityController = controller_of(hass, site.entry_id)
    controller._recompute()  # noqa: SLF001 - the reviewed explicit-tick technique
    await hass.async_block_till_done()
    return await _payload(hass, charger, can_act=True)


async def _state_hybrid_derived_site_no_forecast(hass: HomeAssistant) -> dict[str, Any]:
    """`hybrid` selected, a healthy derived-mode site, no forecast source named anywhere:
    `strategy_state.hybrid.forecast_configured` is `false`. Strategy set before the site, for
    the same ordering reason `_state_solar_derived_site` documents."""
    charger = await _setup_charger(hass, entry_id="hybrid_no_forecast")
    await _set_strategy(hass, charger.entry_id, STRATEGY_HYBRID)
    await _setup_derived_site(
        hass, site_entry_id="hybrid_no_forecast_site", charger_entry_id=charger.entry_id
    )
    await go_auto(
        hass,
        charger.entry_id,
        amps=10,
        phases=3,
        requested_kwh=20.0,
        departure_enabled=False,
    )
    return await _payload(hass, charger, can_act=True)


async def _state_hybrid_derived_site_with_forecast(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The identical shape, with one forecast source configured and genuinely readable:
    `strategy_state.hybrid.forecast_configured` is `true`, and `site.solar_forecast.selected`
    names it.

    Hybrid only ever reads a forecast when the remaining need itself is trustworthy
    (`auto_controller.AutoPlannerController._manual_kwh_remaining`'s own docstring) -- for the
    `manual_kwh` driver this fixture otherwise uses, that requires a real cumulative-energy
    register, so one is wired the same way `tests.test_hybrid_wiring` wires it for the same
    reason (`controller.energy_register_entity_id = register`, a real, readable `total_increasing`
    energy sensor), rather than the *no*-forecast fixture leaving that gate closed on purpose.
    """
    roof = _forecast_entry(hass, entry_id="roof_hybrid", title="Roof (hybrid)")
    charger = await _setup_charger(hass, entry_id="hybrid_with_forecast")
    await _set_strategy(hass, charger.entry_id, STRATEGY_HYBRID)
    await _setup_derived_site(
        hass,
        site_entry_id="hybrid_with_forecast_site",
        charger_entry_id=charger.entry_id,
        solar_forecast_entries=[roof.entry_id],
    )
    register = "sensor.hybrid_with_forecast_energy_register"
    hass.states.async_set(
        register,
        "0.0",
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )
    controller_of(hass, charger.entry_id).energy_register_entity_id = register

    async def _fake_read(_hass: HomeAssistant, entry_ids: Any) -> ForecastReadResult:
        return ForecastReadResult(wh_hours={}, sources_read=tuple(entry_ids))

    monkeypatch.setattr(hybrid_execution, "async_read_forecast_wh", _fake_read)
    await go_auto(
        hass,
        charger.entry_id,
        amps=10,
        phases=3,
        requested_kwh=20.0,
        departure_enabled=False,
    )
    return await _payload(hass, charger, can_act=True, forecast_domains=frozenset({"roof"}))


Builder = Callable[..., Awaitable[dict[str, Any]]]


@freeze_time(NOW)
async def test_the_committed_strategy_and_site_fixtures_are_the_serializers_own_output(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, offline_relay: None
) -> None:
    """Every strategy/site fixture equals what the real capture and serializer produce."""
    produced: dict[str, Any] = {
        "cheapest_no_site.json": await _state_cheapest_no_site(hass),
        "cheapest_direct_site_admin.json": await _state_cheapest_direct_site_admin(hass),
        "charger_states_its_maximum.json": await _state_charger_states_its_maximum(hass),
        "cheapest_direct_site_read_only.json": await _state_cheapest_direct_site_read_only(hass),
        "solar_derived_site.json": await _state_solar_derived_site(hass),
        "hybrid_derived_site_no_forecast.json": await _state_hybrid_derived_site_no_forecast(hass),
        "hybrid_derived_site_with_forecast.json": await _state_hybrid_derived_site_with_forecast(
            hass, monkeypatch
        ),
        # Last: a charger set up earlier shows in every later payload's `chargers` list.
        "solar_direct_site_with_total.json": await _state_solar_direct_site_with_total(hass),
    }
    for name, payload in produced.items():
        assert payload["api_version"] == 1, name
        assert "strategy_state" in payload and "site" in payload, name

    write = os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1"
    if write:
        for name, payload in produced.items():
            (DASHBOARD_FIXTURE_DIR / name).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return

    for name, payload in produced.items():
        stored_fixture = json.loads((DASHBOARD_FIXTURE_DIR / name).read_text(encoding="utf-8"))
        assert stored_fixture == payload, f"{name} no longer matches the serializer's own output"


#: Every file the fixture directory holds, and which module writes it. A fixture nobody writes any
#: more is stale, and this list is what says so.
EXPECTED_FIXTURES: Final = frozenset(
    {
        # This module: strategy, site and the price-wait states.
        "cheapest_no_site.json",
        "cheapest_direct_site_admin.json",
        "cheapest_direct_site_read_only.json",
        "charger_states_its_maximum.json",
        "solar_derived_site.json",
        "solar_direct_site_with_total.json",
        "hybrid_derived_site_no_forecast.json",
        "hybrid_derived_site_with_forecast.json",
        "waiting_for_publication.json",
        "waiting_for_history.json",
        "buying_before_publication.json",
        "charging_without_prices.json",
        # This module: a target charge with an estimated state of charge, and one that ended on it.
        "target_soc_estimated.json",
        "target_soc_stopped_on_estimate.json",
        # `tests/test_vehicle_properties.py`: `vehicles`, `target_vehicle_id`, `soc.efficiency`.
        "target_soc_two_vehicles.json",
        # `tests/test_phases.py`: a one-phase car limiting a three-phase charger.
        "target_soc_phases_limited_by_vehicle.json",
        # `tests/test_dashboard_api.py`: the two plan goldens.
        "proposal_ready.json",
        "pending_beside_installed.json",
        # `tests/test_dashboard_control.py`: one per control state.
        "start_idle.json",
        "stop_charging.json",
        "resume_active.json",
        "pause_clear_failed.json",
        "action_pending.json",
        "manual_stop.json",
        "no_settings.json",
    }
)


def test_no_dashboard_fixture_is_stale_or_missing() -> None:
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        return
    assert sorted(path.name for path in DASHBOARD_FIXTURE_DIR.glob("*.json")) == sorted(
        EXPECTED_FIXTURES
    )


async def _price_wait_payload(
    hass: HomeAssistant, transport: Any, *, rising: bool = False, **changes: Any
) -> dict[str, Any]:
    """One charger, today published and tomorrow not, planned with a departure at 08:00.

    `rising`: the day's expensive hours get dearer by the quarter, so a plan that must buy now starts
    at the clock rather than at the end of the day (equal prices go to the latest slots).
    """
    charger = await _setup_charger(hass, entry_id="price_wait")
    serve(transport, days=(TODAY,), listed=(TODAY,))
    transport.serve(transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY, rising=rising))
    await go_auto(
        hass,
        charger.entry_id,
        strategy=STRATEGY_CHEAPEST,
        departure_enabled=True,
        departure=time(8, 0),
        **changes,
    )
    return await _payload(hass, charger, can_act=True)


def _write_or_compare(produced: dict[str, Any]) -> None:
    DASHBOARD_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in produced.items():
        path = DASHBOARD_FIXTURE_DIR / name
        if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            continue
        assert json.loads(path.read_text(encoding="utf-8")) == payload, (
            f"{name} no longer matches the serializer's own output"
        )


@freeze_time(NOW)
async def test_the_price_wait_fixtures_waiting_and_buying_are_the_serializers_own_output(
    hass: HomeAssistant, transport: Any, offline_relay: None
) -> None:
    waiting = await _price_wait_payload(hass, transport)
    planning = waiting["planning"]
    assert planning["state"] == "waiting_for_publication" and planning["reason"] == "publication_pending"
    assert planning["price_wait"] == "waiting"
    assert planning["publication_at"] == "2026-09-22T11:45:00+00:00"
    assert planning["must_buy_now_kwh"] == 0.0
    assert waiting["plan"]["proposal"] is None

    _write_or_compare({"waiting_for_publication.json": waiting})


#: Thursday evening in Stockholm (CEST): tomorrow's prices are out, the departure is on Sunday at 08:00.
THURSDAY_EVENING = "2026-10-01 18:00:00"


@freeze_time(THURSDAY_EVENING)
async def test_the_history_wait_fixture_is_the_serializers_own_output(
    hass: HomeAssistant, transport: Any, offline_relay: None
) -> None:
    """A dated departure whose unpublished weekend hours the history says are cheaper: waiting, with its
    weekday, saving and weeks as typed status params, and the date carried in the settings record."""
    from datetime import date, timedelta

    from tests.relay import flat_day, index_listing, serve_profile

    charger = await _setup_charger(hass, entry_id="history_wait")
    today = date(2026, 10, 1)
    days = (today, today + timedelta(days=1))
    serve(transport, days=days, listed=days)
    for day in days:
        transport.serve(transport.day_path(SE4, day), 200, flat_day(SE4, day, price=0.10))
    transport.serve("/v1/index.json", 200, index_listing(SE4, [day.isoformat() for day in days]))
    serve_profile(
        transport, to="2026-10-01", generated="2026-10-01T14:05:00+02:00",
        cheap={(7, hour): 0.03 for hour in range(8)},
    )
    await go_auto(
        hass,
        charger.entry_id,
        strategy=STRATEGY_CHEAPEST,
        departure_enabled=True,
        departure=time(8, 0),
        departure_date=date(2026, 10, 4),
        requested_kwh=10.0,
    )
    waiting = await _payload(hass, charger, can_act=True)

    planning = waiting["planning"]
    assert planning["state"] == "waiting_for_publication" and planning["reason"] == "waiting_for_history"
    assert waiting["settings"]["departure_date"] == "2026-10-04"
    line = waiting["status"]["lines"][0]
    assert line["code"] == "waiting_for_history"
    assert line["params"]["weekday"] == 7 and line["params"]["weeks"] == 4
    assert isinstance(line["params"]["percent"], int) and line["params"]["percent"] > 0
    assert waiting["plan"]["proposal"] is None

    _write_or_compare({"waiting_for_history.json": waiting})


@freeze_time(NOW)
async def test_the_price_wait_fixtures_buying_is_the_serializers_own_output(
    hass: HomeAssistant, transport: Any, offline_relay: None
) -> None:
    buying = await _price_wait_payload(hass, transport, requested_kwh=40.0, rising=True)
    planning = buying["planning"]
    assert planning["state"] == "proposal_ready" and planning["reason"] == "buying_before_publication"
    assert planning["price_wait"] == "buy_now" and planning["must_buy_now_kwh"] > 0
    assert buying["plan"]["proposal"]["unpriced"] is False

    _write_or_compare({"buying_before_publication.json": buying})


@freeze_time(LATE)
async def test_the_price_wait_fixtures_guarantee_is_the_serializers_own_output(
    hass: HomeAssistant, transport: Any, offline_relay: None
) -> None:
    guarantee = await _price_wait_payload(hass, transport)
    planning = guarantee["planning"]
    assert planning["state"] == "proposal_unpriced" and planning["reason"] == "charging_without_prices"
    assert planning["price_wait"] == "guarantee"
    assert guarantee["plan"]["proposal"]["priced_slots"] == 0

    _write_or_compare({"charging_without_prices.json": guarantee})


@freeze_time(NOW)
async def test_the_target_soc_fixture_is_the_serializers_own_output(
    hass: HomeAssistant, offline_relay: None
) -> None:
    """A target-SoC charger whose car polled two hours ago and whose meter has moved 30 kWh
    since -- an *estimated* state of charge, and `target_soc` capable."""
    from datetime import timedelta

    from tests.world import REGISTER, charger_and_car

    charger, _, _ = await charger_and_car(hass)
    with freeze_time(NOW) as frozen:
        frozen.tick(timedelta(hours=2))
        hass.states.async_set(
            REGISTER,
            "1030.0",
            {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
        )
        payload = await _payload(hass, charger, can_act=True)
    assert payload["charger"]["capabilities"]["target_soc"] is True
    assert payload["soc"]["estimated"] is True and payload["soc"]["source"] == "vehicle"
    # The installed plan's identity covers the vehicle's (random) device id: pin every 32-hex id.
    import re

    payload = json.loads(re.sub(r'"[0-9a-f]{32}"', '"<id>"', json.dumps(payload)))
    _write_or_compare({"target_soc_estimated.json": payload})


@freeze_time(NOW)
async def test_the_target_reached_on_an_estimate_fixture_is_the_serializers_own_output(
    hass: HomeAssistant, offline_relay: None
) -> None:
    """A target-SoC charge that the meter alone ended (the car's last poll 30 minutes old): the
    composed `status` carries `target_reached{basis: estimate}` after the headline."""
    import re
    from tests.world import real_controller_stop

    with freeze_time(NOW) as frozen:
        # `real_controller_stop` sets up the car, installs the plan, ticks 30 min and moves the meter.
        await real_controller_stop(hass, frozen)
        charger = hass.config_entries.async_get_entry("soc_charger")
        assert charger is not None
        payload = await _payload(hass, charger, can_act=True)
    reached = [line for line in payload["status"]["lines"] if line["code"] == "target_reached"]
    assert len(reached) == 1 and reached[0]["params"]["basis"] == "estimate"
    assert payload["status"]["lines"][0]["code"] != "target_reached"
    payload = json.loads(re.sub(r'"[0-9a-f]{32}"', '"<id>"', json.dumps(payload)))
    _write_or_compare({"target_soc_stopped_on_estimate.json": payload})
