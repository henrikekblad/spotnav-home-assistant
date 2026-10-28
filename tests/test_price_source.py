"""Where a charger's price area and fees come from (`pricing/price_source.py`), and the readers that follow it.

Real config entries on the offline relay (SE4, today 2026-09-22 and tomorrow published), at a frozen clock.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.settings import settings_answer
from custom_components.spotnav.const import CONF_PRICE_ENTRY_ID, DOMAIN, ENTRY_TYPE_PRICE
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.auto_execution import application_for
from custom_components.spotnav.planning.auto_settings import AreaAutoSettings, AutoSettings, FiscalOverride
from custom_components.spotnav.pricing.price_source import (
    effective_settings,
    price_source_for,
    source_revision,
    VIA_CHARGER,
    VIA_ONLY,
    VIA_OWN,
    VIA_SITE,
)
from custom_components.spotnav.runtime import executor_for, preview_for
from custom_components.spotnav.sessions.inputs import current_fiscal, price_market

from .helpers import make_entry, make_site_entry
from .relay import serve, SE4
from .test_price_device import add_price, NOW, SUGGESTED
from .world import go_auto, settings_of, setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

OFF = {"enabled": False, "value": None}
#: The charger's own SE4 row: VAT as suggested, its own tax, no grid fee.
OWN_SE4 = AreaAutoSettings(
    area_id=SE4,
    vat=FiscalOverride(enabled=True),
    tax=FiscalOverride(enabled=True, value=42.0),
    transfer=FiscalOverride(enabled=False),
)
#: A row for an area the charger used before.
OWN_DK1 = AreaAutoSettings(area_id="DK1", vat=FiscalOverride(enabled=True), transfer=FiscalOverride(enabled=True, value=5.0))


def price_entry(hass: HomeAssistant, entry_id: str, area: str = SE4, **fees: Any) -> MockConfigEntry:
    """A price entry added to Home Assistant but not set up: the resolver reads only its data."""
    data = {"entry_type": ENTRY_TYPE_PRICE, "area_id": area, "vat": SUGGESTED, "tax": SUGGESTED, "transfer": SUGGESTED}
    data.update(fees)
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id, title=f"Electricity price {area}", data=data)
    entry.add_to_hass(hass)
    return entry


def charger_entry(hass: HomeAssistant, entry_id: str = "entry_a", **extra: Any) -> MockConfigEntry:
    return make_entry(
        hass,
        entry_id=entry_id,
        charge_control=f"switch.{entry_id}",
        current_limit=None,
        webhook_id=f"webhook-{entry_id}",
        title=entry_id,
        extra=extra or None,
    )


def point(hass: HomeAssistant, entry: MockConfigEntry, price_entry_id: str | None) -> None:
    data = {key: value for key, value in entry.data.items() if key != CONF_PRICE_ENTRY_ID}
    if price_entry_id is not None:
        data[CONF_PRICE_ENTRY_ID] = price_entry_id
    hass.config_entries.async_update_entry(entry, data=data)


# ------------------------------------------------------------------ the order


async def test_the_source_is_the_chargers_pointer_then_the_sites_then_the_only_entry_then_its_own(
    hass: HomeAssistant,
) -> None:
    own = AutoSettings(area_id="DK1", overrides=(OWN_DK1,))
    charger = charger_entry(hass)
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=["entry_a"])

    # Nothing names an area: no source at all (the "area" an Auto plan misses).
    assert price_source_for(hass, "entry_a", AutoSettings()) is None
    # No price entry: the charger's own settings, as before.
    source = price_source_for(hass, "entry_a", own)
    assert (source.via, source.entry_id, source.area_id, source.overrides) == (VIA_OWN, None, "DK1", OWN_DK1)
    assert source.plan_revision is None
    assert effective_settings(hass, "entry_a", own) is own

    # Exactly one price entry: every charger uses it, whatever its own settings say.
    price_entry(hass, "price_a", tax={"enabled": True, "value": 40.0})
    source = price_source_for(hass, "entry_a", own)
    assert (source.via, source.entry_id, source.area_id) == (VIA_ONLY, "price_a", SE4)
    assert source.overrides.tax == FiscalOverride(enabled=True, value=40.0)
    assert source.plan_revision == source.revision

    # Two: neither is "the only one", so the charger's own settings decide again.
    price_entry(hass, "price_b", area="DK1")
    assert price_source_for(hass, "entry_a", own).via == VIA_OWN

    # The site's pointer, and the charger's own over the site's.
    point(hass, site, "price_b")
    assert (price_source_for(hass, "entry_a", own).via, price_source_for(hass, "entry_a", own).entry_id) == (
        VIA_SITE,
        "price_b",
    )
    point(hass, charger, "price_a")
    assert (price_source_for(hass, "entry_a", own).via, price_source_for(hass, "entry_a", own).entry_id) == (
        VIA_CHARGER,
        "price_a",
    )

    # A pointer to an entry that is gone or disabled is passed over.
    point(hass, charger, "price_gone")
    assert price_source_for(hass, "entry_a", own).via == VIA_SITE
    await hass.config_entries.async_set_disabled_by("price_b", ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    source = price_source_for(hass, "entry_a", own)
    assert (source.via, source.entry_id) == (VIA_ONLY, "price_a")


async def test_the_revision_moves_with_the_area_and_every_fee() -> None:
    base = AreaAutoSettings(area_id=SE4, vat=FiscalOverride(enabled=True), tax=FiscalOverride(enabled=True, value=36.0))

    assert source_revision(base) == source_revision(replace(base))
    assert source_revision(base) != source_revision(replace(base, area_id="DK1"))
    assert source_revision(base) != source_revision(replace(base, tax=FiscalOverride(enabled=True, value=37.0)))
    assert source_revision(base) != source_revision(replace(base, vat=FiscalOverride(enabled=False)))
    assert source_revision(base) != source_revision(replace(base, transfer=FiscalOverride(enabled=True)))


async def test_effective_settings_take_the_sources_row_and_keep_the_others(hass: HomeAssistant) -> None:
    charger_entry(hass)
    own = AutoSettings(area_id="DK1", overrides=(OWN_DK1, OWN_SE4))
    price_entry(hass, "price_a", transfer={"enabled": True, "value": 10.0})

    seen = effective_settings(hass, "entry_a", own)

    assert seen.area_id == SE4
    assert seen.override_for("DK1") == OWN_DK1
    assert seen.override_for(SE4) == AreaAutoSettings(
        area_id=SE4,
        vat=FiscalOverride(enabled=True),
        tax=FiscalOverride(enabled=True),
        transfer=FiscalOverride(enabled=True, value=10.0),
    )
    # Everything that is not area or fees is the charger's own.
    assert replace(seen, area_id=own.area_id, overrides=own.overrides) == own


# ------------------------------------------------------------------ the planner


async def test_a_charger_with_no_area_misses_only_what_its_source_does_not_name(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    preview = preview_for(hass, "entry_a")
    assert "area" in preview.snapshot().missing

    await add_price(hass)

    snapshot = preview.snapshot()
    assert "area" not in snapshot.missing
    assert settings_of(hass, "entry_a").area_id is None, "nothing is written to the charger"


async def test_a_price_entrys_fee_change_replans_the_chargers_that_use_it_and_no_other(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    second = await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b")
    first = hass.config_entries.async_get_entry("entry_a")
    await go_auto(hass, "entry_a")
    await go_auto(hass, "entry_b")
    point(hass, first, "price_a")
    point(hass, second, "price_b")
    price = await add_price(hass)
    await add_price(hass, entry_id="price_b")
    a, b = preview_for(hass, "entry_a"), preview_for(hass, "entry_b")
    before_a, before_b = a.snapshot(), b.snapshot()
    assert before_a.state == "proposal_ready" and before_a.proposal_identity is not None
    assert before_a.price_revision == price_source_for(hass, "entry_a").revision
    assert before_a.settings_revision == settings_of(hass, "entry_a").revision

    hass.config_entries.async_update_entry(price, data={**price.data, "transfer": {"enabled": True, "value": 80.0}})
    await hass.async_block_till_done()

    after_a = a.snapshot()
    assert after_a.price_revision == price_source_for(hass, "entry_a").revision != before_a.price_revision
    assert after_a.proposal_identity != before_a.proposal_identity
    assert after_a.settings_revision == before_a.settings_revision, "the charger's own settings did not move"
    assert after_a.proposal.estimated_cost > before_a.proposal.estimated_cost
    # The charger on the other price entry is not even recalculated.
    assert b.snapshot() is before_b


async def test_the_executor_refuses_a_plan_priced_with_fees_the_entry_no_longer_has(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    await go_auto(hass)
    price = await add_price(hass)
    preview, executor = preview_for(hass, "entry_a"), executor_for(hass, "entry_a")
    stale = preview.snapshot()
    settings = effective_settings(hass, "entry_a", settings_of(hass, "entry_a"))
    application = application_for(settings, stale, 1)
    assert application is not None and application.price_revision == stale.price_revision
    assert executor._may_apply(application, executor.attempt)

    # The entry moves under a calculation that is still in flight: its plan is no longer installable.
    hass.config_entries.async_update_entry(price, data={**price.data, "tax": OFF})
    assert not executor._may_apply(application, executor.attempt)


async def test_a_charger_follows_its_sources_area_and_currency(hass: HomeAssistant, freezer, offline_relay) -> None:
    freezer.move_to(NOW)
    serve(offline_relay, "DK1")
    await setup_charger(hass)
    await go_auto(hass)
    assert preview_for(hass, "entry_a").snapshot().currency == "SEK"

    await add_price(hass, area="DK1")

    snapshot = preview_for(hass, "entry_a").snapshot()
    assert (snapshot.area_id, snapshot.currency) == ("DK1", "DKK")
    assert price_source_for(hass, "entry_a").currency == "DKK"
    assert price_market(hass, "entry_a").area.currency == "DKK"
    assert domain_data_owner_area(hass, "entry_a") == "DK1"


def domain_data_owner_area(hass: HomeAssistant, entry_id: str) -> str | None:
    from custom_components.spotnav.planning.auto_controller import OWNER_PREFIX
    from custom_components.spotnav.runtime import domain_data

    return domain_data(hass).price_refresh.owner_area(f"{OWNER_PREFIX}{entry_id}")


# ------------------------------------------------------------------ what the card, the app and sessions read


async def test_without_a_price_entry_the_settings_record_is_the_stored_one(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    charger = await setup_charger(hass)
    await go_auto(hass, overrides=(OWN_SE4, OWN_DK1))
    stored = settings_of(hass, "entry_a")

    record = dashboard_api.serialize_settings(dashboard_api.capture_dashboard(hass, charger))
    # What `get_settings`, `update_settings` and the app's webhook answer with.
    answer = settings_answer(hass, "entry_a", stored)

    assert dashboard_api.capture_dashboard(hass, charger).settings == stored
    assert record["area_id"] == SE4
    assert record["overrides"] == [
        {"area_id": item.area_id, **{key: value.as_dict() for key, value in (("vat", item.vat), ("tax", item.tax), ("transfer", item.transfer))}}
        for item in stored.overrides
    ]
    assert answer["settings"] == record


async def test_with_a_price_entry_the_card_and_the_app_read_its_area_and_fees(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    charger = await setup_charger(hass)
    await go_auto(hass, area_id="DK1", overrides=(OWN_SE4, OWN_DK1))
    await add_price(hass, tax=OFF, transfer={"enabled": True, "value": 12.5})

    capture = dashboard_api.capture_dashboard(hass, charger)
    record = dashboard_api.serialize_settings(capture)
    fiscal = dashboard_api.serialize_fiscal(capture.settings, capture.area_entry)
    answer = settings_answer(hass, "entry_a", settings_of(hass, "entry_a"))

    assert record["area_id"] == SE4
    assert record["revision"] == settings_of(hass, "entry_a").revision
    assert record["overrides"] == [
        {"area_id": "DK1", "vat": {"enabled": True, "value": None}, "tax": OFF, "transfer": {"enabled": True, "value": 5.0}},
        {"area_id": SE4, "vat": SUGGESTED, "tax": OFF, "transfer": {"enabled": True, "value": 12.5}},
    ]
    assert dashboard_api.serialize_market(capture)["area_id"] == SE4
    assert (fiscal["tax"]["policy"], fiscal["transfer"]["effective_value"]) == ("off", 12.5)
    assert answer["settings"] == record
    # The charger's own record is untouched: writes are not routed to the price entry yet.
    assert settings_of(hass, "entry_a").area_id == "DK1"


async def test_a_session_in_a_former_area_keeps_the_chargers_own_fees(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    await setup_charger(hass)
    await go_auto(hass, area_id="DK1", overrides=(OWN_SE4, OWN_DK1))
    await add_price(hass, tax=OFF, transfer={"enabled": True, "value": 12.5})

    former = current_fiscal(hass, "entry_a", "DK1")
    now = current_fiscal(hass, "entry_a", SE4)

    assert (former.transfer_enabled, former.transfer_minor_per_kwh, former.tax_enabled) == (True, 5.0, False)
    assert (now.transfer_minor_per_kwh, now.tax_enabled) == (12.5, False)
    assert current_fiscal(hass, "entry_a") == now
    assert price_market(hass, "entry_a").area_id == SE4


async def test_diagnostics_name_each_chargers_price_source(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    charger = await setup_charger(hass)
    assert (await async_get_config_entry_diagnostics(hass, charger))["price_source"] is None
    await go_auto(hass)
    own = (await async_get_config_entry_diagnostics(hass, charger))["price_source"]
    assert {key: own[key] for key in ("entry_id", "area_id", "via")} == {"entry_id": None, "area_id": SE4, "via": "own"}

    await add_price(hass)

    source = price_source_for(hass, "entry_a")
    assert (await async_get_config_entry_diagnostics(hass, charger))["price_source"] == {
        "entry_id": "price_a",
        "area_id": SE4,
        "revision": source.revision,
        "via": "only",
    }
