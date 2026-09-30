"""`solar` and `hybrid` are selectable and safe, with the price planner standing down for `solar`.

Everything here is about the boundary between "storable" and "executed": choosing `solar` must be
possible, must be visible, and must leave the price planner idle -- never a crash, never a plan. This
module pins:

* the settings write over the webhook reaching every strategy through the one contract;
* the strategy select: `solar` and `hybrid` offered only when the charger's site has derived
  measurement, and a write that goes through the exact same revisioned settings path every other
  Auto select uses;
* the dashboard's `strategy_options`, following exactly the rule the select applies;
* the price planner standing down for `solar` -- no plan is ever calculated, an Auto-owned plan
  already installed under `cheapest` is cleared through the executor's own lock, and switching back
  to `cheapest` resumes planning;
* the dashboard's `strategy` rows: `solar` and `hybrid` available when the site supports them, and
  every strategy named exactly once.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import (
    STRATEGY_CHEAPEST,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
    AutoSettings,
)
from custom_components.spotnav.const import MEASUREMENT_MODE_DERIVED, MEASUREMENT_MODE_DIRECT
from custom_components.spotnav.api.dashboard import STRATEGY_SOLAR_REASON
from custom_components.spotnav.api.settings import (
    SETTINGS_API_VERSION,
    SETTINGS_KEYS,
    encode_settings,
)

from .helpers import make_site_entry, webhook_dashboard
from .relay import StubTransport
from .world import call, entity_id, go_auto, setup_charger, settings_of
from .world import ws_call
from .world import controller_of
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import executor_for, preview_for

pytestmark = pytest.mark.usefixtures("offline_relay")


def _body(settings: AutoSettings, **changes: Any) -> dict[str, Any]:
    """A replacement body: the canonical value without its revision, changes applied."""
    encoded = encode_settings(settings)
    body = {key: value for key, value in encoded.items() if key != "revision"}
    body.update(changes)
    assert set(body) == SETTINGS_KEYS
    return body


@pytest.mark.parametrize("strategy", [STRATEGY_SOLAR, STRATEGY_HYBRID], ids=["solar", "hybrid"])
async def test_a_settings_write_over_the_webhook_can_change_strategy_to_solar_or_hybrid(
    hass: HomeAssistant, hass_client_no_auth, transport: StubTransport, strategy: str
) -> None:
    """The webhook's `settings` action is the one contract's write, so it reaches every strategy
    exactly as the WebSocket command does (`test_strategy_schema.py`)."""
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    before = store.settings(entry.entry_id)
    assert before.strategy == STRATEGY_CHEAPEST
    client = await hass_client_no_auth()
    body = _body(before, strategy=strategy)

    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1,
            "action": "settings",
            "api_version": 1,
            "expected_revision": before.revision,
            "settings": body,
        },
    )

    assert response.status == 200
    answer = await response.json()
    assert answer["ok"] is True and answer["error"] is None
    assert answer["api_version"] == SETTINGS_API_VERSION == 1
    after = store.settings(entry.entry_id)
    assert after.strategy == strategy
    assert after.revision == before.revision + 1
    assert answer["settings"] == encode_settings(after)


# --------------------------------------------------------------------------- the strategy select


async def test_strategy_select_offers_only_cheapest_with_no_site(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    select_id = entity_id(hass, entry.entry_id, "auto_strategy", "select")
    state = hass.states.get(select_id)
    assert state is not None
    assert state.attributes["options"] == [STRATEGY_CHEAPEST]
    assert state.state == STRATEGY_CHEAPEST


async def test_strategy_select_offers_only_cheapest_with_a_direct_mode_site(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    site = make_site_entry(
        hass,
        entry_id="site_direct",
        charger_entry_ids=["entry_a"],
        measurement_mode=MEASUREMENT_MODE_DIRECT,
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    entry = await setup_charger(hass)

    select_id = entity_id(hass, entry.entry_id, "auto_strategy", "select")
    assert hass.states.get(select_id).attributes["options"] == [STRATEGY_CHEAPEST]


async def test_strategy_select_offers_solar_with_a_derived_mode_site(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    site = make_site_entry(
        hass,
        entry_id="site_derived",
        charger_entry_ids=["entry_a"],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    entry = await setup_charger(hass)

    select_id = entity_id(hass, entry.entry_id, "auto_strategy", "select")
    assert hass.states.get(select_id).attributes["options"] == [
        STRATEGY_CHEAPEST, STRATEGY_SOLAR, STRATEGY_HYBRID,
    ]


async def test_selecting_solar_writes_through_the_same_revisioned_path_every_select_uses(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    site = make_site_entry(
        hass,
        entry_id="site_derived",
        charger_entry_ids=["entry_a"],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    entry = await setup_charger(hass)
    before = settings_of(hass, entry.entry_id)
    assert before.strategy == STRATEGY_CHEAPEST

    select_id = entity_id(hass, entry.entry_id, "auto_strategy", "select")
    await call(hass, "select", "select_option", {"entity_id": select_id, "option": STRATEGY_SOLAR})

    after = settings_of(hass, entry.entry_id)
    assert after.strategy == STRATEGY_SOLAR
    # One atomic write through the store's own compare-and-set, exactly like every other Auto
    # select's write (see `AutoAreaSelect`/`AutoPhasesSelect`, and `entity.SpotNavAutoEntity
    # .async_write_settings`) -- never a second, direct write to the store.
    assert after.revision == before.revision + 1
    assert hass.states.get(select_id).state == STRATEGY_SOLAR


# ------------------------------------------------------------- the dashboard's strategy_options
#
# The dashboard's `strategy_options` must follow exactly the rule `AutoStrategySelect.options`
# applies -- both go through the shared `strategy_options_for` (`select.py`), so these mirror the
# three select tests above one for one, cross-checked against the select entity's own live state
# rather than merely re-asserting the same literal list.


async def test_dashboard_strategy_options_is_cheapest_only_with_no_site(
    hass: HomeAssistant, hass_client_no_auth, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    select_id = entity_id(hass, entry.entry_id, "auto_strategy", "select")
    client = await hass_client_no_auth()

    status = await webhook_dashboard(client, "webhook-a")

    assert status["strategy_options"] == [STRATEGY_CHEAPEST]
    assert status["strategy_options"] == hass.states.get(select_id).attributes["options"]


async def test_dashboard_strategy_options_is_cheapest_only_with_a_direct_mode_site(
    hass: HomeAssistant, hass_client_no_auth, transport: StubTransport
) -> None:
    site = make_site_entry(
        hass,
        entry_id="site_direct",
        charger_entry_ids=["entry_a"],
        measurement_mode=MEASUREMENT_MODE_DIRECT,
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    entry = await setup_charger(hass)
    select_id = entity_id(hass, entry.entry_id, "auto_strategy", "select")
    client = await hass_client_no_auth()

    status = await webhook_dashboard(client, "webhook-a")

    assert status["strategy_options"] == [STRATEGY_CHEAPEST]
    assert status["strategy_options"] == hass.states.get(select_id).attributes["options"]


async def test_dashboard_strategy_options_offers_solar_and_hybrid_with_a_derived_mode_site(
    hass: HomeAssistant, hass_client_no_auth, transport: StubTransport
) -> None:
    site = make_site_entry(
        hass,
        entry_id="site_derived",
        charger_entry_ids=["entry_a"],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    entry = await setup_charger(hass)
    select_id = entity_id(hass, entry.entry_id, "auto_strategy", "select")
    client = await hass_client_no_auth()

    status = await webhook_dashboard(client, "webhook-a")

    assert status["strategy_options"] == [STRATEGY_CHEAPEST, STRATEGY_SOLAR, STRATEGY_HYBRID]
    assert status["strategy_options"] == hass.states.get(select_id).attributes["options"]


# ------------------------------------------------------- the price planner stands down for solar


@freeze_time("2026-09-22 06:00:00")
async def test_switching_to_solar_clears_an_installed_plan_and_switching_back_replans(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    snapshot = await go_auto(hass, entry.entry_id)
    assert snapshot.state == "proposal_ready" and snapshot.proposal is not None
    executor = executor_for(hass, entry.entry_id)
    assert executor is not None and executor.applied is not None
    controller = controller_of(hass, entry.entry_id)
    assert controller.plan is not None

    solar_snapshot = await go_auto(hass, entry.entry_id, strategy=STRATEGY_SOLAR)

    assert solar_snapshot.state == "planning_unavailable"
    assert solar_snapshot.reason == "solar_execution_unavailable"
    assert solar_snapshot.proposal is None
    # Cleared through `AutoExecutor.async_reconcile`'s own lock -- never a direct write to the
    # charger from outside the boundary, and never left running with nothing to recalculate it.
    assert executor.applied is None
    assert controller.plan is None

    cheapest_snapshot = await go_auto(hass, entry.entry_id, strategy=STRATEGY_CHEAPEST)

    assert cheapest_snapshot.state == "proposal_ready"
    assert executor.applied is not None
    assert controller.plan is not None


async def test_solar_strategy_never_plans_anything_from_a_blank_start(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """No plan is ever calculated for `solar`, not even the first time -- the price planner stands
    down before an area, prices or a proposal are ever consulted."""
    entry = await setup_charger(hass)

    snapshot = await go_auto(hass, entry.entry_id, strategy=STRATEGY_SOLAR)

    assert snapshot.state == "planning_unavailable"
    assert snapshot.reason == "solar_execution_unavailable"
    assert snapshot.proposal is None
    executor = executor_for(hass, entry.entry_id)
    assert executor is not None and executor.applied is None
    controller = controller_of(hass, entry.entry_id)
    assert controller.plan is None


async def test_pause_and_resume_behave_exactly_as_before_while_the_strategy_is_solar(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id, strategy=STRATEGY_SOLAR)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None

    await preview.async_pause()
    assert settings_of(hass, entry.entry_id).pause.admitted is True

    await preview.async_resume()
    assert settings_of(hass, entry.entry_id).pause.admitted is False


# ------------------------------------------------------------------------ dashboard strategy rows


async def test_dashboard_makes_solar_available_when_the_site_supports_it(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport
) -> None:
    site = make_site_entry(
        hass,
        entry_id="site_derived",
        charger_entry_ids=["entry_a"],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)

    frame = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )
    assert frame["success"] is True
    rows = {row["strategy"]: row for row in frame["result"]["strategy"]["available"]}
    assert rows["solar"] == {"strategy": "solar", "available": True, "reason": None}
    # Hybrid becomes available under the same site condition as solar.
    assert rows["hybrid"] == {"strategy": "hybrid", "available": True, "reason": None}


@pytest.mark.parametrize("selected", [STRATEGY_SOLAR, STRATEGY_HYBRID])
async def test_dashboard_names_each_strategy_once_while_solar_or_hybrid_is_selected(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport, selected: str
) -> None:
    """A selected `solar`/`hybrid` is stated by its own row, never beside a second "not available"
    row for the same strategy -- the contradiction that made the card refuse the whole dashboard
    after a charger was switched to solar. `cheapest` stays choosable while it is not the selected one.
    """
    site = make_site_entry(
        hass,
        entry_id="site_derived",
        charger_entry_ids=["entry_a"],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={},
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(entry.entry_id, mutate=lambda s: replace(s, strategy=selected))
    client = await hass_ws_client(hass)

    frame = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )
    assert frame["success"] is True
    block = frame["result"]["strategy"]
    names = [row["strategy"] for row in block["available"]]
    assert block["selected"] == selected
    assert len(names) == len(set(names)), f"a strategy is named twice: {names}"
    assert {row["strategy"]: row for row in block["available"]} == {
        name: {"strategy": name, "available": True, "reason": None}
        for name in (STRATEGY_CHEAPEST, STRATEGY_SOLAR, STRATEGY_HYBRID)
    }


async def test_dashboard_keeps_solar_disabled_without_site_support(
    hass: HomeAssistant, hass_ws_client, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)

    frame = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )

    rows = {row["strategy"]: row for row in frame["result"]["strategy"]["available"]}
    assert rows["solar"] == {
        "strategy": "solar",
        "available": False,
        "reason": STRATEGY_SOLAR_REASON,
    }
