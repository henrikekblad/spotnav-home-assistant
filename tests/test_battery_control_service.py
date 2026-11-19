"""The owner's test action and settings for the home battery control (`spotnav.battery_control_test`,
`spotnav.battery_control_settings`) and the card's command (`spotnav/battery_control`), against the fake plant of
`test_battery_control.py`."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import gate
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.runtime import site_controller_for
from tests.test_battery_control import DISCHARGE, SWITCH, _site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_the_test_action_is_for_administrators_refused_while_shadow_only_unless_forced(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], hass_admin_user: Any,
    hass_read_only_user: Any,
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, own=True)
    await hass.async_block_till_done()
    control = controller.battery_control
    assert control._started
    admin = Context(user_id=hass_admin_user.id)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "battery_control_test", {"action": "hold"}, blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(DOMAIN, "battery_control_test", {"action": "hold"}, blocking=True, context=admin)
    assert refused.value.translation_key == "battery_control_refused"
    assert refused.value.translation_placeholders == {"reason": "not_enabled"}
    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN, "battery_control_test", {"action": "hold", "force_test": True}, blocking=True, context=admin
        )
    assert refused.value.translation_placeholders == {"reason": "not_enabled"}, "forcing never lifts the opt-in"
    await hass.services.async_call(
        DOMAIN, "battery_control_settings",
        {"battery_control_enabled": True, "battery_control_shadow_only": True}, blocking=True, context=admin,
    )
    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(DOMAIN, "battery_control_test", {"action": "hold"}, blocking=True, context=admin)
    assert refused.value.translation_placeholders == {"reason": "shadow_only"}
    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN, "battery_control_test", {"action": "export_discharge", "force_test": True}, blocking=True,
            context=admin,
        )
    assert refused.value.translation_placeholders == {"reason": "sell_not_allowed"}
    answer = await hass.services.async_call(
        DOMAIN, "battery_control_test", {"action": "hold", "minutes": 5, "force_test": True}, blocking=True,
        context=admin, return_response=True,
    )
    assert answer["trial"]["action"] == "hold" and plant.calls
    for index in range(4):
        await control.async_look(dt_util.now() + timedelta(seconds=10 * (index + 1)))
    assert hass.states.get(DISCHARGE).state == "0" and hass.states.get(SWITCH).state == "on"
    assert control.options() == gate.Options(enabled=True, shadow_only=True), "the test action changes no setting"
    answer = await hass.services.async_call(
        DOMAIN, "battery_control_test", {"action": "stop"}, blocking=True, context=admin, return_response=True,
    )
    assert answer == {"stopped": True}
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"


async def test_the_settings_service_and_the_card_command_write_the_options_without_a_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], hass_admin_user: Any,
    hass_ws_client: Any,
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, own=True)
    await hass.async_block_till_done()
    answer = await hass.services.async_call(
        DOMAIN, "battery_control_settings", {"battery_control_enabled": True, "battery_control_shadow_only": True},
        blocking=True, context=Context(user_id=hass_admin_user.id), return_response=True,
    )
    assert answer["options"][gate.CONF_ENABLED] is True and answer["state"] == "shadow_only"
    assert site_controller_for(hass, site.entry_id) is controller, "no reload"
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id})
    result = (await client.receive_json())["result"]
    assert result["ok"] and result["status"]["line"] == "shadow_only"
    await client.send_json({
        "id": 2, "type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
        "changes": {"battery_control_allow_sell": "yes"},
    })
    assert (await client.receive_json())["result"]["error"] == "spotnav_invalid_value"
    await client.send_json({
        "id": 3, "type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
        "changes": {"battery_control_ai_takeover_ack": True},
    })
    result = (await client.receive_json())["result"]
    assert result["ok"] and result["status"]["options"][gate.CONF_AI_TAKEOVER_ACK] is True
    assert plant.calls == []


async def test_the_battery_mode_is_written_validated_and_the_legacy_sell_option_is_not_writable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], hass_admin_user: Any,
    hass_read_only_user: Any, hass_ws_client: Any,
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, own=True)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    async def ask(changes: dict[str, Any], msg_id: int) -> dict[str, Any]:
        await client.send_json(
            {"id": msg_id, "type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
             "changes": changes}
        )
        return (await client.receive_json())["result"]

    assert (await ask({"battery_mode": "export"}, 1))["error"] == "spotnav_invalid_value"
    assert (await ask({"battery_mode": True}, 2))["error"] == "spotnav_invalid_value"
    assert (await ask({"battery_control_allow_sell": True}, 3))["error"] == "spotnav_invalid_value"
    assert gate.CONF_MODE not in site.data
    result = await ask({"battery_mode": "max_profit"}, 4)
    assert result["ok"] and result["status"]["options"]["battery_mode"] == "max_profit"
    assert result["status"]["options"]["battery_control_allow_sell"] is True, "derived, for one release"
    assert site.data[gate.CONF_MODE] == "max_profit" and site_controller_for(hass, site.entry_id) is controller
    result = await ask({"battery_mode": "self_consumption"}, 5)
    assert result["status"]["options"]["battery_mode"] == "self_consumption"
    assert controller.battery_control.options().mode == "self_consumption"
    assert controller.battery_shadow.snapshot()["plan"]["mode"] in ("balanced", "self_consumption", "max_profit")
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "battery_control_settings", {"battery_control_allow_sell": True}, blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
    answer = await hass.services.async_call(
        DOMAIN, "battery_control_settings", {"battery_mode": "balanced"}, blocking=True,
        context=Context(user_id=hass_admin_user.id), return_response=True,
    )
    assert answer["options"]["battery_mode"] == "balanced"
    assert plant.calls == []


async def test_an_administrator_clears_an_outside_control_verdict_from_the_battery_s_own_signals(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], hass_ws_client: Any,
    hass_read_only_access_token: str,
) -> None:
    from custom_components.spotnav.battery.outside_control import Verdict  # noqa: PLC0415

    site, controller, _plant = await _site(hass, monkeypatch, hass_storage, own=True)
    await hass.async_block_till_done()
    control, shadow = controller.battery_control, controller.battery_shadow
    shadow.outside.verdict = Verdict(True, "settings_changed", dt_util.now() - timedelta(hours=1), None, told=True)
    control.facts = control._read_facts()  # noqa: SLF001
    assert "outside_control:settings_changed" in control.status()["needs"]
    clear = {"type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
             "clear_outside_control": True}
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    await reader.send_json({"id": 1, **clear})
    assert (await reader.receive_json())["result"]["error"] == "spotnav_not_admin"
    assert shadow.outside.verdict.active
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, **clear})
    result = (await client.receive_json())["result"]
    assert result["ok"] and not any(need.startswith("outside_control") for need in result["status"]["needs"])
    assert not shadow.outside.verdict.active
    history = shadow.snapshot()["outside_control"]["history"]
    assert history[-1]["cleared_by"] == "person" and history[-1]["reason"] == "settings_changed"
    assert shadow.book.section("outside_control")["history"][-1]["cleared_by"] == "person", "kept in the store"
    await client.send_json({"id": 2, **clear})
    assert (await client.receive_json())["result"]["error"] == "spotnav_not_clearable"
    # A planner's verdict lasts while it is there.
    shadow.outside.verdict = Verdict(True, "planner:predbat", dt_util.now(), "Predbat")
    await client.send_json({"id": 3, **clear})
    assert (await client.receive_json())["result"]["error"] == "spotnav_not_clearable"
    assert shadow.outside.verdict.active
