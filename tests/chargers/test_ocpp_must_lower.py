"""OCPP: a lowering the fuse needs is read back before it counts, and the regulator's other writes keep a budget.

`ocpp.configure` answers alike whether the charge point accepted `AssignedCurrent` or rejected it, so a
must-lower write that the charge point does not confirm stops the charge instead of being taken as done.
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MODE_OCPP,
)
from custom_components.spotnav.execution.chargers import ocpp as ocpp_module
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_UNCONFIRMED,
    WRITE_REGULATOR,
)
from custom_components.spotnav.execution.controller import (
    ChargingController,
    REGULATED_HELD,
    REGULATED_STOPPED,
    REGULATED_WROTE,
)

SWITCH = "switch.cp_charge_control"


class ChargePoint:
    """The OCPP integration's two services for one charge point.

    `accepts`: a configured value becomes what is read back (else the old value stays, as after a
    `Rejected` that the integration only logs). `read_delay_s`: how long a `get_configuration` takes.
    `unanswered_after_write`: how many reads after a write give no value before the charge point answers.
    """

    def __init__(self, hass: HomeAssistant, value: str = "1.16") -> None:
        self.value = value
        self.accepts = True
        self.read_delay_s = 0.0
        self.unanswered_after_write = 0
        self._unanswered = 0
        self.configured: list[str] = []
        self.reads = 0
        hass.services.async_register(
            "ocpp", "get_configuration", self._get, supports_response=SupportsResponse.ONLY
        )
        hass.services.async_register(
            "ocpp", "configure", self._configure, supports_response=SupportsResponse.OPTIONAL
        )

    async def _get(self, call: ServiceCall) -> dict:
        self.reads += 1
        if self.read_delay_s:
            await asyncio.sleep(self.read_delay_s)
        if self._unanswered > 0:
            self._unanswered -= 1
            return {}
        return {"value": self.value}

    async def _configure(self, call: ServiceCall) -> dict:
        self.configured.append(call.data["value"])
        self._unanswered = self.unanswered_after_write
        if self.accepts:
            self.value = call.data["value"]
        return {"reboot_required": False}


@pytest.fixture
def short_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-back bound of half a second (5 s in use), so the tests run fast."""
    monkeypatch.setattr(ocpp_module, "OCPP_VERIFY_TIMEOUT_S", 0.5)
    monkeypatch.setattr(ocpp_module, "OCPP_VERIFY_RETRY_S", 0.1)


async def _charging_at_16(hass: HomeAssistant) -> tuple[ChargingController, ChargePoint, list[ServiceCall]]:
    """An OCPP charger set through ChangeConfiguration, charging at 16 A that SpotNav wrote."""
    hass.states.async_set(SWITCH, "on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    async_mock_service(hass, "switch", "turn_on")
    charge_point = ChargePoint(hass, "1.6")
    controller = ChargingController(
        hass,
        "entry_ocpp_verify",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: SWITCH,
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: "cp",
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    await controller.async_initialize()
    written = await controller.async_apply_regulated_current(16, must_lower=False)
    assert written.outcome == REGULATED_WROTE
    assert controller.adapter.current.last_written_a == 16
    return controller, charge_point, turn_off


async def test_a_charge_point_that_rejects_the_lowering_during_an_overload_is_stopped(
    hass: HomeAssistant, short_verify: None
) -> None:
    controller, charge_point, turn_off = await _charging_at_16(hass)
    charge_point.accepts = False

    result = await controller.async_apply_regulated_current(8, must_lower=True)

    assert charge_point.configured[-1] == "1.8", "the lowering was sent"
    assert (result.outcome, result.code) == (REGULATED_STOPPED, "safety_stop")
    assert [call.data["entity_id"] for call in turn_off] == [SWITCH]
    await controller.async_shutdown()


async def test_a_charge_point_that_never_answers_the_read_back_in_time_is_stopped(
    hass: HomeAssistant, short_verify: None
) -> None:
    controller, charge_point, turn_off = await _charging_at_16(hass)
    charge_point.read_delay_s = 2.0

    result = await controller.async_apply_regulated_current(8, must_lower=True)

    assert (result.outcome, result.code) == (REGULATED_STOPPED, "safety_stop")
    assert len(turn_off) == 1
    await controller.async_shutdown()


async def test_a_charge_point_that_accepts_the_lowering_is_not_stopped(
    hass: HomeAssistant, short_verify: None
) -> None:
    controller, charge_point, turn_off = await _charging_at_16(hass)

    result = await controller.async_apply_regulated_current(8, must_lower=True)

    assert (result.outcome, result.code, result.written) == (REGULATED_WROTE, ASSIGN_ASSIGNED, True)
    assert charge_point.value == "1.8"
    assert controller.adapter.current.last_written_a == 8
    assert turn_off == []
    await controller.async_shutdown()


async def test_a_slow_read_back_within_the_bound_is_not_stopped(hass: HomeAssistant, short_verify: None) -> None:
    controller, charge_point, turn_off = await _charging_at_16(hass)
    charge_point.read_delay_s = 0.15
    charge_point.unanswered_after_write = 1

    result = await controller.async_apply_regulated_current(8, must_lower=True)

    assert result.outcome == REGULATED_WROTE
    assert turn_off == []
    await controller.async_shutdown()


async def test_an_unconfirmed_lowering_the_fuse_does_not_need_is_not_a_stop(
    hass: HomeAssistant, short_verify: None
) -> None:
    """Only a must-lower write is read back: an optional one keeps today's behaviour."""
    controller, charge_point, turn_off = await _charging_at_16(hass)
    charge_point.accepts = False
    reads_before = charge_point.reads

    result = await controller.async_apply_regulated_current(10, must_lower=False)

    assert result.outcome == REGULATED_WROTE
    assert charge_point.reads == reads_before + 1, "the read before the write, no read-back"
    assert turn_off == []
    await controller.async_shutdown()


async def test_the_regulators_writes_keep_a_budget_that_a_must_lower_write_ignores(
    hass: HomeAssistant, short_verify: None
) -> None:
    controller, charge_point, turn_off = await _charging_at_16(hass)
    current = controller.adapter.current
    budget = current.policy.max_writes_per_minute
    assert budget is not None and budget > 1
    # The 16 A of the setup was the first write of the minute.
    for amps in range(15, 16 - budget, -1):
        assert await current.async_set(amps, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    sent = len(charge_point.configured)

    held = await controller.async_apply_regulated_current(12, must_lower=False)

    assert (held.outcome, held.code) == (REGULATED_HELD, ASSIGN_RATE_LIMITED)
    assert len(charge_point.configured) == sent, "nothing sent beyond the budget"
    assert current.wait_s() > 0

    lowered = await controller.async_apply_regulated_current(6, must_lower=True)

    assert lowered.outcome == REGULATED_WROTE
    assert charge_point.configured[-1] == "1.6"
    assert turn_off == []
    await controller.async_shutdown()


async def test_a_session_start_is_not_held_by_the_regulators_budget(hass: HomeAssistant) -> None:
    controller, charge_point, _turn_off = await _charging_at_16(hass)
    current = controller.adapter.current
    while current.wait_s() == 0:
        await current.async_set(10 if charge_point.value == "1.16" else 16, reason=WRITE_REGULATOR)

    assert await controller._async_assign_current_outcome(12) == ASSIGN_ASSIGNED  # noqa: SLF001
    assert charge_point.value == "1.12"
    await controller.async_shutdown()


async def test_the_restore_reads_back_within_the_same_bound(hass: HomeAssistant, short_verify: None) -> None:
    controller, charge_point, _turn_off = await _charging_at_16(hass)
    charge_point.accepts = False

    outcome = await controller.adapter.current.async_set(10, reason="restore", verify=True)

    assert outcome == ASSIGN_UNCONFIRMED
    await controller.async_shutdown()
