"""Direct unit tests for ChargingController storage isolation, and for
requested/setpoint current tracking -- distinct from each other and from
any notion of a measured charging current (see site/site_capacity.py)."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    DOMAIN,
)
from custom_components.spotnav.execution.controller import (
    ChargingController,
    _validate_current_limit_state,
    _validate_stored_amps,
    rewrite_assigned_current,
)
from tests.helpers import install_schedule
from custom_components.spotnav.vehicles.ocpp_identity import (
    OcppConnectorTarget,
    target_from_entity_id,
)


async def test_each_controller_uses_a_distinct_store_key(hass: HomeAssistant) -> None:
    """Two controllers for different entries must never share a storage file."""
    controller_a = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    controller_b = ChargingController(hass, "entry_b", {CONF_CHARGE_CONTROL: "switch.b"})

    assert controller_a._store.key == f"{DOMAIN}.entry_a"
    assert controller_b._store.key == f"{DOMAIN}.entry_b"
    assert controller_a._store.key != controller_b._store.key


async def test_saving_one_controllers_plan_does_not_touch_the_others_storage(
    hass: HomeAssistant,
) -> None:
    """Persisting charger A's schedule must not write anything under charger B's key."""
    hass.states.async_set("switch.a", "off")
    hass.states.async_set("switch.b", "off")
    controller_a = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    controller_b = ChargingController(hass, "entry_b", {CONF_CHARGE_CONTROL: "switch.b"})
    await controller_a.async_initialize()
    await controller_b.async_initialize()

    start = dt_util.utcnow() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    await install_schedule(
        controller_a,
        {"start": start.isoformat(), "end": end.isoformat(), "amps": 10}
    )

    assert controller_a.plan is not None
    assert controller_b.plan is None

    saved_a = await controller_a._store.async_load()
    saved_b = await controller_b._store.async_load()
    assert saved_a is not None and saved_a["plan"] is not None
    assert saved_b is None

    # Cancel the pending start/end timers so pytest-homeassistant-custom-component
    # doesn't flag them as lingering after this test.
    await controller_a.async_shutdown()
    await controller_b.async_shutdown()


async def test_a_malformed_stored_target_stop_record_is_discarded_even_alongside_other_records(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Storage is untrusted input: a record of another shape is dropped, not used.

    Both keys are present on purpose. The warning is the only sign that something
    wrote a shape this integration does not write, and it is easy to break by
    inserting a new branch in the middle of the `if`/`elif` that produces it --
    the added branch then owns the `elif`, and the warning silently stops firing
    whenever the new record happens to be present.
    """
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller._store.async_save(
        {
            "target_stop": "not a record",
            "pilot_floor_probe": {
                "started": True,
                "completed": False,
                "devid": "charger",
                "last_written": "1.1,2.10",
                "remembered": "1.16,2.10",
            },
        }
    )

    with caplog.at_level(logging.WARNING):
        await controller.async_initialize()

    assert "Discarding invalid saved SpotNav target stop record" in caplog.text
    assert controller._target_stop is None

    await controller.async_shutdown()


# --- requested_current_a / setpoint_current_a --------------------------


async def test_start_now_with_amps_and_no_schedule_sets_requested_current(
    hass: HomeAssistant,
) -> None:
    """A manual "start now" with explicit amps and no schedule is reflected in requested_current_a."""
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()

    await controller.async_start(amps=20)

    assert controller.requested_current_a == 20
    assert controller.plan is None

    await controller.async_shutdown()


async def test_start_with_amps_never_writes_the_current_limit(
    hass: HomeAssistant,
) -> None:
    """A start with an explicit current never touches the charger's limit entity.

    The request is recorded -- that is what the status response reports and what
    the load balancer computes against -- and the entity stays exactly as
    it was. Writing it would ask the charger's own integration to send the
    charger a persistent charging profile, which is a change to the charger
    rather than a command to it, and this controller does not make those. The
    switch is still turned on: that part of a start is unchanged.
    """
    hass.states.async_set("switch.a", "off")
    hass.states.async_set("number.a_limit", "6", {"unit_of_measurement": "A"})
    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    number_calls = async_mock_service(hass, "number", "set_value")
    controller = ChargingController(
        hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a", CONF_CURRENT_LIMIT: "number.a_limit"}
    )
    await controller.async_initialize()

    await controller.async_start(amps=16)

    assert number_calls == []
    assert controller.requested_current_a == 16
    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].data["entity_id"] == "switch.a"

    await controller.async_shutdown()


async def test_scheduled_active_charging_sets_requested_current_from_the_plan(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    start = dt_util.utcnow() - timedelta(minutes=1)
    end = start + timedelta(hours=1)

    await install_schedule(
        controller,
        {"start": start.isoformat(), "end": end.isoformat(), "amps": 12}
    )

    assert controller.requested_current_a == 12

    await controller.async_shutdown()


async def test_schedule_outside_its_active_period_does_not_set_a_requested_current(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    start = dt_util.utcnow() + timedelta(hours=1)
    end = start + timedelta(hours=1)

    await install_schedule(
        controller,
        {"start": start.isoformat(), "end": end.isoformat(), "amps": 12}
    )

    assert controller.charging is False
    assert controller.requested_current_a is None

    await controller.async_shutdown()


async def test_charger_on_without_any_spotnav_request_leaves_requested_current_unknown(
    hass: HomeAssistant,
) -> None:
    """The switch reporting "on" (e.g. turned on manually in Home Assistant,
    outside SpotNav entirely) must never be read as evidence of any specific
    requested current -- unknown stays unknown, never defaulted to 0.
    """
    hass.states.async_set("switch.a", "on")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()

    assert controller.charging is True
    assert controller.requested_current_a is None

    await controller.async_shutdown()


async def test_requested_current_survives_a_stop_and_is_reused_on_the_next_start(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()

    await controller.async_start(amps=16)
    assert controller.requested_current_a == 16
    hass.states.async_set("switch.a", "on")  # simulate the real switch responding

    await controller.async_stop()
    assert controller.requested_current_a == 16  # stop does not forget the setpoint
    hass.states.async_set("switch.a", "off")

    await controller.async_start()  # no amps given this time -- reuses the memory
    assert controller.requested_current_a == 16

    await controller.async_shutdown()


async def test_requested_current_is_restored_after_a_restart(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    await controller.async_start(amps=10)
    await controller.async_shutdown()

    restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await restarted.async_initialize()

    assert restarted.requested_current_a == 10

    await restarted.async_shutdown()


async def test_setpoint_current_reads_the_live_current_limit_entity(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    hass.states.async_set("number.a_limit", "16", {"unit_of_measurement": "A"})
    controller = ChargingController(
        hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a", CONF_CURRENT_LIMIT: "number.a_limit"}
    )
    await controller.async_initialize()

    assert controller.setpoint_current_a == 16

    hass.states.async_set("number.a_limit", "unavailable")
    assert controller.setpoint_current_a is None

    await controller.async_shutdown()


async def test_start_with_no_amps_and_no_memory_does_not_use_the_setpoint_as_a_request(
    hass: HomeAssistant,
) -> None:
    """A start-now with nothing explicit and no prior memory must leave
    requested_current_a genuinely unknown -- the current-limit entity's
    live setpoint is a real, useful value (see `resolve_current`), but it
    is not something SpotNav asked for, and must never be backfilled into
    requested_current_a as if it were.
    """
    hass.states.async_set("switch.a", "off")
    hass.states.async_set("number.a_limit", "10", {"unit_of_measurement": "A"})
    async_mock_service(hass, "switch", "turn_on")
    number_calls = async_mock_service(hass, "number", "set_value")
    controller = ChargingController(
        hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a", CONF_CURRENT_LIMIT: "number.a_limit"}
    )
    await controller.async_initialize()

    await controller.async_start()  # no amps, no plan, no prior memory

    assert controller.requested_current_a is None
    assert number_calls == []  # nothing was ever written

    resolved = controller.resolve_current()
    assert resolved.amps == 10
    assert resolved.source == "setpoint"

    await controller.async_shutdown()


async def test_requested_and_setpoint_current_are_independent_values(
    hass: HomeAssistant,
) -> None:
    """Neither value is ever derived from the other, and neither is a
    measured charging current."""
    hass.states.async_set("switch.a", "off")
    hass.states.async_set("number.a_limit", "6", {"unit_of_measurement": "A"})
    async_mock_service(hass, "switch", "turn_on")
    number_calls = async_mock_service(hass, "number", "set_value")
    controller = ChargingController(
        hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a", CONF_CURRENT_LIMIT: "number.a_limit"}
    )
    await controller.async_initialize()

    await controller.async_start(amps=20)

    assert controller.requested_current_a == 20
    # Nothing was written: a start records the request and never applies it.
    assert number_calls == []
    # The live entity in this test harness is never actually changed by the
    # mocked service call -- confirming setpoint_current_a is read fresh
    # from the entity itself, not cached from what was just requested.
    assert controller.setpoint_current_a == 6
    resolved = controller.resolve_current()
    assert resolved.amps == 20
    assert resolved.source == "requested"

    await controller.async_shutdown()


# --- _validate_current_limit_state: pure, no hass needed -------------------


def _numeric_state(value: str, unit: str | None = "A", **extra_attrs) -> State:
    attributes = {} if unit is None else {"unit_of_measurement": unit}
    attributes.update(extra_attrs)
    return State("number.x", value, attributes)


def test_validate_current_limit_state_accepts_a_clean_whole_amp_value() -> None:
    assert _validate_current_limit_state(_numeric_state("16")) == 16


def test_validate_current_limit_state_rejects_missing_entity() -> None:
    assert _validate_current_limit_state(None) is None


def test_validate_current_limit_state_rejects_unknown_unavailable_and_empty() -> None:
    assert _validate_current_limit_state(_numeric_state("unknown")) is None
    assert _validate_current_limit_state(_numeric_state("unavailable")) is None
    assert _validate_current_limit_state(_numeric_state("")) is None


def test_validate_current_limit_state_rejects_nan_and_infinite() -> None:
    assert _validate_current_limit_state(_numeric_state("nan")) is None
    assert _validate_current_limit_state(_numeric_state("inf")) is None
    assert _validate_current_limit_state(_numeric_state("-inf")) is None


def test_validate_current_limit_state_rejects_overflow_strings() -> None:
    # A string float() can parse but that overflows to +/-inf internally on
    # some platforms, or otherwise raises OverflowError, must not crash.
    assert _validate_current_limit_state(_numeric_state("1" + "0" * 400)) is None


def test_validate_current_limit_state_rejects_a_missing_or_wrong_unit() -> None:
    assert _validate_current_limit_state(_numeric_state("16", unit=None)) is None
    assert _validate_current_limit_state(_numeric_state("16", unit="W")) is None
    assert _validate_current_limit_state(_numeric_state("16", unit="")) is None


def test_validate_current_limit_state_rejects_a_fractional_value_never_rounds() -> None:
    assert _validate_current_limit_state(_numeric_state("15.7")) is None
    # A value that is a whole number even though written with a decimal
    # point is fine -- it is not being "rounded", just parsed.
    assert _validate_current_limit_state(_numeric_state("16.0")) == 16


def test_validate_current_limit_state_respects_entity_min_and_max() -> None:
    state = _numeric_state("4", min=6, max=32)
    assert _validate_current_limit_state(state) is None  # below entity min
    state = _numeric_state("40", min=6, max=32)
    assert _validate_current_limit_state(state) is None  # above entity max
    state = _numeric_state("16", min=6, max=32)
    assert _validate_current_limit_state(state) == 16


def test_validate_current_limit_state_ignores_broken_min_max_attributes() -> None:
    state = _numeric_state("16", min="not-a-number", max=None)
    assert _validate_current_limit_state(state) == 16


def test_validate_current_limit_state_enforces_absolute_bounds_regardless_of_entity_attrs() -> None:
    # Entity claims a huge max, but this integration's own absolute ceiling
    # (80A) still applies.
    state = _numeric_state("100", min=0, max=200)
    assert _validate_current_limit_state(state) is None


def test_validate_stored_amps_accepts_a_clean_integer() -> None:
    assert _validate_stored_amps(16) == 16


def test_validate_stored_amps_rejects_none_bool_and_fractional() -> None:
    assert _validate_stored_amps(None) is None
    assert _validate_stored_amps(True) is None
    assert _validate_stored_amps(15.7) is None


def test_validate_stored_amps_rejects_nan_inf_and_out_of_range() -> None:
    assert _validate_stored_amps(float("nan")) is None
    assert _validate_stored_amps(float("inf")) is None
    assert _validate_stored_amps(0) is None
    assert _validate_stored_amps(9999) is None


def test_validate_stored_amps_rejects_a_non_numeric_legacy_value() -> None:
    assert _validate_stored_amps("not-a-number") is None
    assert _validate_stored_amps({"unexpected": "shape"}) is None


async def test_restart_with_a_corrupted_requested_current_discards_it_without_a_traceback(
    hass: HomeAssistant, caplog
) -> None:
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    await controller._store.async_save({"plan": None, "requested_current_a": "corrupt"})

    restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await restarted.async_initialize()

    assert restarted.requested_current_a is None
    assert "Discarding invalid saved SpotNav requested current" in caplog.text
    assert "Traceback" not in caplog.text

    await controller.async_shutdown()
    await restarted.async_shutdown()


# --- the OCPP ChangeConfiguration helpers: pure -----------------------------


def test_rewrite_assigned_current_replaces_one_connector_and_keeps_the_rest() -> None:
    """The connector named is rewritten; every other entry is preserved.

    The list comes back in ascending connector order, which is the order the
    charger's own integration writes, so what is written is comparable with what
    was read. A connector that is not in the list is *added* rather than left
    out: an unlisted connector has no entry at all, and omitting it would ask
    for a change that does not include the connector this call is about.
    """
    assert rewrite_assigned_current("1.16,2.10", 1, 10) == "1.10,2.10"
    assert rewrite_assigned_current("1.16,2.10", 2, 6) == "1.16,2.6"
    assert rewrite_assigned_current("1.16", 2, 10) == "1.16,2.10"
    # A value that is already right is written back as it was, not dropped.
    assert rewrite_assigned_current("1.16,2.10", 2, 10) == "1.16,2.10"
    # An out-of-order reading is normalised, so the result has one shape.
    assert rewrite_assigned_current("2.10,1.16", 1, 10) == "1.10,2.10"


def test_rewrite_assigned_current_refuses_anything_it_cannot_read() -> None:
    """A list this integration cannot read is refused, never patched up.

    Refusing is the safe answer: the caller then leaves the charger on its
    previous assignment rather than writing a value nobody can read back.
    """
    for unreadable in ("", "1.x", "x.10", "1.16,", "1", "1.16.2", "0.16", "1.-16", "1. 16"):
        with pytest.raises(ValueError):
            rewrite_assigned_current(unreadable, 1, 10)
    with pytest.raises(ValueError):
        rewrite_assigned_current("1.16", 0, 10)
    with pytest.raises(ValueError):
        rewrite_assigned_current("1.16", 1, 0)


def test_the_012_roles_name_their_connector_and_a_single_connector_is_flat() -> None:
    """0.12's per-connector roles, and the flat form the integration itself uses for one connector.

    The flat form is the integration's own statement that the charge point has one connector (`number.py`),
    which is why it answers connector 1 -- while the equally flat *station* ceiling answers nothing at all.
    """
    assert target_from_entity_id(
        "switch.charger_connector_2_charge_control"
    ) == OcppConnectorTarget("charger", 2)
    assert target_from_entity_id(
        "number.charger_connector_1_session_current_limit"
    ) == OcppConnectorTarget("charger", 1)
    assert target_from_entity_id("switch.charger_charge_control") == OcppConnectorTarget(
        "charger", 1
    )
    assert target_from_entity_id(
        "number.charger_session_current_limit"
    ) == OcppConnectorTarget("charger", 1)
    assert target_from_entity_id("number.charger_maximum_current") is None


def test_no_shape_that_names_no_connector_ever_answers() -> None:
    """A station-wide entity names no connector, so it answers `None`.

    `AssignedCurrent` is a per-connector list, so guessing which connector a
    station-wide entity meant is exactly the kind of guess this integration does
    not make: the caller records the request and writes nothing.
    """
    for unmappable in (
        "number.charger_maximum_current",
        "number.charger_connector_0_maximum_current",
        "number.charger_connector_x_maximum_current",
        "number.charger_connector_1_maximum_current_extra",
        "number.charger_connector_1_minimum_current",
        "number.connector_1_maximum_current",
        "switch.charger_connector_1_maximum_current",
        "sensor.charger_connector_1_maximum_current",
        "number.charger_connector_1_session_current",
        "switch.charger_connector_1_availability",
    ):
        assert target_from_entity_id(unmappable) is None, unmappable


# --- the controller's ChangeConfiguration mode ------------------------------


def _change_configuration_controller(hass: HomeAssistant) -> ChargingController:
    """A controller opted in to ChangeConfiguration, with a per-connector limit."""
    return ChargingController(
        hass,
        "entry_a",
        {
            CONF_CHARGE_CONTROL: "switch.a",
            CONF_CURRENT_LIMIT: "number.charger_connector_1_session_current_limit",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: "charger",
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )


async def test_change_configuration_start_reads_and_rewrites_the_assigned_current(
    hass: HomeAssistant,
) -> None:
    """A start in this mode assigns the current through the charger's own service.

    The list is *read first*, because `AssignedCurrent` holds every connector at
    once: writing it means writing all of them, and the connectors this start is
    not about keep the values they had. The read is mocked with the response the
    real service returns, which is what makes `return_response=True` work.
    """
    hass.states.async_set("switch.a", "off")
    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    controller = _change_configuration_controller(hass)
    await controller.async_initialize()

    await controller.async_start(amps=10)

    assert len(configure_calls) == 1
    assert configure_calls[0].data == {
        "devid": "charger",
        "ocpp_key": "AssignedCurrent",
        "value": "1.10,2.10",
    }
    assert controller.requested_current_a == 10
    assert len(turn_on_calls) == 1

    await controller.async_shutdown()


async def test_change_configuration_never_writes_when_the_string_cannot_be_read(
    hass: HomeAssistant,
) -> None:
    """An unreadable list means no write at all -- and no error either.

    A charger left on its previous assignment is strictly better than one written
    to blindly, so every unreadable answer is one outcome: nothing written. The
    request is still recorded, which is the same contract the default mode has.
    """
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    for unreadable_response in ({"value": "not a list"}, {"value": ""}, {}, None):
        async_mock_service(hass, "ocpp", "get_configuration", response=unreadable_response)
        configure_calls = async_mock_service(hass, "ocpp", "configure")
        controller = _change_configuration_controller(hass)
        await controller.async_initialize()

        await controller.async_start(amps=10)

        assert configure_calls == [], unreadable_response
        assert controller.requested_current_a == 10
        await controller.async_shutdown()


async def test_a_sub_floor_current_sends_nothing_and_is_not_called_a_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The regulator's own 0 A decisions are not logged as a failure.

    There is no valid pilot current below 6 A, so `_async_assign_current(0)`
    cannot be expressed and nothing is sent -- which is what the charger already
    did with those decisions. The old line for this case said "Could not set the
    charger's current through ChangeConfiguration", describing a service call
    that failed; none was ever attempted. What a charger *does* with a
    sub-floor value is the subject of the separate probe and is deliberately not
    guessed at here.
    """
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    controller = _change_configuration_controller(hass)
    await controller.async_initialize()

    with caplog.at_level(logging.DEBUG):
        await controller._async_assign_current(0)

    assert configure_calls == []
    assert "Could not set the charger's current" not in caplog.text
    assert "below the 6.0A floor" in caplog.text

    await controller.async_shutdown()


async def test_no_current_control_mode_writes_nothing(hass: HomeAssistant) -> None:
    """Without the opt-in neither mechanism is used: the default is unchanged.

    A charger that did not opt in gets neither `number.set_value` (a persistent profile) nor
    `ChangeConfiguration`.
    """
    hass.states.async_set("switch.a", "off")
    async_mock_service(hass, "switch", "turn_on")
    number_calls = async_mock_service(hass, "number", "set_value")
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    controller = ChargingController(
        hass,
        "entry_a",
        {
            CONF_CHARGE_CONTROL: "switch.a",
            CONF_CURRENT_LIMIT: "number.charger_connector_1_maximum_current",
        },
    )
    await controller.async_initialize()

    await controller.async_start(amps=10)

    assert number_calls == []
    assert configure_calls == []
    assert controller.requested_current_a == 10

    await controller.async_shutdown()
