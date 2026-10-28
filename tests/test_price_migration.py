"""Electricity price entries SpotNav makes itself (`pricing/price_setup.py`): the price flow's import step, the
first install, a card write with no price entry yet, the one-time migration of the chargers' own area and fees,
the wire revision across a change of source (`pricing/price_store.py`), and the pointers a removed entry leaves.

Real config entries on the offline relay (SE4, NO1, DK1), at a frozen clock.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.spotnav.api.settings import async_update_settings, settings_answer, wire_settings
from custom_components.spotnav.const import CONF_PRICE_ENTRY_ID, DOMAIN, ENTRY_TYPE_PRICE
from custom_components.spotnav.planning.auto_settings import AreaAutoSettings, AutoSettingsError, FiscalOverride
from custom_components.spotnav.pricing import price_setup
from custom_components.spotnav.pricing.price_setup import async_migrate_price_sources
from custom_components.spotnav.pricing.price_source import (
    async_write_price_entry,
    fees_from_data,
    price_source_for,
    VIA_CHARGER,
    VIA_SITE,
)
from custom_components.spotnav.pricing.price_store import price_duplicates
from custom_components.spotnav.runtime import domain_data

from .helpers import make_site_entry
from .relay import SE4
from .test_price_device import add_price, NOW
from .test_strategy_schema import body_of
from .world import go_auto, settings_of, setup_charger, setup_site

pytestmark = pytest.mark.usefixtures("offline_relay")

OFF = {"enabled": False, "value": None}
SUGGESTED = {"enabled": True, "value": None}
#: SE4 with the area's suggestions: what the price flow preselects.
SE4_SUGGESTED = AreaAutoSettings(
    area_id=SE4, vat=FiscalOverride(enabled=True), tax=FiscalOverride(enabled=True), transfer=FiscalOverride(enabled=True)
)
#: SE4 with a grid fee of the owner's own.
SE4_OWN_FEE = AreaAutoSettings(
    area_id=SE4,
    vat=FiscalOverride(enabled=True),
    tax=FiscalOverride(enabled=True),
    transfer=FiscalOverride(enabled=True, value=24.5),
)


def prices(hass: HomeAssistant) -> list[Any]:
    return [entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.data.get("entry_type") == ENTRY_TYPE_PRICE]


def fees_of(entry: Any) -> AreaAutoSettings:
    return fees_from_data(entry.data["area_id"], entry.data)


def pointer(hass: HomeAssistant, entry_id: str) -> str | None:
    return hass.config_entries.async_get_entry(entry_id).data.get(CONF_PRICE_ENTRY_ID)


def wire(hass: HomeAssistant, charger_id: str = "entry_a") -> int:
    return settings_answer(hass, charger_id, settings_of(hass, charger_id))["settings"]["revision"]


async def import_price(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """The import step as SpotNav starts it: with the integration set up."""
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data=data
    )
    await hass.async_block_till_done()
    return result


async def migrate_again(hass: HomeAssistant) -> bool:
    """The migration as an upgraded installation runs it: the marker cleared, the chargers' settings as they are."""
    domain_data(hass).price_store.migrated = False
    done = await async_migrate_price_sources(hass)
    await hass.async_block_till_done()
    return done


async def two_chargers(hass: HomeAssistant) -> None:
    await setup_charger(hass)
    await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")


# ------------------------------------------------------------------ the import step


async def test_the_import_step_adds_a_price_judged_and_named_as_the_form_would(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    result = await import_price(
        hass,
        {"area_id": SE4, "vat": SUGGESTED, "tax": {"enabled": False, "value": 12.0}, "transfer": {"enabled": True, "value": 24.5}},
    )

    assert result["type"] == "create_entry", result
    entry = result["result"]
    assert entry.title == "Electricity price SE4" and entry.state.value == "loaded"
    # An off fee keeps no figure, as the form stores it.
    assert dict(entry.data) == {
        "entry_type": ENTRY_TYPE_PRICE,
        "area_id": SE4,
        "vat": SUGGESTED,
        "tax": OFF,
        "transfer": {"enabled": True, "value": 24.5},
    }


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"area_id": "XX9", "vat": OFF, "tax": OFF, "transfer": OFF}, "unknown_price_area"),
        ({"area_id": SE4, "vat": {"enabled": "yes", "value": None}, "tax": OFF, "transfer": OFF}, "invalid_price_fees"),
        ({"area_id": SE4, "vat": {"enabled": True, "value": 150.0}, "tax": OFF, "transfer": OFF}, "invalid_price_fees"),
        ({"area_id": SE4, "vat": OFF, "tax": {"enabled": True, "value": -1.0}, "transfer": OFF}, "invalid_price_fees"),
    ],
)
async def test_the_import_step_refuses_what_the_form_refuses(
    hass: HomeAssistant, freezer, data: dict[str, Any], reason: str
) -> None:
    freezer.move_to(NOW)
    result = await import_price(hass, data)

    assert (result["type"], result["reason"]) == ("abort", reason)
    assert prices(hass) == []


# ------------------------------------------------------------------ the first install


async def test_the_first_charger_gets_a_price_from_the_location(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    hass.config.country = "SE"
    hass.config.latitude, hass.config.longitude = 55.6, 13.0

    await setup_charger(hass)
    await hass.async_block_till_done()

    [price] = prices(hass)
    assert price.title == "Electricity price SE4"
    assert fees_of(price) == SE4_SUGGESTED, "the fees the price flow preselects"
    assert pointer(hass, "entry_a") == price.entry_id
    assert price_source_for(hass, "entry_a").via == VIA_CHARGER

    # A second charger joins it without anything new.
    await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")
    await hass.async_block_till_done()
    assert prices(hass) == [price]
    assert price_source_for(hass, "entry_b").entry_id == price.entry_id


async def test_a_first_site_points_at_the_price_it_brings(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    hass.config.country = "SE"
    hass.config.latitude, hass.config.longitude = 55.6, 13.0

    site = await setup_site(hass)
    await hass.async_block_till_done()

    [price] = prices(hass)
    assert fees_of(price) == SE4_SUGGESTED
    assert pointer(hass, site.entry_id) == price.entry_id


async def test_without_a_suggestion_the_first_install_makes_nothing(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    hass.config.country = None

    await setup_charger(hass)
    await hass.async_block_till_done()

    assert prices(hass) == []
    assert pointer(hass, "entry_a") is None
    assert domain_data(hass).price_store.migrated, "the migration ran once Home Assistant had started"


async def test_the_area_set_through_the_card_makes_the_price_and_points_at_it(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    hass.config.country = None
    await setup_charger(hass)
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=["entry_a"])
    before = settings_of(hass, "entry_a")
    revision = wire(hass)
    row = {"area_id": SE4, "vat": SUGGESTED, "tax": SUGGESTED, "transfer": {"enabled": True, "value": 24.5}}

    await async_update_settings(
        hass, "entry_a", expected_revision=revision, replacement=body_of(before, area_id=SE4, overrides=[row])
    )
    await hass.async_block_till_done()

    [price] = prices(hass)
    assert fees_of(price) == SE4_OWN_FEE
    assert pointer(hass, site.entry_id) == price.entry_id, "the charger's site points at it"
    assert pointer(hass, "entry_a") is None
    source = price_source_for(hass, "entry_a")
    assert (source.via, source.entry_id) == (VIA_SITE, price.entry_id)
    assert wire(hass) > revision

    # A later write of the area goes to that entry; no second one is made.
    current = settings_of(hass, "entry_a")
    await async_update_settings(
        hass, "entry_a", expected_revision=wire(hass), replacement=body_of(current, area_id="NO1", overrides=[])
    )
    await hass.async_block_till_done()
    assert prices(hass) == [price] and price.data["area_id"] == "NO1"


# ------------------------------------------------------------------ the migration


async def test_one_charger_reuses_an_identical_price(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass)
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(SE4_OWN_FEE,))
    existing = await add_price(hass, transfer={"enabled": True, "value": 24.5})
    own_before = settings_of(hass, "entry_a")

    assert await migrate_again(hass)

    assert prices(hass) == [existing]
    assert pointer(hass, "entry_a") == existing.entry_id
    assert price_duplicates(hass) == []
    own = settings_of(hass, "entry_a")
    assert (own.area_id, own.overrides) == (own_before.area_id, own_before.overrides), "a rollback reads them"


async def test_one_charger_without_a_price_gets_one(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass)
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(SE4_OWN_FEE,))

    assert await migrate_again(hass)

    [price] = prices(hass)
    assert (price.title, fees_of(price)) == ("Electricity price SE4", SE4_OWN_FEE)
    assert pointer(hass, "entry_a") == price.entry_id
    assert price_source_for(hass, "entry_a").overrides == SE4_OWN_FEE


async def test_two_chargers_with_the_same_fees_share_one_price_through_their_site(hass: HomeAssistant, freezer) -> None:
    await two_chargers(hass)
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=["entry_a", "entry_b"])
    freezer.move_to(NOW)
    # The same fees, stated differently: an off fee with a figure kept prices as off.
    no_tax = AreaAutoSettings(area_id=SE4, vat=FiscalOverride(enabled=True), transfer=FiscalOverride(enabled=True, value=24.5))
    await go_auto(hass, "entry_a", overrides=(no_tax,))
    await go_auto(hass, "entry_b", overrides=(replace(no_tax, tax=FiscalOverride(enabled=False, value=12.0)),))

    assert await migrate_again(hass)

    [price] = prices(hass)
    assert fees_of(price) == no_tax
    assert pointer(hass, site.entry_id) == price.entry_id
    assert pointer(hass, "entry_a") is None and pointer(hass, "entry_b") is None
    for charger in ("entry_a", "entry_b"):
        assert price_source_for(hass, charger).via == VIA_SITE


async def test_site_members_with_different_fees_point_at_their_own(hass: HomeAssistant, freezer) -> None:
    await two_chargers(hass)
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=["entry_a", "entry_b"])
    freezer.move_to(NOW)
    await go_auto(hass, "entry_a", overrides=(SE4_OWN_FEE,))
    await go_auto(hass, "entry_b", overrides=(SE4_SUGGESTED,))

    assert await migrate_again(hass)

    assert len(prices(hass)) == 2
    by_fees = {fees_of(entry): entry.entry_id for entry in prices(hass)}
    assert pointer(hass, site.entry_id) is None
    assert pointer(hass, "entry_a") == by_fees[SE4_OWN_FEE]
    assert pointer(hass, "entry_b") == by_fees[SE4_SUGGESTED]
    [duplicate] = price_duplicates(hass)
    assert duplicate == {"area_id": SE4, "entry_ids": sorted(by_fees.values()), "answered": False}


async def test_an_existing_price_with_other_fees_is_left_alone_and_recorded(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass)
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(SE4_OWN_FEE,))
    existing = await add_price(hass)
    existing_data = dict(existing.data)

    assert await migrate_again(hass)

    [made] = [entry for entry in prices(hass) if entry.entry_id != existing.entry_id]
    assert fees_of(made) == SE4_OWN_FEE
    assert dict(existing.data) == existing_data, "never edited"
    assert pointer(hass, "entry_a") == made.entry_id
    assert price_duplicates(hass) == [
        {"area_id": SE4, "entry_ids": sorted([existing.entry_id, made.entry_id]), "answered": False}
    ]
    # Answered once, not asked again.
    assert await domain_data(hass).price_store.async_answer_duplicates(SE4)
    assert price_duplicates(hass) == []


async def test_a_second_run_changes_nothing(hass: HomeAssistant, freezer) -> None:
    await two_chargers(hass)
    freezer.move_to(NOW)
    await go_auto(hass, "entry_a", overrides=(SE4_OWN_FEE,))
    await go_auto(hass, "entry_b", overrides=(SE4_SUGGESTED,))
    assert await migrate_again(hass)
    made = sorted(entry.entry_id for entry in prices(hass))
    pointers = (pointer(hass, "entry_a"), pointer(hass, "entry_b"))

    # The marker holds: the run returns at once.
    assert await async_migrate_price_sources(hass)
    # Even run again from the start, it reuses what it made.
    assert await migrate_again(hass)

    assert sorted(entry.entry_id for entry in prices(hass)) == made
    assert (pointer(hass, "entry_a"), pointer(hass, "entry_b")) == pointers
    assert len(price_duplicates(hass)) == 1


async def test_an_interrupted_run_resumes_without_duplicates(hass: HomeAssistant, freezer, monkeypatch) -> None:
    await two_chargers(hass)
    freezer.move_to(NOW)
    await go_auto(hass, "entry_a", overrides=(SE4_OWN_FEE,))
    await go_auto(hass, "entry_b", overrides=(SE4_SUGGESTED,))
    real = price_setup._write_pointers

    def _interrupted(*_args: Any) -> None:
        raise RuntimeError("stopped")

    monkeypatch.setattr(price_setup, "_write_pointers", _interrupted)
    domain_data(hass).price_store.migrated = False
    with pytest.raises(RuntimeError):
        await async_migrate_price_sources(hass)
    assert len(prices(hass)) == 2 and pointer(hass, "entry_a") is None
    assert domain_data(hass).price_store.migrated is False

    monkeypatch.setattr(price_setup, "_write_pointers", real)
    assert await async_migrate_price_sources(hass)

    assert len(prices(hass)) == 2, "the entries the first run made are reused"
    assert {pointer(hass, "entry_a"), pointer(hass, "entry_b")} == {entry.entry_id for entry in prices(hass)}
    assert domain_data(hass).price_store.migrated is True


@pytest.mark.parametrize("same_fees", [True, False])
async def test_the_owners_installation(hass: HomeAssistant, freezer, same_fees: bool) -> None:
    """One charger on a site and one electricity price for SE4, with the same fees or other ones."""
    await setup_charger(hass)
    site = await setup_site(hass, charger_entry_ids=["entry_a"])
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(SE4_SUGGESTED if same_fees else SE4_OWN_FEE,))
    existing = await add_price(hass)
    existing_data = dict(existing.data)

    assert await migrate_again(hass)

    assert dict(existing.data) == existing_data
    assert pointer(hass, "entry_a") is None
    if same_fees:
        assert prices(hass) == [existing]
        assert pointer(hass, site.entry_id) == existing.entry_id
        assert price_duplicates(hass) == []
    else:
        [made] = [entry for entry in prices(hass) if entry.entry_id != existing.entry_id]
        assert fees_of(made) == SE4_OWN_FEE
        assert pointer(hass, site.entry_id) == made.entry_id
        assert price_duplicates(hass) == [
            {"area_id": SE4, "entry_ids": sorted([existing.entry_id, made.entry_id]), "answered": False}
        ]
    assert price_source_for(hass, "entry_a").overrides == (SE4_SUGGESTED if same_fees else SE4_OWN_FEE)


async def test_a_charger_without_an_area_is_left_to_the_first_install(hass: HomeAssistant, freezer) -> None:
    hass.config.country = None
    await setup_charger(hass)
    freezer.move_to(NOW)

    assert await migrate_again(hass)

    assert prices(hass) == [] and pointer(hass, "entry_a") is None


# ------------------------------------------------------------------ the wire revision


async def test_the_wire_revision_only_grows_across_a_change_of_source(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass)
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=["entry_a"])
    freezer.move_to(NOW)
    await go_auto(hass)
    # A price entry written many times, then a new one with its counter at zero.
    busy = await add_price(hass, write_revision=40)
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_PRICE_ENTRY_ID: busy.entry_id})
    on_busy = wire(hass)
    assert on_busy == settings_of(hass, "entry_a").revision + 40
    fresh = await add_price(hass, entry_id="price_b", tax={"enabled": False, "value": None})

    hass.config_entries.async_update_entry(site, data={**site.data, CONF_PRICE_ENTRY_ID: fresh.entry_id})
    on_fresh = wire(hass)

    assert on_fresh > on_busy
    assert wire(hass) == on_fresh, "steady while the source stays"
    # A write against that revision goes through, and moves it on by one.
    current = wire_settings(hass, "entry_a", settings_of(hass, "entry_a"))
    await async_update_settings(hass, "entry_a", expected_revision=on_fresh, replacement=body_of(current, amps=12))
    assert wire(hass) == on_fresh + 1
    # A body read on the old source is stale.
    with pytest.raises(AutoSettingsError) as refused:
        await async_update_settings(hass, "entry_a", expected_revision=on_busy, replacement=body_of(current, amps=11))
    assert refused.value.code == "revision_conflict"

    # The entry removed: the charger's own settings answer, still above.
    before_removal = wire(hass)
    await hass.config_entries.async_remove(fresh.entry_id)
    await hass.config_entries.async_remove(busy.entry_id)
    await hass.async_block_till_done()
    assert price_source_for(hass, "entry_a").entry_id is None
    assert wire(hass) > before_removal


# ------------------------------------------------------------------ removal


async def test_a_removed_price_takes_its_pointers_along(hass: HomeAssistant, freezer) -> None:
    await two_chargers(hass)
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=["entry_a"])
    freezer.move_to(NOW)
    gone = await add_price(hass)
    kept = await add_price(hass, entry_id="price_b")
    for entry in (site, hass.config_entries.async_get_entry("entry_b")):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PRICE_ENTRY_ID: gone.entry_id})
    charger_a = hass.config_entries.async_get_entry("entry_a")
    hass.config_entries.async_update_entry(charger_a, data={**charger_a.data, CONF_PRICE_ENTRY_ID: kept.entry_id})

    await hass.config_entries.async_remove(gone.entry_id)
    await hass.async_block_till_done()

    assert pointer(hass, site.entry_id) is None and pointer(hass, "entry_b") is None
    assert pointer(hass, "entry_a") == kept.entry_id, "a pointer to another entry stays"


# ------------------------------------------------------------------ names beside a same-area price


async def test_a_second_price_for_an_area_is_named_after_its_site(hass: HomeAssistant, freezer) -> None:
    """The owner's installation: an existing SE4 price, and a site whose charger had other fees."""
    await setup_charger(hass)
    await setup_site(hass, charger_entry_ids=["entry_a"], title="Home")
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(SE4_OWN_FEE,))
    existing = await add_price(hass)

    assert await migrate_again(hass)

    [made] = [entry for entry in prices(hass) if entry.entry_id != existing.entry_id]
    assert (existing.title, made.title) == ("Electricity price SE4", "Electricity price SE4 (Home)")


async def test_a_second_price_is_named_after_its_charger_or_its_count(hass: HomeAssistant, freezer) -> None:
    await two_chargers(hass)
    await setup_charger(hass, entry_id="entry_c", webhook_id="webhook-c", charge_control="switch.charger_c", title="Garage")
    freezer.move_to(NOW)
    await go_auto(hass, "entry_a", overrides=(SE4_OWN_FEE,))
    await go_auto(hass, "entry_b", overrides=(SE4_OWN_FEE,))
    company = replace(SE4_OWN_FEE, vat=FiscalOverride())
    await go_auto(hass, "entry_c", overrides=(company,))
    await add_price(hass)

    assert await migrate_again(hass)

    by_fees = {fees_of(entry): entry.title for entry in prices(hass)}
    assert by_fees[SE4_OWN_FEE] == "Electricity price SE4 (2 chargers)"
    assert by_fees[company] == "Electricity price SE4 (Garage)"


async def test_a_sole_price_keeps_the_forms_name(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass, title="Garage")
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(SE4_OWN_FEE,))

    assert await migrate_again(hass)

    assert [entry.title for entry in prices(hass)] == ["Electricity price SE4"]


async def test_a_name_after_its_users_stays_when_the_area_changes(hass: HomeAssistant, freezer) -> None:
    await setup_charger(hass)
    await setup_site(hass, charger_entry_ids=["entry_a"], title="Home")
    freezer.move_to(NOW)
    await go_auto(hass, overrides=(SE4_OWN_FEE,))
    existing = await add_price(hass)
    assert await migrate_again(hass)
    [made] = [entry for entry in prices(hass) if entry.entry_id != existing.entry_id]

    async_write_price_entry(hass, made, AreaAutoSettings(area_id="DK1"))
    await hass.async_block_till_done()

    assert made.data["area_id"] == "DK1"
    assert made.title == "Electricity price SE4 (Home)", "not the default name, so it does not follow the area"
