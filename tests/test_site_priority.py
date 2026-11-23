"""The site's one priority list (`site/priority.py`, `site/priority_store.py`): migration from the old solar and
charger priorities, completion, the values derived for older apps, both directions (the solar surplus and load
balancing top down, the shed order bottom up) and the write in the site settings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_CHARGER_PRIORITY,
    CONF_PRIORITY,
    CONF_SOLAR_PRIORITY,
)
from custom_components.spotnav.site.priority import (
    battery_first_in,
    charger_rank,
    complete,
    default_order,
    derived_charger_priorities,
    effective_order,
    legacy_order,
    merged,
    shed_sequence,
    solar_priority,
    with_charger_priority,
    with_solar_priority,
)
from custom_components.spotnav.site.regulator import overload_order
from custom_components.spotnav.site.shed_order import (
    ENTER_S,
    LOAD_BATTERY,
    LOAD_CAR,
    LOAD_HEAT,
    LOAD_POOL_HEAT,
    LOAD_WATER_HEATER,
    ORDER,
    Offer,
    Projection,
    ShedOrder,
)
from custom_components.spotnav.site.site_capacity import ChargerRequest, allocation_order

from .messages import update_site_settings_message
from .test_solar_charger_priority import _tick_at, _two_charger_site
from .world import admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay", "chargers_not_switch_only")

A, B, C = "charger:a", "charger:b", "charger:c"
TAIL = ["spa", "sauna", "cooling", "water_heater", "battery", "spa_idle", "pool_circulation", "pool_heat"]


# ------------------------------------------------------------------------------ the list itself


def test_the_default_order_is_the_owners() -> None:
    assert default_order(["a", "b"]) == ["heat", A, B, *TAIL]
    tail = [item for item in TAIL if item != "battery"]
    assert default_order(["a", "b"], battery_first=True) == ["heat", "battery", A, B, *tail]


@pytest.mark.parametrize(
    ("ranks", "solar", "expected"),
    [
        ({}, "car_first", ["heat", A, B, C, *TAIL]),
        ({}, "battery_first", ["heat", "battery", A, B, C, *[i for i in TAIL if i != "battery"]]),
        ({"c": "first"}, "car_first", ["heat", C, A, B, *TAIL]),
        ({"a": "last", "b": "first"}, "car_first", ["heat", B, C, A, *TAIL]),
        ({"a": "last", "b": "normal", "c": "first"}, "car_first", ["heat", C, B, A, *TAIL]),
        ({"a": "urgent"}, "car_first", ["heat", A, B, C, *TAIL]),
    ],
)
def test_the_old_settings_become_a_list(ranks: dict[str, str], solar: str, expected: list[str]) -> None:
    assert legacy_order(["a", "b", "c"], ranks, solar) == expected


def test_without_a_stored_list_the_old_keys_are_read_exactly() -> None:
    config = {CONF_CHARGER_ENTRY_IDS: ["a", "b"], CONF_SOLAR_PRIORITY: "battery_first"}
    assert solar_priority(config) == "battery_first"
    assert charger_rank(config, "a") is None
    assert effective_order(config, {"b": "first"})[:4] == ["heat", "battery", B, A]
    assert solar_priority({CONF_CHARGER_ENTRY_IDS: []}) == "car_first"


def test_a_stored_list_is_completed() -> None:
    chargers = ["a", "b", "c"]
    # An unknown id and a charger no longer on the site are dropped, a repeat too; a new charger goes below the
    # last car, a load the list lacks to its default place.
    stored = ["heat", "charger:gone", A, "mystery", "battery", B, A, "water_heater"]
    out = complete(stored, chargers)
    assert out[:3] == ["heat", A, "battery"]
    assert out.index(C) == out.index(B) + 1
    assert out.index("spa") == out.index(C) + 1
    # Directly below the nearest row above it in the default order that the list has.
    assert out.index("spa_idle") == out.index("battery") + 1
    assert sorted(out) == sorted(default_order(chargers))
    # Without any car left the fixed rows keep their places.
    found = complete(["water_heater", "heat"], [])
    assert found.index("water_heater") < found.index("heat")


def test_the_solar_priority_is_the_batterys_place_against_the_cars() -> None:
    config = {CONF_CHARGER_ENTRY_IDS: ["a", "b"], CONF_PRIORITY: ["heat", A, "battery", B]}
    # Above one car but not every one: car first.
    assert solar_priority(config) == "car_first"
    config[CONF_PRIORITY] = ["battery", "heat", A, B]
    assert solar_priority(config) == "battery_first"
    # A stored list wins over an old key left behind.
    config[CONF_SOLAR_PRIORITY] = "car_first"
    assert solar_priority(config) == "battery_first"
    # No car: the battery above the water heater.
    assert battery_first_in(["heat", "battery", "water_heater"]) is True
    assert battery_first_in(["heat", "water_heater", "battery"]) is False


def test_the_old_values_an_older_app_reads_and_writes() -> None:
    order = ["heat", A, B, C, *TAIL]
    assert derived_charger_priorities(order) == {"a": "first", "b": "normal", "c": "last"}
    assert derived_charger_priorities(["heat", A, *TAIL]) == {"a": "normal"}
    # "first" goes to the top of the cars, "last" to their bottom; the other rows stay where they are.
    assert with_charger_priority(order, "c", "first") == ["heat", C, A, B, *TAIL]
    assert with_charger_priority(order, "a", "last") == ["heat", B, C, A, *TAIL]
    # Classes as the app was shown them before a list was stored.
    shown = {"a": "normal", "b": "normal", "c": "normal"}
    assert with_charger_priority(order, "b", "first", shown) == ["heat", B, A, C, *TAIL]
    assert with_solar_priority(order, "battery_first")[:2] == ["heat", "battery"]
    assert with_solar_priority(with_solar_priority(order, "battery_first"), "car_first") == order
    assert with_solar_priority(order, "car_first") == order


def test_a_write_of_the_rows_shown_keeps_the_others_in_place() -> None:
    full = ["heat", A, "spa", "water_heater", "battery", "pool_heat"]
    assert merged(full, ["battery", "heat", A]) == ["battery", "heat", "spa", "water_heater", A, "pool_heat"]


def test_the_shed_order_is_the_list_bottom_up() -> None:
    assert ORDER == (
        "pool_heat", "pool_circulation", "spa_idle", LOAD_BATTERY, "water_heater", "cooling", "sauna", "spa",
        LOAD_CAR, "heat",
    )
    # Every car is one place, at the top car: a load between two cars gives way before the cars are touched.
    sequence = shed_sequence(complete(["heat", A, "water_heater", B, "battery"], ["a", "b"]))
    assert sequence.index("water_heater") < sequence.index(LOAD_CAR) < sequence.index("heat")
    assert sequence.count(LOAD_CAR) == 1


# ----------------------------------------------------------------- load balancing between chargers


def _request(entry_id: str, priority: str | int, order: int) -> ChargerRequest:
    return ChargerRequest(charger_entry_id=entry_id, requested_current_a=16.0, phases=3, priority=priority, order=order)


def test_load_balancing_serves_the_list_top_down_and_cuts_it_bottom_up() -> None:
    config = {CONF_CHARGER_ENTRY_IDS: ["a", "b", "c"], CONF_PRIORITY: ["heat", C, A, B]}
    requests = [_request(entry_id, charger_rank(config, entry_id), index) for index, entry_id in enumerate("abc")]  # type: ignore[arg-type]
    assert [request.charger_entry_id for request in allocation_order(requests)] == ["c", "a", "b"]
    assert [request.charger_entry_id for request in overload_order(requests)] == ["b", "a", "c"]
    # Before a list is stored, the old classes as ever.
    legacy = [_request("a", "normal", 0), _request("b", "first", 1), _request("c", "normal", 2)]
    assert [request.charger_entry_id for request in allocation_order(legacy)] == ["b", "a", "c"]


# ------------------------------------------------------------------------- the shed order follows it

HOUR = datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc)
HEATER = Offer(live=True, ready=True, give_w=2000.0)
HEAT = Offer(live=True, ready=True, give_w=1500.0)


def _projection(*, house: float, held: float = 0.0, rate: float = 5000.0, help_w: float = 0.0) -> Projection:
    return Projection(
        period_start=HOUR,
        period_end=HOUR + timedelta(hours=1),
        period_s=3600.0,
        remaining_s=3000.0,
        rate_w=rate,
        house_w=house,
        cars_held_w=held,
        battery_help_w=help_w,
    )


def _run(order: ShedOrder, seconds: float, offers: dict[str, Offer | None], **kwargs: float) -> None:
    now = HOUR + timedelta(minutes=5)
    for step in range(int(seconds / 5) + 1):
        order.update(now + timedelta(seconds=step * 5), _projection(**kwargs), offers)


def test_the_water_heater_above_the_cars_waits_for_them() -> None:
    """Over only with the cars at what they want: by default the water heater gives way; moved above the cars it
    does not (the cars give way first), and heat moved below the cars does."""
    offers: dict[str, Offer | None] = {LOAD_WATER_HEATER: HEATER, LOAD_HEAT: HEAT}
    default = ShedOrder()
    _run(default, ENTER_S + 10, offers, house=4000, held=3000)
    assert set(default.steps) == {LOAD_WATER_HEATER}

    moved = ShedOrder()
    moved.set_priority(complete(["water_heater", "heat", A], ["a"]))
    _run(moved, ENTER_S + 10, offers, house=4000, held=3000)
    assert moved.steps == {}

    heat_low = ShedOrder()
    heat_low.set_priority(complete([A, "heat", "water_heater"], ["a"]))
    _run(heat_low, ENTER_S + 10, {LOAD_HEAT: HEAT}, house=4000, held=3000)
    assert set(heat_low.steps) == {LOAD_HEAT}


def test_a_load_below_the_battery_is_judged_without_its_help() -> None:
    """The pool's heat pump gives way before the battery's help: over once that help is taken off, as a `would` step
    that holds no live step back."""
    pool = Offer(live=False, ready=True, give_w=3000.0)
    order = ShedOrder()
    _run(order, ENTER_S + 10, {LOAD_POOL_HEAT: pool, LOAD_WATER_HEATER: HEATER}, house=4000, help_w=2000)
    assert set(order.steps) == {LOAD_POOL_HEAT}
    assert order.steps[LOAD_POOL_HEAT].live is False
    # Not over without the help counted: nothing.
    calm = ShedOrder()
    _run(calm, ENTER_S + 10, {LOAD_POOL_HEAT: pool}, house=4000)
    assert calm.steps == {}


def test_a_watched_load_holds_no_live_step_back() -> None:
    pool = Offer(live=False, ready=True, give_w=3000.0)
    order = ShedOrder()
    # Over with the cars: the pool's `would` step and the water heater's live one, each after its own delay.
    _run(order, ENTER_S + 10, {LOAD_POOL_HEAT: pool, LOAD_WATER_HEATER: HEATER}, house=4000, held=3000)
    assert set(order.steps) == {LOAD_POOL_HEAT, LOAD_WATER_HEATER}


def test_a_hard_rule_still_wins_over_the_list() -> None:
    """The water heater at the bottom of the list, but its own rule (a full heat due) keeps it on: no step."""
    order = ShedOrder()
    order.set_priority(complete(["heat", A, "battery", "water_heater"], ["a"]))
    blocked = Offer(live=True, ready=False, give_w=2000.0, why="full_heat_due")
    _run(order, ENTER_S + 10, {LOAD_WATER_HEATER: blocked}, house=6000, help_w=1000)
    assert order.steps == {}
    assert order.blocked == {LOAD_WATER_HEATER: "full_heat_due"}


# ---------------------------------------------------------------------- the write and both directions


async def test_the_list_is_written_in_the_site_settings_and_the_solar_split_follows_it(
    hass: HomeAssistant, hass_ws_client
) -> None:
    site_controller, coordinators, clocks, turn_on_calls = await _two_charger_site(hass, priority_a="first", priority_b=None)
    client = await admin(hass, hass_ws_client)
    view = (await ws_call(client, {"type": "spotnav/get_site_settings_view", "api_version": 1, "site_id": "pair_site"}))[
        "result"
    ]
    # Migrated from the old keys: `a` is First.
    assert view["priority"] == {
        "order": [{"id": "charger:a_charger", "name": "Charger a"}, {"id": "charger:b_charger", "name": "Charger b"}],
        "writable": True,
    }
    shown = ["charger:a_charger", "charger:b_charger"]

    async def write(changes: dict, expected: dict | None = None) -> dict:
        message = update_site_settings_message(charger_id=None, expected=expected or {}, changes=changes)
        message["site_id"] = "pair_site"
        return (await ws_call(client, message))["result"]

    for bad in (["charger:b_charger"], ["charger:b_charger", "charger:b_charger"], "b", ["charger:b_charger", "x"]):
        refused = await write({"priority": bad})
        assert refused["ok"] is False and refused["error"] == "spotnav_invalid_value"
    stale = await write({"priority": list(reversed(shown))}, {"priority": list(reversed(shown))})
    assert stale["error"] == "spotnav_conflict"
    both = await write({"priority": list(reversed(shown)), "solar_priority": "battery_first"})
    assert both["error"] == "spotnav_invalid_value"

    written = await write({"priority": ["charger:b_charger", "charger:a_charger"]}, {"priority": shown})
    assert written["ok"] is True
    assert [row["id"] for row in written["priority"]["order"]] == ["charger:b_charger", "charger:a_charger"]
    site = hass.config_entries.async_get_entry("pair_site")
    assert site.data[CONF_PRIORITY][:3] == ["heat", "charger:b_charger", "charger:a_charger"]
    assert site_controller.config[CONF_PRIORITY] == site.data[CONF_PRIORITY]
    # The old keys are dropped.
    assert CONF_CHARGER_PRIORITY not in hass.config_entries.async_get_entry("a_charger").data
    assert site_controller.charger_priority("b_charger") < site_controller.charger_priority("a_charger")

    # The solar surplus goes to `b` first now.
    from .world import set_site_power_w  # noqa: PLC0415

    set_site_power_w(hass, "pair_site", -1_400.0)
    await _tick_at(hass, site_controller, clocks, 0.0)
    assert coordinators["b"].state.state == "arming"
    assert coordinators["a"].state.state == "off"

    # Over a webhook the list is read only.
    from custom_components.spotnav.api.site_settings import async_webhook_update_site_settings  # noqa: PLC0415

    status, body = await async_webhook_update_site_settings(
        hass, hass.config_entries.async_get_entry("a_charger"), {"expected": {}, "changes": {"priority": shown}}
    )
    assert status == 403 and body["error"] == "spotnav_not_permitted_over_webhook"


async def test_an_older_apps_writes_move_the_rows(hass: HomeAssistant, hass_ws_client) -> None:
    site_controller, _coordinators, _clocks, _calls = await _two_charger_site(hass, priority_a=None, priority_b=None)
    client = await admin(hass, hass_ws_client)
    message = update_site_settings_message(
        charger_id="a_charger", expected={"solar_priority": "car_first"}, changes={"solar_priority": "battery_first"}
    )
    answer = (await ws_call(client, message))["result"]
    assert answer["ok"] is True and answer["site"]["solar_priority"] == "battery_first"
    assert site_controller.config[CONF_PRIORITY][:4] == ["heat", "battery", "charger:a_charger", "charger:b_charger"]

    priority = {
        "type": "spotnav/update_entity_config",
        "api_version": 1,
        "charger_id": "b_charger",
        "scope": "charger",
        "expected": {"charger_priority": "last"},
        "changes": {"charger_priority": "first"},
    }
    result = (await ws_call(client, priority))["result"]
    assert result["ok"] is True
    assert site_controller.config[CONF_PRIORITY][:4] == ["heat", "battery", "charger:b_charger", "charger:a_charger"]
    # Still battery first: the battery stays above the cars.
    from custom_components.spotnav.site.priority import solar_priority as derived  # noqa: PLC0415

    assert derived(site_controller.config) == "battery_first"
