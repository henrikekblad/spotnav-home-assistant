"""The grid tariff on the electricity price entry: how it is stored, read, resolved for a plan and kept by writes
that do not know it.

Real config entries on the offline relay (SE4) at a frozen clock; the pure parts read the bundled v2 catalogue.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.planning.auto_controller import fiscal_choice_for
from custom_components.spotnav.planning.auto_settings import (
    AreaAutoSettings,
    AutoSettings,
    AutoSettingsError,
    FiscalOverride,
    GridTariff,
)
from custom_components.spotnav.planning.planner import effective_minor_per_kwh, HighLoadWindow
from custom_components.spotnav.pricing.price_source import (
    async_write_price_entry,
    fees_from_data,
    price_source_for,
    price_write_revision,
    source_revision,
)
from custom_components.spotnav.pricing.relay_contract import parse_catalogue
from custom_components.spotnav.runtime import preview_for

from .relay import SE4
from .test_price_device import add_price, NOW
from .world import call, entity_id, go_auto, setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

OFF = {"enabled": False, "value": None}
SUGGESTED = {"enabled": True, "value": None}
#: The owner's tariff: 16 öre ex VAT fixed plus 5 % of spot.
OWNER_PERCENT = {"enabled": True, "value": 5.0}
WINDOW = {"weekdays_only": True, "from_hour": 7, "to_hour": 19, "winter_only": True}

CATALOGUE = parse_catalogue(
    json.loads((Path(__file__).parent / "fixtures" / "relay_v2" / "areas-v2.json").read_text("utf-8")), version=2
)


def _se4(**tariff: Any) -> AreaAutoSettings:
    return AreaAutoSettings(
        area_id=SE4,
        vat=FiscalOverride(enabled=True),
        tax=FiscalOverride(enabled=True),
        transfer=FiscalOverride(enabled=True, value=16.0),
        grid_tariff=None if not tariff else GridTariff(**tariff),
    )


# ------------------------------------------------------------------ stored and read


def test_an_entry_without_the_keys_reads_exactly_as_before() -> None:
    data = {"area_id": SE4, "vat": SUGGESTED, "tax": SUGGESTED, "transfer": OFF}
    row = fees_from_data(SE4, data)
    assert row.grid_tariff is None
    assert row == AreaAutoSettings(area_id=SE4, vat=FiscalOverride(True), tax=FiscalOverride(True))
    # The digest is the one it always was: the digest of the three fees alone.
    assert source_revision(row) == source_revision(AreaAutoSettings(area_id=SE4, vat=FiscalOverride(True), tax=FiscalOverride(True)))
    # All parts stored off read as none at all, with the same digest.
    off = fees_from_data(SE4, {**data, "transfer_percent": OFF, "transfer_high": OFF, "high_load_window": None})
    assert off.grid_tariff is None and source_revision(off) == source_revision(row)


def test_a_stored_tariff_is_read_and_moves_the_revision() -> None:
    data = {
        "area_id": SE4, "vat": SUGGESTED, "tax": SUGGESTED, "transfer": OFF,
        "transfer_percent": OWNER_PERCENT, "transfer_high": OFF, "high_load_window": WINDOW,
    }
    row = fees_from_data(SE4, data)
    assert row.grid_tariff == GridTariff(
        transfer_percent=FiscalOverride(True, 5.0), high_load_window=HighLoadWindow(True, 7, 19, True)
    )
    assert source_revision(row) != source_revision(row.fees_only())
    # The settings record's row is untouched: no new key in `as_dict`.
    assert set(row.as_dict()) == {"area_id", "vat", "tax", "transfer"}


def test_an_unreadable_stored_part_is_off_never_a_guess() -> None:
    data = {
        "area_id": SE4, "transfer_percent": {"enabled": "yes", "value": 5}, "transfer_high": {"enabled": True},
        "high_load_window": {"weekdays_only": True, "from_hour": 19, "to_hour": 7, "winter_only": True},
    }
    assert fees_from_data(SE4, data).grid_tariff is None


@pytest.mark.parametrize(
    "tariff",
    [
        GridTariff(transfer_percent=FiscalOverride(True, None)),
        GridTariff(transfer_high=FiscalOverride(True, 50.0)),
        GridTariff(transfer_high=FiscalOverride(True, None), high_load_window=HighLoadWindow()),
        GridTariff(transfer_percent=FiscalOverride(True, -1.0)),
        GridTariff(high_load_window=HighLoadWindow(from_hour=19, to_hour=19)),
    ],
)
def test_a_tariff_that_cannot_be_priced_is_refused(tariff: GridTariff) -> None:
    with pytest.raises(AutoSettingsError) as refused:
        tariff.validated()
    assert refused.value.code == "invalid_fiscal"


# ------------------------------------------------------------------ resolved for a plan


def test_the_owners_tariff_resolves_and_prices_both_signs() -> None:
    se4 = CATALOGUE.area(SE4)
    fiscal = fiscal_choice_for(
        AutoSettings(area_id=SE4, overrides=(_se4(transfer_percent=FiscalOverride(True, 5.0)),)), se4
    )
    assert fiscal is not None
    assert (fiscal.transfer_percent_enabled, fiscal.transfer_percent, fiscal.high_load_window) == (True, 5.0, None)
    # Tax 36 öre (the area's suggestion), the fixed fee 16 öre ex VAT, 5 % of spot, VAT 25 %.
    for spot_kr in (0.80, -0.10):
        assert effective_minor_per_kwh(spot_kr, fiscal) == pytest.approx((1.05 * spot_kr * 100 + 36 + 16) * 1.25)


def test_the_window_takes_the_areas_zone_and_only_while_the_high_fee_is_on() -> None:
    se4 = CATALOGUE.area(SE4)
    window = HighLoadWindow(True, 7, 19, True)
    on = fiscal_choice_for(
        AutoSettings(
            area_id=SE4,
            overrides=(_se4(transfer_high=FiscalOverride(True, 60.0), high_load_window=window),),
        ),
        se4,
    )
    assert on is not None and on.high_load_window == replace(window, tz="Europe/Stockholm")
    assert on.transfer_high_minor_per_kwh == 60.0
    kept = fiscal_choice_for(
        AutoSettings(area_id=SE4, overrides=(_se4(transfer_high=FiscalOverride(False, 60.0), high_load_window=window),)),
        se4,
    )
    assert kept is not None and kept.high_load_window is None and not kept.needs_start


def test_an_area_whose_price_includes_the_grid_fee_locks_both_parts() -> None:
    london = CATALOGUE.area("GB-C")
    row = AreaAutoSettings(
        area_id="GB-C",
        grid_tariff=GridTariff(
            transfer_percent=FiscalOverride(True, 5.0),
            transfer_high=FiscalOverride(True, 9.0),
            high_load_window=HighLoadWindow(False, 16, 19, False),
        ),
    )
    fiscal = fiscal_choice_for(AutoSettings(area_id="GB-C", overrides=(row,)), london)
    assert fiscal is not None
    assert not fiscal.transfer_percent_enabled and not fiscal.transfer_high_enabled
    assert fiscal.high_load_window is None


def test_a_fee_on_without_a_figure_leaves_the_fees_unresolved() -> None:
    se4 = CATALOGUE.area(SE4)
    row = _se4(transfer_percent=FiscalOverride(True, None))
    assert fiscal_choice_for(AutoSettings(area_id=SE4, overrides=(row,)), se4) is None


# ------------------------------------------------------------------ writes


async def test_a_fee_write_that_knows_only_the_three_fees_keeps_the_grid_tariff(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    price = await add_price(hass, transfer_percent=OWNER_PERCENT, transfer_high=OFF, high_load_window=WINDOW)
    # What an older card, app or the options flow hands over: a row without any grid tariff.
    async_write_price_entry(
        hass, price, AreaAutoSettings(area_id=SE4, vat=FiscalOverride(True, 12.0)), expected_write_revision=0
    )
    await hass.async_block_till_done()
    assert price.data["vat"] == {"enabled": True, "value": 12.0}
    assert price.data["transfer_percent"] == OWNER_PERCENT
    assert price.data["high_load_window"] == WINDOW
    assert price_write_revision(price) == 1


async def test_an_area_change_switches_the_high_fee_off_and_keeps_the_rest(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    price = await add_price(
        hass, transfer_percent=OWNER_PERCENT, transfer_high={"enabled": True, "value": 60.0}, high_load_window=WINDOW
    )
    async_write_price_entry(hass, price, AreaAutoSettings(area_id="DK1"))
    await hass.async_block_till_done()
    assert price.data["transfer_high"] == OFF
    assert price.data["transfer_percent"] == OWNER_PERCENT
    assert price.data["high_load_window"] == WINDOW


async def test_a_tariff_change_replans_with_the_new_price(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    await go_auto(hass)
    price = await add_price(hass)
    preview = preview_for(hass, "entry_a")
    before = preview.snapshot()
    assert before.state == "proposal_ready"
    revision = price_source_for(hass, "entry_a").revision

    source = price_source_for(hass, "entry_a")
    tariff = GridTariff(
        transfer_percent=FiscalOverride(True, 5.0),
        transfer_high=FiscalOverride(True, 200.0),
        high_load_window=HighLoadWindow(False, 0, 24, False),
    )
    async_write_price_entry(hass, price, replace(source.overrides, grid_tariff=tariff), expected_write_revision=0)
    await hass.async_block_till_done()

    after = preview.snapshot()
    assert price_source_for(hass, "entry_a").revision != revision
    assert after.price_revision != before.price_revision
    assert after.proposal_identity != before.proposal_identity
    # Every hour is a high-load hour at 2 kr more, plus 5 % of spot: the plan costs more.
    assert after.proposal.estimated_cost > before.proposal.estimated_cost


# ------------------------------------------------------------------ the price device's entities


#: The hidden grid tariff entities, by platform and key.
HIDDEN = (
    ("select", "grid_fee_percent_policy"),
    ("select", "grid_fee_high_policy"),
    ("number", "high_load_from_hour"),
    ("number", "high_load_to_hour"),
    ("switch", "high_load_weekdays_only"),
    ("switch", "high_load_winter_only"),
)


async def _enable_hidden(hass: HomeAssistant, price: Any) -> None:
    registry = er.async_get(hass)
    for platform, key in HIDDEN:
        registry.async_update_entity(entity_id(hass, price.entry_id, key, platform), disabled_by=None)
    assert await hass.config_entries.async_reload(price.entry_id)
    await hass.async_block_till_done()


async def test_the_two_fee_numbers_are_shown_and_the_rest_hidden(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    price = await add_price(hass)
    registry = er.async_get(hass)
    for key in ("grid_fee_percent", "grid_fee_high"):
        entity = registry.async_get(entity_id(hass, price.entry_id, key, "number"))
        assert entity.disabled_by is None and entity.entity_category.value == "config"
        assert hass.states.get(entity.entity_id).state == "unknown"
    for platform, key in HIDDEN:
        entity = registry.async_get(entity_id(hass, price.entry_id, key, platform))
        assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
        assert entity.entity_category.value == "config"


async def test_the_percentage_number_prices_the_owners_tariff_now(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    price = await add_price(hass, transfer={"enabled": True, "value": 16.0})
    now = entity_id(hass, price.entry_id, "price_now", "sensor")
    spot = hass.states.get(now).attributes["spot_price"]

    await call(hass, "number", "set_value", {"entity_id": entity_id(hass, price.entry_id, "grid_fee_percent", "number"), "value": 5})

    assert price.data["transfer_percent"] == {"enabled": True, "value": 5.0}
    assert "transfer_high" in price.data and price.data["high_load_window"] is None
    # (1.05 x spot + tax 36 + 16) x 1.25, in kr/kWh.
    expected = (1.05 * spot * 100 + 36 + 16) * 1.25 / 100
    assert float(hass.states.get(now).state) == pytest.approx(expected, abs=1e-3)


async def test_the_high_fee_number_starts_the_usual_window_and_the_window_entities_change_it(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    price = await add_price(hass)
    await _enable_hidden(hass, price)
    weekdays = entity_id(hass, price.entry_id, "high_load_weekdays_only", "switch")
    assert hass.states.get(weekdays).state == "unknown"

    await call(hass, "number", "set_value", {"entity_id": entity_id(hass, price.entry_id, "grid_fee_high", "number"), "value": 60})
    assert price.data["transfer_high"] == {"enabled": True, "value": 60.0}
    assert price.data["high_load_window"] == WINDOW
    assert hass.states.get(weekdays).state == "on"
    assert hass.states.get(entity_id(hass, price.entry_id, "grid_fee_high_policy", "select")).state == "manual"

    await call(hass, "switch", "turn_off", {"entity_id": weekdays})
    await call(hass, "switch", "turn_off", {"entity_id": entity_id(hass, price.entry_id, "high_load_winter_only", "switch")})
    await call(hass, "number", "set_value", {"entity_id": entity_id(hass, price.entry_id, "high_load_from_hour", "number"), "value": 6})
    await call(hass, "number", "set_value", {"entity_id": entity_id(hass, price.entry_id, "high_load_to_hour", "number"), "value": 22})
    assert price.data["high_load_window"] == {"weekdays_only": False, "from_hour": 6, "to_hour": 22, "winter_only": False}

    # A window that ends before it starts is refused, and nothing moves.
    revision = price_write_revision(price)
    with pytest.raises(ServiceValidationError):
        await call(hass, "number", "set_value", {"entity_id": entity_id(hass, price.entry_id, "high_load_to_hour", "number"), "value": 5})
    assert price_write_revision(price) == revision

    # Off keeps the figure and the window, so on again needs nothing else.
    policy = entity_id(hass, price.entry_id, "grid_fee_high_policy", "select")
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "off"})
    assert price.data["transfer_high"] == {"enabled": False, "value": 60.0}
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "manual"})
    assert price.data["transfer_high"] == {"enabled": True, "value": 60.0}

    # "manual" with no figure kept is refused: no area suggests one.
    percent = entity_id(hass, price.entry_id, "grid_fee_percent_policy", "select")
    with pytest.raises(ServiceValidationError):
        await call(hass, "select", "select_option", {"entity_id": percent, "option": "manual"})


async def test_the_grid_tariff_entities_are_unavailable_where_the_price_includes_the_grid_fee(
    hass: HomeAssistant, offline_relay
) -> None:
    from .test_relay_v2 import serve_v2

    serve_v2(offline_relay)
    price = await add_price(hass, area="GB-C")
    for key in ("grid_fee_percent", "grid_fee_high"):
        assert hass.states.get(entity_id(hass, price.entry_id, key, "number")).state == STATE_UNAVAILABLE
