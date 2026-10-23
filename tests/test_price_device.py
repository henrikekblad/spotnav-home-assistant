"""The electricity price entry: its flow, its device's entities, the `get_prices` action, and its lifetime.

Real config entries on the offline relay (SE4, today 2026-09-22 and tomorrow published), at a frozen
clock: 06:00 UTC is 08:00 in Stockholm, the 33rd quarter of the day.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
    MockConfigEntry,
)

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.common import ERROR_UNKNOWN_CHARGER, lookup_charger
from custom_components.spotnav.const import DOMAIN, ENTRY_TYPE_PRICE
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.planning.auto_settings import AreaAutoSettings, FiscalOverride
from custom_components.spotnav.pricing.price_points import CHEAP, EXPENSIVE
from custom_components.spotnav.runtime import charger_data, domain_data, price_data

from .relay import fixture, serve, SE4, TODAY
from .world import go_auto, setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

NOW = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
FX = 11.275
SUGGESTED = {"enabled": True, "value": None}


def today_prices() -> list[float]:
    return json.loads(fixture("day_SE4_2026-09-22_96.json"))["prices"]


def effective(eur: float, *, tax: float = 36, transfer: float = 30, vat: float = 25) -> float:
    """SE4's price with its suggested fees, in kr/kWh: (spot öre + tax + grid fee) x (1 + VAT)."""
    return (eur * FX * 100 + tax + transfer) * (1 + vat / 100) / 100


async def add_price(
    hass: HomeAssistant,
    *,
    entry_id: str = "price_a",
    area: str = SE4,
    title: str | None = None,
    **fees: Any,
) -> MockConfigEntry:
    data = {"entry_type": ENTRY_TYPE_PRICE, "area_id": area, "vat": SUGGESTED, "tax": SUGGESTED, "transfer": SUGGESTED}
    data.update(fees)
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id, title=title or f"Electricity price {area}", data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity(hass: HomeAssistant, entry_id: str, key: str, platform: str) -> str:
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry_id}_{key}")
    assert found is not None, key
    return found


# ------------------------------------------------------------------ the flow


async def test_the_flow_adds_an_electricity_price_with_the_areas_suggestions(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    hass.config.country = "SE"
    hass.config.latitude, hass.config.longitude = 55.6, 13.0
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "price"})
    assert result["step_id"] == "price"
    # Home Assistant's own place picks the area to start from (Malmö is SE4).
    schema = result["data_schema"].schema
    assert next(key for key in schema if key == "area_id").default() == SE4

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"area_id": SE4})
    assert result["step_id"] == "price_fees"
    assert result["description_placeholders"]["tax_suggestion"] == "36 öre/kWh"
    defaults = {str(key): key.default() for key in result["data_schema"].schema if hasattr(key, "default") and callable(key.default)}
    assert defaults == {"vat_mode": "suggested", "tax_mode": "suggested", "transfer_mode": "suggested"}

    # "My own figure" with no figure is sent back.
    refused = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"vat_mode": "suggested", "tax_mode": "suggested", "transfer_mode": "manual"}
    )
    assert refused["errors"] == {"transfer_value": "fee_value_required"}
    done = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"vat_mode": "suggested", "tax_mode": "off", "transfer_mode": "manual", "transfer_value": 22.5},
    )
    await hass.async_block_till_done()

    assert done["type"] is FlowResultType.CREATE_ENTRY and done["title"] == "Electricity price SE4"
    assert done["data"] == {
        "entry_type": "price",
        "area_id": SE4,
        "vat": {"enabled": True, "value": None},
        "tax": {"enabled": False, "value": None},
        "transfer": {"enabled": True, "value": 22.5},
    }
    entry = hass.config_entries.async_get_entry(done["result"].entry_id)
    state = hass.states.get(entity(hass, entry.entry_id, "price_now", "sensor"))
    assert state.state == str(round((today_prices()[32] * FX * 100 + 22.5) * 1.25 / 100, 4))


async def test_a_suggestion_the_area_lacks_cannot_be_chosen(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "price"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"area_id": "DE-LU"})
    defaults = {str(key): key.default() for key in result["data_schema"].schema if hasattr(key, "default") and callable(key.default)}
    # Germany publishes no suggestion: every fee starts at nothing.
    assert defaults == {"vat_mode": "off", "tax_mode": "off", "transfer_mode": "off"}

    refused = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"vat_mode": "suggested", "tax_mode": "off", "transfer_mode": "off"}
    )

    assert refused["errors"] == {"vat_mode": "fee_no_suggestion"}


async def test_several_electricity_prices_live_side_by_side(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    first = await add_price(hass)
    second = await add_price(hass, entry_id="price_b", title="Bathroom floor", tax={"enabled": False, "value": None})

    one = float(hass.states.get(entity(hass, first.entry_id, "price_now", "sensor")).state)
    other = float(hass.states.get(entity(hass, second.entry_id, "price_now", "sensor")).state)

    assert one == pytest.approx(effective(today_prices()[32]), abs=1e-4)
    assert other == pytest.approx(effective(today_prices()[32], tax=0), abs=1e-4)
    # One area, one stream: both are owners of the same subscription.
    manager = domain_data(hass).price_refresh
    assert manager.owner_area("price:price_a") == manager.owner_area("price:price_b") == SE4
    assert manager.area_snapshot(SE4).subscriber_count == 2


async def test_configure_changes_the_area_and_fees_and_a_given_name_follows_the_area(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "price_init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area_id": "NO1"})
    assert result["step_id"] == "price_fees"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"vat_mode": "suggested", "tax_mode": "manual", "tax_value": 9.5, "transfer_mode": "off"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.title == "Electricity price NO1"
    assert entry.data["area_id"] == "NO1" and entry.data["tax"] == {"enabled": True, "value": 9.5}
    manager = domain_data(hass).price_refresh
    assert manager.owner_area("price:price_a") == "NO1"


# ------------------------------------------------------------------ the entities


async def test_price_now_is_this_quarters_price_with_fees_and_small_unrecorded_attributes(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)

    state = hass.states.get(entity(hass, entry.entry_id, "price_now", "sensor"))

    assert state.entity_id == "sensor.electricity_price_se4_price_now"
    assert float(state.state) == round(effective(today_prices()[32]), 4)
    assert state.attributes["unit_of_measurement"] == "kr/kWh"
    assert state.attributes["state_class"] == "measurement"
    assert {key: state.attributes[key] for key in ("area", "currency", "resolution_minutes", "tomorrow_published")} == {
        "area": SE4,
        "currency": "SEK",
        "resolution_minutes": 15,
        "tomorrow_published": True,
    }
    assert state.attributes["spot_price"] == round(today_prices()[32] * FX, 4)
    assert dt_util.parse_datetime(state.attributes["next_change"]) == NOW + timedelta(minutes=15)
    sensor = hass.data["sensor"].get_entity(state.entity_id)
    assert sensor._unrecorded_attributes >= {"spot_price", "next_change", "area", "tomorrow_published"}
    assert not any(isinstance(value, (list, dict)) for value in state.attributes.values())


async def test_price_now_moves_on_exactly_at_the_quarters_boundary(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW + timedelta(minutes=14, seconds=59))
    entry = await add_price(hass)
    entity_id = entity(hass, entry.entry_id, "price_now", "sensor")
    before = hass.states.get(entity_id)
    assert float(before.state) == round(effective(today_prices()[32]), 4)

    boundary = NOW + timedelta(minutes=15)
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()

    after = hass.states.get(entity_id)
    assert float(after.state) == round(effective(today_prices()[33]), 4)
    assert after.last_reported == boundary


async def test_price_now_is_unavailable_without_a_price_for_now(hass: HomeAssistant, freezer, offline_relay) -> None:
    # Two days on: nothing the relay serves covers it.
    freezer.move_to(NOW + timedelta(days=2))
    serve(offline_relay, days=())
    entry = await add_price(hass)

    assert hass.states.get(entity(hass, entry.entry_id, "price_now", "sensor")).state == "unavailable"
    assert hass.states.get(entity(hass, entry.entry_id, "cheap_now", "binary_sensor")).state == "unavailable"


async def test_the_fees_are_a_chargers_with_the_same_settings(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    charger = await setup_charger(hass)
    fees = AreaAutoSettings(
        area_id=SE4,
        vat=FiscalOverride(enabled=True),
        tax=FiscalOverride(enabled=True, value=42.0),
        transfer=FiscalOverride(enabled=False, value=7.0),
    )
    await go_auto(hass, overrides=(fees,))
    entry = await add_price(
        hass, tax={"enabled": True, "value": 42.0}, transfer={"enabled": False, "value": None}
    )

    rows = dashboard_api.capture_dashboard(hass, charger).intervals
    by_start = {point.start: point.price for point in price_data(hass, entry.entry_id).device.points}

    assert len(rows) == 192 and len(by_start) == 192
    for row in rows:
        assert by_start[row.utc_start] * 100 == pytest.approx(row.effective_minor_per_kwh)


async def test_cheap_and_expensive_follow_their_hours(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)
    device = price_data(hass, entry.entry_id).device
    cheap = entity(hass, entry.entry_id, "cheap_now", "binary_sensor")
    expensive = entity(hass, entry.entry_id, "expensive_now", "binary_sensor")
    cheap_hours = entity(hass, entry.entry_id, "cheap_hours", "number")
    assert cheap_hours == "number.electricity_price_se4_cheap_hours_per_day"
    assert hass.states.get(cheap_hours).state == "6.0"
    assert hass.states.get(entity(hass, entry.entry_id, "expensive_hours", "number")).state == "4.0"

    # The ranking the sensors show, worked out here from today's prices: ties to the earlier quarter.
    prices = today_prices()
    order = sorted(range(96), key=lambda index: (prices[index], index))
    assert (hass.states.get(cheap).state == "on") == (32 in order[:24])
    assert (hass.states.get(expensive).state == "on") == (32 in order[-16:])
    attributes = hass.states.get(cheap).attributes
    assert attributes["threshold"] == round(effective(prices[order[23]]), 4)
    assert device.is_on(CHEAP) is (32 in order[:24])

    await hass.services.async_call("number", "set_value", {"entity_id": cheap_hours, "value": 24}, blocking=True)
    assert hass.states.get(cheap).state == "on"
    await hass.services.async_call("number", "set_value", {"entity_id": cheap_hours, "value": 0}, blocking=True)
    assert hass.states.get(cheap).state == "off"
    assert hass.states.get(cheap).attributes["threshold"] is None
    # A quarter more than the price of now's rank: on exactly from that rank.
    rank_now = order.index(32)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": cheap_hours, "value": (rank_now + 1) / 4}, blocking=True
    )
    assert hass.states.get(cheap).state == "on"
    await hass.services.async_call("number", "set_value", {"entity_id": cheap_hours, "value": rank_now / 4}, blocking=True)
    assert hass.states.get(cheap).state == "off"
    assert device.hours(EXPENSIVE) == 4.0


async def test_the_hours_are_restored_after_a_restart(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("number.electricity_price_se4_cheap_hours_per_day", "2.25"),
                {"native_value": 2.25, "native_min_value": 0, "native_max_value": 24, "native_step": 0.25, "native_unit_of_measurement": "h"},
            ),
            (
                State("number.electricity_price_se4_expensive_hours_per_day", "7.5"),
                {"native_value": 7.5, "native_min_value": 0, "native_max_value": 24, "native_step": 0.25, "native_unit_of_measurement": "h"},
            ),
        ],
    )
    entry = await add_price(hass)
    device = price_data(hass, entry.entry_id).device

    assert device.hours(CHEAP) == 2.25 and device.hours(EXPENSIVE) == 7.5
    assert len(device.ranking(CHEAP).chosen) == 9 * 2  # nine quarters today and nine tomorrow
    assert hass.states.get("number.electricity_price_se4_cheap_hours_per_day").state == "2.25"


# ------------------------------------------------------------------ the action


async def test_get_prices_answers_today_and_tomorrow_for_any_user(
    hass: HomeAssistant, freezer, hass_read_only_user: Any
) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)

    response = await hass.services.async_call(
        DOMAIN,
        "get_prices",
        {"config_entry_id": entry.entry_id},
        blocking=True,
        return_response=True,
        context=Context(user_id=hass_read_only_user.id),
    )

    assert {key: response[key] for key in response if key != SE4} == {
        "config_entry_id": entry.entry_id,
        "area": SE4,
        "currency": "SEK",
        "unit": "kr/kWh",
        "resolution_minutes": 15,
        "tomorrow_published": True,
    }
    rows = response[SE4]
    assert len(rows) == 192
    assert rows[0] == {
        "start": "2026-09-22T00:00:00+02:00",
        "end": "2026-09-22T00:15:00+02:00",
        "price": round(effective(today_prices()[0]), 4),
        "spot_price": round(today_prices()[0] * FX, 4),
    }
    assert rows[-1]["end"] == "2026-09-24T00:00:00+02:00"


async def test_get_prices_without_tomorrow_and_by_device_and_span(hass: HomeAssistant, freezer, offline_relay) -> None:
    freezer.move_to(NOW)
    serve(offline_relay, days=(TODAY,))
    entry = await add_price(hass)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)

    response = await hass.services.async_call(
        DOMAIN, "get_prices", {"device_id": device.id}, blocking=True, return_response=True
    )
    assert response["tomorrow_published"] is False and len(response[SE4]) == 96
    assert response[SE4][-1]["end"] == "2026-09-23T00:00:00+02:00"
    assert hass.states.get(entity(hass, entry.entry_id, "price_now", "sensor")).attributes["tomorrow_published"] is False

    span = await hass.services.async_call(
        DOMAIN,
        "get_prices",
        {"start": "2026-09-22T08:00:00+02:00", "end": "2026-09-22T09:00:00+02:00"},
        blocking=True,
        return_response=True,
    )
    assert [row["start"][11:16] for row in span[SE4]] == ["08:00", "08:15", "08:30", "08:45"]


async def test_get_prices_refuses_what_it_cannot_answer(hass: HomeAssistant, freezer) -> None:
    from homeassistant.exceptions import ServiceValidationError

    freezer.move_to(NOW)
    charger = await setup_charger(hass)
    await add_price(hass)
    await add_price(hass, entry_id="price_b")

    for data, key in (
        ({}, "price_entry_required"),
        ({"config_entry_id": charger.entry_id}, "price_entry_unknown"),
        ({"config_entry_id": "price_a", "start": "2026-09-22T10:00:00+02:00", "end": "2026-09-22T09:00:00+02:00"}, "price_range_invalid"),
    ):
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(DOMAIN, "get_prices", data, blocking=True, return_response=True)
        assert raised.value.translation_key == key


# ------------------------------------------------------------------ lifetime and the rest of SpotNav


async def test_unload_and_remove_release_only_this_entrys_share(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    charger = await setup_charger(hass)
    await go_auto(hass)
    entry = await add_price(hass)
    manager = domain_data(hass).price_refresh
    assert manager.owner_area("price:price_a") == SE4

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert manager.owner_area("price:price_a") is None
    # The charger's own subscriptions to the same area are untouched.
    assert manager.area_snapshot(SE4) is not None and manager.area_snapshot(SE4).subscriber_count >= 1
    assert charger_data(hass, charger.entry_id) is not None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert manager.owner_area("price:price_a") == SE4
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert manager.owner_area("price:price_a") is None
    assert hass.states.get("sensor.electricity_price_se4_price_now") is None


async def test_a_price_entry_is_never_taken_for_a_charger(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry = await add_price(hass)

    assert charger_data(hass, entry.entry_id) is None
    assert lookup_charger(hass, entry.entry_id) == (None, ERROR_UNKNOWN_CHARGER)
    assert dashboard_api.capture_chargers(hass) == ()


async def test_diagnostics_and_the_bundle_carry_the_price_entry(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    entry = await add_price(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    bundle = await async_build_debug_bundle(hass)

    assert diagnostics["entry_type"] == "price" and diagnostics["device"]["area_id"] == SE4
    assert diagnostics["device"]["intervals"] == 192 and diagnostics["device"]["subscribed"] is True
    assert diagnostics["device"]["fiscal"] == {"vat_percent": 25, "tax_minor_per_kwh": 36, "transfer_minor_per_kwh": 30}
    assert [item["entry_id"] for item in bundle["prices"]] == [entry.entry_id]
    assert [item["entry_id"] for item in bundle["chargers"]] == ["entry_a"]


@pytest.mark.parametrize(
    ("name", "day", "count"),
    [("day_SE4_2025-10-26_100.json", "2025-10-26", 100), ("day_SE4_2026-03-29_92.json", "2026-03-29", 92)],
)
async def test_price_now_on_a_clock_change_day(hass: HomeAssistant, freezer, offline_relay, name: str, day: str, count: int) -> None:
    from datetime import date

    from .relay import index_listing

    when = date.fromisoformat(day)
    offline_relay.serve(offline_relay.day_path(SE4, when), 200, fixture(name))
    offline_relay.serve("/v1/index.json", 200, index_listing(SE4, [day]))
    document = json.loads(fixture(name))
    # The day starts at local midnight (22:00 or 23:00 UTC the evening before); 01:30 UTC is in the hour that
    # happens twice in October and just after the hour that is skipped in March.
    start = dt_util.parse_datetime(document["start"]).astimezone(timezone.utc)
    now = datetime(when.year, when.month, when.day, 1, 30, tzinfo=timezone.utc)
    index = int((now - start).total_seconds() // 900)
    freezer.move_to(now)
    entry = await add_price(hass)
    device = price_data(hass, entry.entry_id).device

    assert len(device.points) == count
    state = hass.states.get(entity(hass, entry.entry_id, "price_now", "sensor"))
    rate = document["fx"]["SEK"]
    assert float(state.state) == round((document["prices"][index] * rate * 100 + 36 + 30) * 1.25 / 100, 4)
    # Six cheap hours are 24 quarters whatever the day's length.
    assert len(device.ranking(CHEAP).chosen) == 24

    boundary = now + timedelta(minutes=15)
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()
    state = hass.states.get(entity(hass, entry.entry_id, "price_now", "sensor"))
    assert float(state.state) == round((document["prices"][index + 1] * rate * 100 + 36 + 30) * 1.25 / 100, 4)
