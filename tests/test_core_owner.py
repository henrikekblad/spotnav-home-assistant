"""The owner in the core's session (the core end state plan, step 7): while the core drives, who owns the charge is the
session's `owner`, and today's `charge_origin` and `plan_charge` are derived from it (`controller._CORE_OWNER_ORIGIN`),
read from it, and still written to today's keys, so an older build reads them as before. With the option off,
today's fields stay the truth. Every test here runs in both modes."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.runtime import executor_for

from .world import solar_setup

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture(params=[True, False], ids=["core", "previous"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


@pytest.mark.parametrize("cause", [None, "other"])
async def test_the_suns_take_over_never_stops_a_charge_now_start_on_its_way(
    hass: HomeAssistant, drives: bool, cause: str | None
) -> None:
    """A Charge-now start (a webhook's, a caller of `async_start`) whose charger reports charging before the command
    returns: the sun's take-over, decided meanwhile under the boundary's lock alone, must not take it for a charge the
    charger began by itself and stop it once the start is done."""
    charger, _site, controller, _coordinator, _clock, _on, turn_off_calls = await solar_setup(
        hass, strategy=STRATEGY_SOLAR
    )
    executor = executor_for(hass, charger.entry_id)
    switch = f"switch.{charger.entry_id}"
    answered = asyncio.Event()
    release = asyncio.Event()

    async def slow_start(*_args, **_kwargs) -> bool:
        # The charger takes the start and reports charging; the command itself returns later.
        hass.states.async_set(switch, "on")
        answered.set()
        await release.wait()
        return True

    controller.adapter.async_start = slow_start  # type: ignore[method-assign]
    start = hass.async_create_task(controller.async_start(cause=cause))
    await answered.wait()
    assert controller.charging and not controller.self_started_charge()

    take_over = hass.async_create_task(executor.async_solar_take_over_stop())
    await asyncio.sleep(0)
    release.set()
    assert await start
    stopped = await take_over
    await hass.async_block_till_done()

    assert not stopped and turn_off_calls == [], "the sun does not stop a Charge-now start"
    assert controller.charge_origin == "other"
    if drives:
        assert controller.ownership_shadow.session.owner == "charge_now"


@pytest.mark.shadow_disagreement_expected
async def test_the_owner_is_kept_as_the_core_decided_it_and_a_difference_is_mirror_drift(hass: HomeAssistant) -> None:
    """Owned by the core (`OWNER`): the line-up never takes it from today's state; a difference is counted and told as
    `mirror_drift` (not as drift), and the core's owner stays. Nobody's and the charger's own charge are one owner to
    it: which of them it is is seen now, and taken from today's state with no mirror drift."""
    from custom_components.spotnav.core import events as ev
    from custom_components.spotnav.core.session import ChargeSession

    today = {"session": ChargeSession(plugged=True, owner="person")}
    heard: list[str] = []
    ownership_shadow.LISTENERS.append(lambda kind, record: heard.append(kind))
    try:
        shadow = ownership_shadow.OwnershipShadow(lambda: today["session"], owned_fields=ownership_shadow.OWNER)
        shadow.session = today["session"]
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.counts["mirror_drift"] == 0
        today["session"] = today["session"].with_changes(owner="solar")
        shadow.feed(ev.ConnectionUnknown())
        assert heard.count("mirror_drift") == 1 and "drift" not in heard
        assert shadow.session.owner == "person", "never taken"
        shadow.session = shadow.session.with_changes(owner="none")
        today["session"] = today["session"].with_changes(owner="charger_self")
        heard.clear()
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.session.owner == "charger_self", "seen, not kept"
        today["session"] = today["session"].with_changes(owner="none")
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.session.owner == "none"
    finally:
        ownership_shadow.LISTENERS.pop()
    (record,) = [item for item in shadow.diagnostics()["drift"] if item.get("mirror")]
    assert record["fields"] == {"owner": {"core": "person", "today": "solar"}}
