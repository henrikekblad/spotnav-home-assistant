"""The entity path for a write that committed and whose reconcile then failed.

One real charger entry, one real Auto preview and one real service call through Home Assistant's
service boundary. The `_reconcile` failure is injected *after* the fixture has set the charger up, so
only the call under test meets it.
"""

from __future__ import annotations


import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service


from custom_components.spotnav.planning.auto_controller import (
    AutoPlannerController,
    SettingsReconcileError,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.entity import SpotNavAutoEntity
from tests.world import call, entity_id, go_auto, settings_of, setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

CURRENT_LIMIT = "number.charger_a_limit"


def forbid_charger_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Close every route from this layer to a charger and report what tried to use one.

    The controller methods are patched on the class, so no instance however obtained can schedule,
    start, stop or cancel anything; the switch services are mocked at the registry, because a call
    that then failed to reach a service would still have been made.
    """
    seen: list[str] = []

    def forbidden(name: str):
        async def record(*_args, **_kwargs):
            seen.append(f"ChargingController.{name}")
            raise AssertionError(f"a rendered failure reached ChargingController.{name}")

        return record

    for name in (
        "async_install",
        "async_start",
        "async_stop",
        "async_cancel",
        "async_follow_schedule",
        "async_enforce_target",
    ):
        if hasattr(ChargingController, name):
            monkeypatch.setattr(ChargingController, name, forbidden(name))
    return seen


async def test_a_committed_write_that_fails_to_reconcile_is_reported_and_rendered(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass, current_limit=CURRENT_LIMIT)
    amps = entity_id(hass, entry.entry_id, "charging_current", "number")
    # The charger's own limit and switch, for the "nothing physically wrote" half of this test.
    hass.states.async_set(CURRENT_LIMIT, "6")
    hass.states.async_set("switch.charger_a", "off")
    await go_auto(hass, entry.entry_id, amps=10)
    before = settings_of(hass, entry.entry_id)

    # Injected only now: the fixture's own setup and `go_auto` reconcile normally.
    async def failing_reconcile(_self, _committed):
        raise RuntimeError("the calculation exploded")

    monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_reconcile)

    renders: list[str] = []
    original_write_state = SpotNavAutoEntity.async_write_ha_state

    def counting_write_state(self) -> None:
        renders.append(self.entity_id)
        return original_write_state(self)

    monkeypatch.setattr(SpotNavAutoEntity, "async_write_ha_state", counting_write_state)
    charger_calls = forbid_charger_writes(monkeypatch)
    switch_on = async_mock_service(hass, "switch", "turn_on")
    switch_off = async_mock_service(hass, "switch", "turn_off")

    with pytest.raises(SettingsReconcileError) as failure:
        await call(hass, "number", "set_value", {"entity_id": amps, "value": 16})

    # 1. Committed exactly once: one new revision, and it carries the requested value.
    committed = settings_of(hass, entry.entry_id)
    assert committed.revision == before.revision + 1
    assert committed.amps == 16

    # 2/3. The caller sees the same stable error, carrying the committed record, with no prose from
    # the reconcile failure anywhere in it.
    assert failure.value.settings == committed
    assert failure.value.code == "spotnav_settings_reconcile_failed"
    assert "the calculation exploded" not in str(failure.value)

    # 4. The affected entity re-rendered (so a person sees the committed truth, not the request).
    assert renders.count(amps) == 1

    # 5. And what it renders is read from the committed store record.
    state = hass.states.get(amps)
    assert state is not None
    assert float(state.state) == 16.0

    # 6. Rendering the failure touched no charger: no schedule call, no switch write and no physical
    # current-limit write (its entity still reads what it read before).
    assert charger_calls == []
    assert switch_on == [] and switch_off == []
    assert hass.states.get(CURRENT_LIMIT).state == "6"
    assert hass.states.get("switch.charger_a").state == "off"
