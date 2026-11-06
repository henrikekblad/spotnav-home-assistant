"""A person's Stop at a charger that cannot see the car.

The field report of 2026-11-06: a Charge Amps charger through its cloud integration (`chargeamps`) and a Tesla. A
person pressed Stop, which holds until the car is unplugged. The integration stops by turning the connector off, and
a connector that is off says `Unavailable` with or without a car, so SpotNav never saw the unplug: the Stop held for
four days, and the phones were told the charge would not finish while no car was plugged in. The car's own plug
sensor said "unplugged" all along.

Now, while a person's Stop holds:

* no notice about the plan is told (a new plan, one at risk, one that did not start); the card still shows the plan;
* at a charger that otherwise says when a car is plugged in but says nothing now, the car's own plug sensor, read
  fresh since then, tells the unplug (which ends the Stop, as a charger's would) and the next plug-in, recorded as
  from the car's sensor; the charger's own reading always wins;
* with no such sensor, the card says to press Start or Resume (`stop_unplug_unseen`), never a phone;
* SpotNav never ends the Stop by itself, and never starts the charger while it holds.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.webhook import _for_app
from custom_components.spotnav.core.replay import replay
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.car_plug_fallback import SOURCE_CAR_PLUG
from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.runtime import charger_data, domain_data

from .test_plan_notify_noise import _go, at, plan_of
from .test_status_sensor_no_car import _night, _through_the_night, AT_RISK, FIRST, LAST, Night, relay, STATUS
from .test_push import FakeRelay
from .world import add_car, vehicle_entity

__all__ = ["relay"]

PLUG = "binary_sensor.tesla_charging_cable"
ON_TIME = SimpleNamespace(reason="ready", proposal=None, applied=True)


async def _with_car(hass: HomeAssistant, night: Night, *, plug: str | None = "on") -> str:
    """The Tesla as the charger's car, with its plug sensor saying `plug` (`None`: it has none)."""
    tesla = add_car(hass, "Tesla")
    if plug is not None:
        vehicle_entity(
            hass, device_id=tesla, domain="binary_sensor", object_id="tesla_charging_cable", state=plug,
            attributes={"device_class": "plug"},
        )
    await domain_data(hass).auto_store.async_update(
        night.entry.entry_id,
        mutate=lambda settings: replace(settings, target=TargetSocIntent(vehicle_id=tesla, target_percent=40.0)),
    )
    await hass.async_block_till_done()
    return tesla


def _executor(night: Night) -> Any:
    executor = charger_data(night.hass, night.entry.entry_id).executor
    assert executor is not None
    return executor


async def _stop(night: Night) -> None:
    """The person presses Stop in the card: the charger is turned off, its connector then says `off_status`."""
    await _executor(night).async_manual_stop()
    await night.hass.async_block_till_done()
    assert night.controller.held_off_by_person


def _status(night: Night) -> list[str]:
    """The card's line under the pause: the dashboard's fact (its settings are incomplete here, so the composed block
    says that instead; `test_status_compose.py` composes the line from the fact)."""
    capture = dashboard_api.capture_dashboard(night.hass, night.entry)
    return ["stop_unplug_unseen"] if capture.live.stop_unplug_unseen else []


def _car_says(hass: HomeAssistant, value: str) -> None:
    hass.states.async_set(PLUG, value, {"device_class": "plug"})


def _car_reports_again(hass: HomeAssistant) -> None:
    """A poll that writes the same value: only `last_reported` moves."""
    current = hass.states.get(PLUG)
    hass.states.async_set(PLUG, current.state, current.attributes, force_update=False)


def _connection_events(night: Night) -> list[dict[str, Any]]:
    return [
        record["event"]
        for record in night.controller.session_runner.events
        if record["event"]["kind"] in ("plug_in", "unplug")
    ]


async def _charging(hass: HomeAssistant, freezer: Any, night: Night) -> None:
    """The car charges in the plan's open window, as the plan started it."""
    await night.controller.async_install(plan_of([FIRST, LAST], 30.0))
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=5))
    assert night.starts, "the plan's window starts the car"
    assert night.controller.charging and night.controller.adapter.vehicle_connected() is True


# ------------------------------------------------------------------ the car's plug sensor stands in


async def test_the_cars_plug_sensor_ends_the_stop_at_a_charger_that_cannot_tell_and_the_plug_in_runs_the_plan(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night)
    await _charging(hass, freezer, night)

    await _stop(night)
    assert hass.states.get(STATUS).state == "Unavailable"
    adapter = night.controller.adapter
    assert adapter.charger_vehicle_connected() is None, "a connector turned off cannot tell"
    assert adapter.vehicle_connected() is None
    # The sensor said "plugged in" before the Stop: not fresh, so it can tell nothing yet.
    assert "stop_unplug_unseen" in _status(night)
    starts = len(night.starts)

    # The car's cloud reports again: a fresh "plugged in", so an unplug will be seen.
    _car_reports_again(hass)
    await hass.async_block_till_done()
    assert adapter.vehicle_connected() is True
    assert "stop_unplug_unseen" not in _status(night)
    assert night.controller.held_off_by_person

    # The Stop cleared the plan; a plan in force again (Auto's next one) with its window open starts nothing.
    await night.controller.async_install(plan_of([FIRST, LAST], 30.0))
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=20))
    assert len(night.starts) == starts and night.controller.held_off_by_person

    # The car is unplugged: its sensor says so, and the Stop ends as at a charger's own unplug.
    _car_says(hass, "off")
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person
    assert night.controller.session_runner.session.manual is None
    assert night.controller.known_connected is False
    assert _connection_events(night)[-1] == {"kind": "unplug", "previous": True, "source": SOURCE_CAR_PLUG}
    assert len(night.starts) == starts, "nothing starts for a car that left"
    # The rest of SpotNav hears the same unplug: the car's identification (a guest car would leave with it).
    identifier = charger_data(hass, night.entry.entry_id).identifier
    assert identifier is not None and identifier._connected is False  # noqa: SLF001
    facts = night.controller.car_plug_facts()
    assert facts["last"]["connected"] is False and facts["entity_id"] == PLUG
    diagnostics = await async_get_config_entry_diagnostics(hass, night.entry)
    assert diagnostics["controller"]["car_plug"]["last"]["connected"] is False

    # Plugged in again: a plug-in, and the plan's open window starts it as for any plug-in.
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=30))
    _car_says(hass, "on")
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=31))
    assert _connection_events(night)[-1] == {"kind": "plug_in", "previous": False, "source": SOURCE_CAR_PLUG}
    assert len(night.starts) == starts + 1, "the plan runs as for a new plug-in"
    # The connector is on again and says the car itself: the charger's reading counts from now on.
    assert adapter.charger_vehicle_connected() is True
    assert not night.controller.car_plug_facts()["armed_since"]

    # What the core decided replays from the record.
    report = replay(list(night.controller.session_runner.events))
    assert not report.differs_from_recording


async def test_the_chargers_own_reading_wins_and_the_cars_sensor_is_not_read(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    """A connector that still says the car is there while it is stopped (`SuspendedEVSE`)."""
    night = await _night(hass, freezer, off_status="SuspendedEVSE", car=True)
    await _with_car(hass, night)
    await _charging(hass, freezer, night)
    await _stop(night)
    starts = len(night.starts)
    assert night.controller.adapter.charger_vehicle_connected() is True

    _car_says(hass, "off")
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=20))
    assert night.controller.held_off_by_person, "the charger says the car is there: it wins"
    assert night.controller.adapter.vehicle_connected() is True
    assert "stop_unplug_unseen" not in _status(night)

    # The charger says the car left while the car's sensor says it is plugged in: the charger wins.
    _car_says(hass, "on")
    hass.states.async_set(STATUS, "Available")
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person
    assert _connection_events(night)[-1] == {"kind": "unplug", "previous": True, "source": None}
    assert len(night.starts) == starts


async def test_a_reading_from_before_the_stop_never_ends_it(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    """The car's sensor said "unplugged" before the Stop and says nothing since: it ends nothing."""
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night, plug="off")
    await _charging(hass, freezer, night)
    await _stop(night)
    for minutes in (10, 20, 40, 60):
        await _go(hass, freezer, FIRST[0] + timedelta(minutes=minutes))
    assert night.controller.held_off_by_person
    assert night.controller.known_connected is True
    assert "stop_unplug_unseen" in _status(night)
    # A Stop nothing can see end is never ended by SpotNav itself: the plan's later window starts nothing.
    starts = len(night.starts)
    await _go(hass, freezer, LAST[0] + timedelta(minutes=5))
    assert len(night.starts) == starts and night.controller.held_off_by_person


async def test_after_a_restart_the_cars_plugged_in_keeps_the_stop_and_starts_nothing(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    """A reload while the Stop holds: the first connection known is the car's sensor's "plugged in", which is not a
    new plug-in. The Stop holds and nothing starts; the unplug after it still ends it."""
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night)
    await _charging(hass, freezer, night)
    await _stop(night)
    starts = len(night.starts)
    assert await hass.config_entries.async_reload(night.entry.entry_id)
    await hass.async_block_till_done()
    assert night.controller.held_off_by_person, "the Stop is kept across the restart"
    await night.controller.async_install(plan_of([FIRST, LAST], 30.0))
    await hass.async_block_till_done()

    _car_reports_again(hass)
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=20))
    assert _connection_events(night)[-1] == {"kind": "plug_in", "previous": None, "source": SOURCE_CAR_PLUG}
    assert night.controller.held_off_by_person
    assert len(night.starts) == starts, "a person's Stop holds: nothing starts"

    _car_says(hass, "off")
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person


async def test_with_no_car_sensor_the_stop_holds_the_card_says_press_start_and_no_phone_is_told(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night, plug=None)
    await _charging(hass, freezer, night)
    await _stop(night)
    told = list(night.told())
    pushed = len(relay.requests)
    starts = len(night.starts)

    assert _status(night) == ["stop_unplug_unseen"]
    # An app that does not read the line is not sent it; one that does is.
    payload = {
        "status": {
            "tone": "normal",
            "lines": [
                {"code": "paused", "params": {"until": None, "choice": "manual", "action": "stop", "ends": "unplug"}},
                {"code": "stop_unplug_unseen", "params": {}},
            ],
        }
    }
    old_app = _for_app(payload, {})
    assert "stop_unplug_unseen" not in [line["code"] for line in old_app["status"]["lines"]]
    assert old_app["status"]["tone"] == payload["status"]["tone"]
    new_app = _for_app(payload, {"reads": ["stop_unplug_unseen"]})
    assert "stop_unplug_unseen" in [line["code"] for line in new_app["status"]["lines"]]

    # The whole night: a new plan, a departure at risk, windows that do not start. Nothing is told, nothing starts.
    await _go(hass, freezer, at(20))
    await night.controller.async_install(plan_of([(at(1, day=1), at(2, day=1)), (at(4, 15, day=1), at(5, 15, day=1))], 30.0))
    await hass.async_block_till_done()
    await _go(hass, freezer, at(1, 57, day=1))
    night.notifier._on_snapshot(AT_RISK)
    await hass.async_block_till_done()
    for hour in range(2, 9):
        await _go(hass, freezer, at(hour, day=1))
    assert night.told() == told and len(relay.requests) == pushed, "no notice about the plan under a person's Stop"
    assert len(night.starts) == starts
    assert night.controller.held_off_by_person, "SpotNav never ends a person's Stop by itself"
    assert "stop_unplug_unseen" in _status(night)


# ------------------------------------------------------------------ the plan's notices under a person's Stop


async def test_a_person_stop_at_a_charger_that_tells_silences_the_plans_notices_until_resume(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="SuspendedEVSE", car=True)
    await _stop(night)
    assert "stop_unplug_unseen" not in _status(night), "this charger sees the unplug"
    await _through_the_night(hass, freezer, night)
    assert night.told() == [] and relay.requests == [], "no plan notice under a person's Stop"
    assert night.starts == [], "nothing starts under a person's Stop"
    assert night.notifier.plan_notice.get("seq", 0) == 0, "no new plan counted for the app either"

    # The person resumes the plan: the Stop is over, and the plan's notices come back.
    await _executor(night).async_manual_follow()
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person
    await _at_risk_again(hass, night)
    assert night.told() and relay.requests, "a departure at risk is told again"


async def _at_risk_again(hass: HomeAssistant, night: Night) -> None:
    night.notifier._on_snapshot(ON_TIME)
    await hass.async_block_till_done()
    night.notifier._on_snapshot(AT_RISK)
    await hass.async_block_till_done()


async def test_the_plans_notices_come_back_after_an_unplug_and_a_plug_in(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="SuspendedEVSE", car=True)
    await _stop(night)
    await night.controller.async_install(plan_of([(at(20), at(21))], 10.0))
    await hass.async_block_till_done()
    await _at_risk_again(hass, night)
    await _go(hass, freezer, at(14, 10))
    assert night.told() == [] and relay.requests == []

    hass.states.async_set(STATUS, "Available")
    night.car = False
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person, "the unplug ends the Stop"
    await _go(hass, freezer, at(14, 20))
    await night.plug_in()
    await _go(hass, freezer, at(14, 21))
    await _at_risk_again(hass, night)
    assert night.told() and relay.requests, "a departure at risk is told again"


# ------------------------------------------------------------------ after the Stop


async def test_a_start_after_the_cars_sensor_ended_the_stop_is_never_refused_for_the_cars_word(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    """The car's sensor told the unplug; the person plugs in again and presses Start before the car's cloud says
    "plugged in". Only the charger's own word refuses a Start, and it says nothing."""
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night)
    await _charging(hass, freezer, night)
    await _stop(night)
    _car_reports_again(hass)
    _car_says(hass, "off")
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person
    assert night.controller.adapter.vehicle_connected() is False, "the car's sensor stands in until the plug-in"
    starts = len(night.starts)

    await _executor(night).async_manual_start()
    await hass.async_block_till_done()
    assert len(night.starts) == starts + 1, "the person's Start goes out"
    person_start = [
        record["event"]
        for record in night.controller.session_runner.events
        if record["event"]["kind"] == "person_start"
    ][-1]
    assert person_start["connected"] is None, "the charger's own word, not the car's sensor's"
    # The connector, on again, says the car is there: the first it states since the car's sensor stood in, which
    # ends no plug-in, so the person's Start holds, in the core and the stored pause alike.
    assert _connection_events(night)[-1] == {"kind": "plug_in", "previous": None, "source": None}
    manual = night.controller.session_runner.session.manual
    assert (manual.action, manual.scope) == ("start", "plug_in")
    pause = domain_data(hass).auto_store.settings(night.entry.entry_id).pause
    assert (pause.action, pause.scope) == ("start", "plug_in")


async def test_after_resume_the_cars_sensor_no_longer_stands_in(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night)
    await _charging(hass, freezer, night)
    await _stop(night)
    assert night.controller.car_plug_facts()["armed_since"]
    events = len(_connection_events(night))

    await _executor(night).async_manual_follow()
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person
    assert night.controller.car_plug_facts()["armed_since"] is None

    _car_says(hass, "off")
    await hass.async_block_till_done()
    await _go(hass, freezer, FIRST[0] + timedelta(minutes=20))
    assert len(_connection_events(night)) == events, "no unplug from the car's sensor outside a person's Stop"
    assert night.controller.car_plug_facts()["armed_since"] is None


async def test_the_card_line_comes_back_when_the_cars_sensor_goes_unavailable(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="Unavailable", car=True)
    await _with_car(hass, night)
    await _charging(hass, freezer, night)
    await _stop(night)
    _car_reports_again(hass)
    await hass.async_block_till_done()
    assert _status(night) == []
    hass.states.async_set(PLUG, "unavailable", {"device_class": "plug"})
    await hass.async_block_till_done()
    assert _status(night) == ["stop_unplug_unseen"]
    assert night.controller.held_off_by_person, "a sensor gone unavailable is no unplug"


async def test_a_departure_at_risk_under_the_stop_is_told_once_when_the_person_resumes(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="SuspendedEVSE", car=True)
    await _stop(night)
    await night.controller.async_install(plan_of([(at(20), at(21))], 10.0))
    await hass.async_block_till_done()
    night.notifier._on_snapshot(AT_RISK)
    await hass.async_block_till_done()
    await _go(hass, freezer, at(14, 10))
    assert night.told() == [] and relay.requests == []

    await _executor(night).async_manual_follow()
    await hass.async_block_till_done()
    night.notifier._on_snapshot(AT_RISK)
    await hass.async_block_till_done()
    assert len(night.told()) == 1, "told once the Stop is over, as it still holds"
    night.notifier._on_snapshot(AT_RISK)
    await _go(hass, freezer, at(14, 20))
    assert len(night.told()) == 1, "and only once"


async def test_a_departure_at_risk_under_the_stop_is_dropped_by_the_unplug_that_ends_it(
    hass: HomeAssistant, freezer: Any, relay: FakeRelay
) -> None:
    night = await _night(hass, freezer, off_status="SuspendedEVSE", car=True)
    await _stop(night)
    await night.controller.async_install(plan_of([(at(20), at(21))], 10.0))
    await hass.async_block_till_done()
    night.notifier._on_snapshot(AT_RISK)
    await hass.async_block_till_done()

    hass.states.async_set(STATUS, "Available")
    night.car = False
    await hass.async_block_till_done()
    assert not night.controller.held_off_by_person
    night.notifier._on_snapshot(AT_RISK)
    await _go(hass, freezer, at(14, 20))
    assert night.told() == [] and relay.requests == [], "nothing is told for a car that left"
