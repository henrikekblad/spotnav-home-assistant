"""The area and fee entities of an electricity price device: real platforms, real services, real config entries.

They were a charger's until the price device took the area and fees over: one set per price device, writing the
entry's data in place (`pricing/price_source.py:async_write_price_entry`), and a charger's former set is removed
from the entity registry at its setup. Offline relay (SE4, NO1, DK1, DE-LU), today 2026-09-22.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.pricing.price_source import fees_from_data, price_write_revision
from custom_components.spotnav.runtime import domain_data, preview_for

from .relay import DE_LU, SE4, StubTransport
from .test_price_device import add_price, NOW
from .world import call, entity_id, go_auto, setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

NO1 = "NO1"
DK1 = "DK1"
OFF = {"enabled": False, "value": None}

FISCAL = (
    # component, policy select key, value number key
    ("vat", "fiscal_vat_policy", "vat_rate"),
    ("tax", "fiscal_tax_policy", "energy_tax"),
    ("transfer", "fiscal_transfer_policy", "transfer_fee"),
)

#: What the fixture catalogue publishes, per market, for the three components. A `None` is a market that publishes
#: no suggestion for that component, which is a state of its own.
SUGGESTIONS = {
    SE4: {"vat": 25.0, "tax": 36.0, "transfer": 30.0},
    NO1: {"vat": 25.0, "tax": 7.13, "transfer": 0.0},
    DK1: {"vat": 25.0, "tax": 0.0, "transfer": None},
    DE_LU: {"vat": None, "tax": None, "transfer": None},
}


def fee_of(hass: HomeAssistant, entry_id: str, component: str) -> Any:
    """One fee as the price entry stores it."""
    entry = hass.config_entries.async_get_entry(entry_id)
    return getattr(fees_from_data(entry.data["area_id"], entry.data), component)


async def add_off_price(hass: HomeAssistant) -> Any:
    """An electricity price for SE4 with every fee off."""
    return await add_price(hass, vat=OFF, tax=OFF, transfer=OFF)


# ------------------------------------------------------------------ where they live


async def test_the_area_and_fee_entities_are_the_price_devices_and_no_chargers(hass: HomeAssistant) -> None:
    charger = await setup_charger(hass)
    price = await add_price(hass)
    registry = er.async_get(hass)

    for platform, key in (
        ("select", "price_area"),
        *(("select", policy) for _, policy, _ in FISCAL),
        *(("number", value) for _, _, value in FISCAL),
    ):
        entity = registry.async_get(entity_id(hass, price.entry_id, key, platform))
        assert entity.config_entry_id == price.entry_id
        assert entity.entity_category.value == "config"
        assert registry.async_get_entity_id(platform, DOMAIN, f"{charger.entry_id}_{key}") is None
    # The hours stay.
    assert entity_id(hass, price.entry_id, "cheap_hours", "number")
    assert hass.states.get(entity_id(hass, price.entry_id, "price_area", "select")).entity_id == (
        "select.electricity_price_se4_price_area"
    )


async def test_a_chargers_former_area_and_fee_entities_are_removed_once_at_its_setup(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    former = {
        ("select", "price_area"),
        *(("select", policy) for _, policy, _ in FISCAL),
        *(("number", value) for _, _, value in FISCAL),
    }
    # What the release before created for this charger, left in the registry.
    for platform, key in former:
        registry.async_get_or_create(platform, DOMAIN, f"entry_a_{key}", suggested_object_id=f"garage_{key}")
    kept = registry.async_get_or_create("number", DOMAIN, "entry_a_charging_current")

    charger = await setup_charger(hass)

    for platform, key in former:
        assert registry.async_get_entity_id(platform, DOMAIN, f"entry_a_{key}") is None, key
    assert registry.async_get(kept.entity_id) is not None, "the charger's own entities stay"
    # A reload finds nothing left to remove and removes nothing else.
    before = {item.unique_id for item in er.async_entries_for_config_entry(registry, charger.entry_id)}
    assert await hass.config_entries.async_reload(charger.entry_id)
    await hass.async_block_till_done()
    assert {item.unique_id for item in er.async_entries_for_config_entry(registry, charger.entry_id)} == before


# ------------------------------------------------------------------ the area


async def test_area_options_come_only_from_the_catalogue(hass: HomeAssistant) -> None:
    price = await add_price(hass)
    state = hass.states.get(entity_id(hass, price.entry_id, "price_area", "select"))
    assert state.state == SE4
    assert [area for area in state.attributes["options"] if area] == ["DE-LU", "DK1", "NO1", "SE4"]
    assert state.attributes["area_names"][SE4]
    assert (state.attributes["currency"], state.attributes["minor_unit"]) == ("SEK", "öre")


async def test_a_configured_area_survives_an_offline_catalogue(hass: HomeAssistant, transport: StubTransport) -> None:
    """Losing the relay never loses the choice: it stays selected, stays an option, and the list says it is stale."""
    price = await add_price(hass)
    area = entity_id(hass, price.entry_id, "price_area", "select")
    await call(hass, "select", "select_option", {"entity_id": area, "option": DK1})

    transport.serve("/v1/areas.json", 500, "")
    repository = domain_data(hass).price_repository
    manager = domain_data(hass).price_refresh
    await repository.async_get_catalogue(refresh=True)
    assert repository.catalogue_snapshot().state == "stale"
    await manager.async_ensure_catalogue()
    await hass.async_block_till_done()

    state = hass.states.get(area)
    assert state.state == DK1 and DK1 in state.attributes["options"]
    assert state.attributes["catalogue_state"] == "stale"
    assert price.data["area_id"] == DK1
    # A market that is already configured stays selectable.
    await call(hass, "select", "select_option", {"entity_id": area, "option": DK1})


async def test_another_area_starts_from_its_own_suggestions_and_never_copies_a_figure(hass: HomeAssistant) -> None:
    """SE4's öre figures never become DE-LU's, the units follow the market, and the entry is renamed with it."""
    price = await add_price(hass)
    area = entity_id(hass, price.entry_id, "price_area", "select")
    tax = entity_id(hass, price.entry_id, "energy_tax", "number")
    policy = entity_id(hass, price.entry_id, "fiscal_tax_policy", "select")
    se_unit = hass.states.get(tax).attributes["unit_of_measurement"]
    assert se_unit == "öre/kWh"
    await call(hass, "number", "set_value", {"entity_id": tax, "value": 45.0})
    assert hass.states.get(policy).state == "manual"

    await call(hass, "select", "select_option", {"entity_id": area, "option": DE_LU})

    assert price.data["area_id"] == DE_LU and price.title == "Electricity price DE-LU"
    assert hass.states.get(tax).attributes["unit_of_measurement"] != se_unit
    assert hass.states.get(policy).state == "off", "DE-LU suggests nothing"
    assert hass.states.get(tax).state == "unknown", "and no copied figure"
    assert domain_data(hass).price_refresh.owner_area("price:price_a") == DE_LU, "the device moved to the area"

    await call(hass, "select", "select_option", {"entity_id": area, "option": SE4})
    assert hass.states.get(tax).attributes["unit_of_measurement"] == se_unit
    assert hass.states.get(policy).state == "suggested", "SE4's suggestion, as a new electricity price has"
    assert float(hass.states.get(tax).state) == SUGGESTIONS[SE4]["tax"]


async def test_an_unknown_area_is_refused(hass: HomeAssistant, transport: StubTransport) -> None:
    price = await add_price(hass)
    area = entity_id(hass, price.entry_id, "price_area", "select")
    transport.serve("/v1/areas.json", 500, "")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("select", "select_option", {"entity_id": area, "option": "XX9"}, blocking=True)
    assert price.data["area_id"] == SE4 and price_write_revision(price) == 0


@pytest.mark.usefixtures("no_bundled_catalogue")
async def test_the_market_list_reaches_the_select_once_it_can_be_read(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    transport.serve("/v1/areas.json", 500, "")  # nothing can be loaded at setup
    price = await add_price(hass)
    area = entity_id(hass, price.entry_id, "price_area", "select")
    assert hass.states.get(area).attributes["options"] == [SE4], "only the configured area"

    transport.serve_area(SE4)
    await domain_data(hass).price_refresh.async_ensure_catalogue()
    await hass.async_block_till_done()

    state = hass.states.get(area)
    assert state.attributes["options"] == ["DE-LU", "DK1", "NO1", "SE4"]
    assert state.attributes["catalogue_state"] == "ready"


# ------------------------------------------------------------------ the fees


@pytest.mark.parametrize(("component", "policy_key", "value_key"), FISCAL)
async def test_every_fee_round_trips_its_three_policies(
    hass: HomeAssistant, component: str, policy_key: str, value_key: str
) -> None:
    """Off, suggested and manual are three states, and each survives a round trip."""
    price = await add_off_price(hass)
    policy = entity_id(hass, price.entry_id, policy_key, "select")
    value = entity_id(hass, price.entry_id, value_key, "number")

    assert hass.states.get(policy).state == "off"
    assert hass.states.get(value).state == "unknown"

    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    assert (fee_of(hass, price.entry_id, component).enabled, fee_of(hass, price.entry_id, component).value) == (
        True,
        None,
    )
    shown = hass.states.get(value)
    assert float(shown.state) == SUGGESTIONS[SE4][component], "the suggestion, not unknown"
    assert shown.attributes["value_source"] == "suggested"
    assert shown.attributes["explicit_value"] is None
    assert shown.attributes["effective_value"] == SUGGESTIONS[SE4][component]
    assert shown.attributes["effective"] is True

    await call(hass, "number", "set_value", {"entity_id": value, "value": 7.5})
    assert fee_of(hass, price.entry_id, component).value == 7.5
    assert hass.states.get(policy).state == "manual"
    assert float(hass.states.get(value).state) == 7.5

    # Off again: the typed figure is kept for later, shown, and named as not charged.
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "off"})
    stored = fee_of(hass, price.entry_id, component)
    assert (stored.enabled, stored.value) == (False, 7.5)
    retained = hass.states.get(value)
    assert float(retained.state) == 7.5
    assert retained.attributes["value_source"] == "off" and retained.attributes["effective"] is False

    await call(hass, "select", "select_option", {"entity_id": policy, "option": "manual"})
    assert fee_of(hass, price.entry_id, component).value == 7.5
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    assert fee_of(hass, price.entry_id, component).value is None


async def test_manual_without_a_value_is_refused_and_a_zero_is_a_real_value(hass: HomeAssistant) -> None:
    price = await add_off_price(hass)
    policy = entity_id(hass, price.entry_id, "fiscal_vat_policy", "select")
    value = entity_id(hass, price.entry_id, "vat_rate", "number")

    with pytest.raises(ServiceValidationError) as refusal:
        await call(hass, "select", "select_option", {"entity_id": policy, "option": "manual"})
    assert refusal.value.translation_key == "invalid_fiscal"
    assert hass.states.get(policy).state == "off" and price_write_revision(price) == 0

    await call(hass, "number", "set_value", {"entity_id": value, "value": 0})
    stored = fee_of(hass, price.entry_id, "vat")
    assert (stored.enabled, stored.value) == (True, 0.0)
    assert hass.states.get(policy).state == "manual"


async def test_a_suggestion_the_area_lacks_and_a_published_zero(hass: HomeAssistant) -> None:
    price = await add_off_price(hass)
    area = entity_id(hass, price.entry_id, "price_area", "select")
    policy = entity_id(hass, price.entry_id, "fiscal_transfer_policy", "select")
    transfer = entity_id(hass, price.entry_id, "transfer_fee", "number")

    # DK1 publishes no grid fee: suggested, and nothing to show or charge.
    await call(hass, "select", "select_option", {"entity_id": area, "option": DK1})
    assert hass.states.get(policy).state == "off", "a new area's fee without a suggestion starts off"
    await call(hass, "select", "select_option", {"entity_id": policy, "option": "suggested"})
    unresolved = hass.states.get(transfer)
    assert unresolved.attributes["value_source"] == "suggestion_unavailable"
    assert unresolved.state == "unknown" and unresolved.attributes["effective"] is False

    # NO1 publishes a literal zero: shown, not blanked.
    await call(hass, "select", "select_option", {"entity_id": area, "option": NO1})
    shown = hass.states.get(transfer)
    assert hass.states.get(policy).state == "suggested"
    assert float(shown.state) == 0.0 and shown.attributes["suggested_value"] == 0.0


async def test_reading_a_fee_writes_nothing_and_each_write_is_counted_once(hass: HomeAssistant) -> None:
    price = await add_off_price(hass)
    await call(
        hass,
        "select",
        "select_option",
        {"entity_id": entity_id(hass, price.entry_id, "fiscal_vat_policy", "select"), "option": "suggested"},
    )
    assert price_write_revision(price) == 1
    for _ in range(2):
        for key, platform in (
            ("vat_rate", "number"),
            ("energy_tax", "number"),
            ("transfer_fee", "number"),
            ("fiscal_vat_policy", "select"),
            ("price_area", "select"),
        ):
            assert hass.states.get(entity_id(hass, price.entry_id, key, platform)) is not None
        await hass.async_block_till_done()
    assert price_write_revision(price) == 1
    assert fee_of(hass, price.entry_id, "vat").value is None


async def test_a_fee_entity_changes_the_price_now_in_place_and_every_charger_replans(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")
    await go_auto(hass, "entry_a")
    await go_auto(hass, "entry_b")
    price = await add_price(hass)
    now = entity_id(hass, price.entry_id, "price_now", "sensor")
    before_price = float(hass.states.get(now).state)
    before = {charger: preview_for(hass, charger).snapshot() for charger in ("entry_a", "entry_b")}
    states: list[str] = []

    def record(event: Any) -> None:
        if event.data["entity_id"] == now:
            states.append(event.data["new_state"].state)

    hass.bus.async_listen("state_changed", record)
    transfer = entity_id(hass, price.entry_id, "transfer_fee", "number")

    await call(hass, "number", "set_value", {"entity_id": transfer, "value": 80.0})

    assert float(hass.states.get(now).state) > before_price
    assert "unavailable" not in states, "never unavailable on the way"
    assert price.state.value == "loaded"
    for charger in ("entry_a", "entry_b"):
        after = preview_for(hass, charger).snapshot()
        assert after.price_revision != before[charger].price_revision
        assert after.proposal.estimated_cost > before[charger].proposal.estimated_cost


async def test_the_included_parts_are_unavailable(hass: HomeAssistant, offline_relay) -> None:
    from homeassistant.const import STATE_UNAVAILABLE

    from .test_relay_v2 import serve_v2

    serve_v2(offline_relay)
    price = await add_price(hass, area="GB-C")
    for platform, key in (
        *(("select", policy) for _, policy, _ in FISCAL),
        *(("number", value) for _, _, value in FISCAL),
    ):
        assert hass.states.get(entity_id(hass, price.entry_id, key, platform)).state == STATE_UNAVAILABLE
    area = entity_id(hass, price.entry_id, "price_area", "select")
    await call(hass, "select", "select_option", {"entity_id": area, "option": SE4})
    assert hass.states.get(entity_id(hass, price.entry_id, "fiscal_vat_policy", "select")).state != STATE_UNAVAILABLE
