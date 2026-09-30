"""Turning a site's active load balancing on and off: `spotnav/update_site_settings`
and the off/on transitions of `SiteCapacityController` it drives.

WHAT THE OPTIONS-FLOW TOGGLE DID BEFORE THIS ROUND (read from the code; pinned by
`test_the_options_flow_toggle_reloads_the_site_and_restores_nothing` below)
------------------------------------------------------------------------------------------
`SiteCapacityOptionsFlow._save_site_details` writes `CONF_ACTIVE_CONTROL_ENABLED` into the site
entry's `data` and then awaits `hass.config_entries.async_reload(site_entry)`; this integration
registers no update listener, so *only* that explicit reload ever applies the change. A reload is
unload + setup of the whole site entry:

* every `SiteCapacityController` object is discarded (`_async_unload_site_entry` pops it and calls
  `async_shutdown`) and a **new** one is built from `dict(entry.data)`; the charger controllers,
  the solar/hybrid coordinators (re-bound by `async_rebind_solar_execution`) and the site's
  platform entities are torn down and rebuilt around it;
* `async_shutdown` cancels the timer, the state and charger listeners and cancels an in-flight apply
  pass **mid-write** -- whether the `AssignedCurrent` write of that pass reached the charger is
  simply unknown afterwards;
* the new controller has empty `RegulatorDamper`s, empty `YieldStepper`s and yield diagnostics,
  empty direction history and empty "logged once" memory. Nothing remembers what was written;
* **disabling** therefore restores *nothing*: a current the regulator had lowered stays lowered on
  the charger (the OCPP `AssignedCurrent` slot), while SpotNav's own `requested_current_a` still
  says the higher figure -- a silently throttled charger with balancing "off";
* **enabling** builds the controller and `async_initialize` runs `_recompute()`, which schedules an
  apply pass at once; the empty damper's first proposal is a `first_write`, written immediately,
  with no fresh-snapshot step of its own and no chance for the person to see it coming;
* observation (regulator decision, capacity sensor, membership conflicts) restarts from scratch too.

WHAT THE SWITCH DOES NOW (see `SiteCapacityController.async_enable/disable_active_control`)
------------------------------------------------------------------------------------------
In place, on the running controller, no reload: off cancels modulation and any admitted-but-not-
written decision, keeps observation running, and restores every lowered current through the one
serialized current-control boundary (`ChargingController.async_restore_current`), reading the charger
back before it claims `restored`. On starts from a clean slate, refuses an unavailable site, and
never writes inside the call.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import site_settings as site_settings_module
from custom_components.spotnav.const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_MEASURED_CURRENT_SOURCE,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
    SOLAR_PRIORITY_BATTERY_FIRST,
    SOLAR_PRIORITY_CAR_FIRST,
)
from custom_components.spotnav.execution.controller import (
    ASSIGN_UNCONFIRMED,
    ChargingController,
)
from custom_components.spotnav.api.dashboard import capture_site, serialize_site
from custom_components.spotnav.site.measurement_source import (
    PhaseMeasurementSource,
    source_to_dict,
)
from custom_components.spotnav.site.regulator import (
    PhaseDecisionBasis,
    RegulatorDecision,
)
from custom_components.spotnav.site.site_capacity import PHASES
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController
from custom_components.spotnav.api.site_settings import (
    ERROR_ACTIVE_CONTROL_UNAVAILABLE,
    ERROR_CONFIRMATION_FAILED,
    ERROR_CONFLICT,
    ERROR_INVALID_VALUE,
    ERROR_NO_SITE,
    ERROR_UNAVAILABLE,
    SITE_SETTINGS_API_VERSION,
    SiteSettingsRefusal,
    async_update_site_settings,
)
from custom_components.spotnav.api.common import ERROR_NOT_ADMIN

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import forecast_entry, setup_site_with_charger
from .messages import update_site_settings_message
from .world import admin, non_admin, ws_call
from .world import controller_of

_REQUEST_IDS = itertools.count(1000)
_REQUESTED_A: Final = 16
_THROTTLED_A: Final = 10
SITE_SETTINGS_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "site_settings" / "v1"


@pytest.fixture(autouse=True)
def _forecast_capable_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_hass: HomeAssistant) -> frozenset[str]:
        return frozenset({"roof"})

    monkeypatch.setattr(site_settings_module, "async_forecast_capable_domains", _fake)


class FakeOcpp:
    """A stateful stand-in for the OCPP integration's two services: `AssignedCurrent` per charge
    point, every `configure` recorded, and switches to make the charger refuse, ignore (accept but
    not apply -- read-back then disagrees), fail its reads, or hold a write in flight."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.assigned: dict[str, str] = {}
        self.configure_calls: list[tuple[str, str]] = []
        self.fail_configure = False
        self.ignore_configure = False
        self.fail_get = False
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.on_configure: Callable[[], Awaitable[None]] | None = None
        self.lock_probe: Callable[[str], None] | None = None
        hass.services.async_register(
            "ocpp",
            "get_configuration",
            self._get,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register("ocpp", "configure", self._configure)

    async def _get(self, call: ServiceCall) -> dict[str, Any]:
        if self.fail_get:
            raise HomeAssistantError("charger unreachable")
        return {"value": self.assigned.setdefault(call.data["devid"], f"1.{_REQUESTED_A},2.10")}

    async def _configure(self, call: ServiceCall) -> None:
        devid, value = call.data["devid"], call.data["value"]
        self.configure_calls.append((devid, value))
        if self.lock_probe is not None:
            self.lock_probe(devid)
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        if self.on_configure is not None:
            await self.on_configure()
        if self.fail_configure:
            raise HomeAssistantError("charger refused")
        if not self.ignore_configure:
            self.assigned[devid] = value

    def amps(self, devid: str) -> int:
        return int(self.assigned[devid].split(",")[0].split(".")[1])


class World:
    def __init__(
        self,
        hass: HomeAssistant,
        ocpp: FakeOcpp,
        site_entry: Any,
        charger_ids: list[str],
        prefix: str,
    ) -> None:
        self.hass, self.ocpp, self.site_entry = hass, ocpp, site_entry
        self.charger_ids, self.prefix = charger_ids, prefix

    @property
    def controller(self) -> SiteCapacityController:
        return controller_of(self.hass, self.site_entry.entry_id)

    def charger(self, charger_id: str) -> ChargingController:
        return controller_of(self.hass, charger_id)

    def devid(self, charger_id: str) -> str:
        return self.charger(charger_id).ocpp_target.devid

    def set_site_power(self, watts: float | str) -> None:
        for phase in PHASES:
            self.hass.states.async_set(
                f"sensor.{self.prefix}_power_{phase.lower()}",
                str(watts),
                {"unit_of_measurement": "W"},
            )

    def make_measurement_unavailable(self) -> None:
        for phase in PHASES:
            self.hass.states.async_set(f"sensor.{self.prefix}_power_{phase.lower()}", "unavailable")

    def restore_measurement(self) -> None:
        self.set_site_power(2000.0 * len(self.charger_ids))

    def block(self) -> list[dict[str, Any]]:
        return [self.card_site(charger_id) for charger_id in self.charger_ids]

    def card_site(self, charger_id: str) -> dict[str, Any] | None:
        return serialize_site(capture_site(self.hass, charger_id), can_act=True)

    def active(self, charger_id: str | None = None) -> dict[str, Any]:
        return self.card_site(charger_id or self.charger_ids[0])["active_control"]  # type: ignore[index]

    def stored(self) -> bool:
        return bool(
            self.hass.config_entries.async_get_entry(self.site_entry.entry_id).data.get(
                CONF_ACTIVE_CONTROL_ENABLED, False
            )
        )


def _handmade_decision(amps: float) -> RegulatorDecision:
    return RegulatorDecision(
        proposed_current_a=amps,
        reason="increase_within_safe_uncredited_margin",
        limiting_phase="L1",
        basis={
            "L1": PhaseDecisionBasis(
                signed_active_power_w=2000.0,
                signed_active_power_age_s=5.0,
                measured_current_a=6.0,
                measured_current_age_s=5.0,
                requested_current_a=amps,
                direction="net_import",
                confirmed_direction="net_import",
            )
        },
    )


async def make_world(
    hass: HomeAssistant,
    *,
    prefix: str = "w",
    chargers: int = 1,
    active: bool = True,
    healthy: bool = True,
    ocpp: FakeOcpp | None = None,
) -> World:
    """A derived-mode site with `chargers` ChangeConfiguration-commandable chargers, each started at
    16 A through the real path, on a stateful fake OCPP; healthy measurement unless told otherwise."""
    ocpp = ocpp or FakeOcpp(hass)
    async_mock_service(hass, "switch", "turn_on")
    charger_ids: list[str] = []
    for index in range(chargers):
        charger_id = f"{prefix}_c{index + 1}"
        hass.states.async_set(f"switch.{charger_id}", "off")
        entry = make_entry(
            hass,
            entry_id=charger_id,
            charge_control=f"switch.{charger_id}",
            current_limit=f"number.{charger_id}_connector_1_session_current_limit",
            webhook_id=f"webhook-{charger_id}",
            title=f"Charger {index + 1}",
            current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
            ocpp_target=(charger_id, 1),
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await controller_of(hass, charger_id).async_start(amps=_REQUESTED_A)
        hass.states.async_set(f"switch.{charger_id}", "on")
        for phase in PHASES:
            set_current_sensor(hass, f"sensor.{charger_id}_{phase.lower()}", 6)
        charger_ids.append(charger_id)

    derived = {
        phase: {
            "power": f"sensor.{prefix}_power_{phase.lower()}",
            "reactive_power": f"sensor.{prefix}_reactive_{phase.lower()}",
            "voltage": f"sensor.{prefix}_voltage_{phase.lower()}",
        }
        for phase in PHASES
    }
    for phase in PHASES:
        hass.states.async_set(
            derived[phase]["power"], str(2000.0 * chargers), {"unit_of_measurement": "W"}
        )
        hass.states.async_set(derived[phase]["reactive_power"], "0", {"unit_of_measurement": "var"})
        hass.states.async_set(derived[phase]["voltage"], "230", {"unit_of_measurement": "V"})
    site = make_site_entry(
        hass,
        entry_id=f"{prefix}_site",
        main_fuse_a=20.0,
        safety_margin_a=1.0,
        charger_entry_ids=charger_ids,
        phase_wiring={
            charger_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="separate_entities",
                        entity_ids={p: f"sensor.{charger_id}_{p.lower()}" for p in PHASES},
                    )
                ),
            }
            for charger_id in charger_ids
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        active_control_enabled=active,
        title="Home",
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    world = World(hass, ocpp, site, charger_ids, prefix)
    if not healthy:
        world.make_measurement_unavailable()
        await hass.async_block_till_done()
    ocpp.configure_calls.clear()
    return world


async def throttle(world: World, amps: int = _THROTTLED_A) -> None:
    """Have the regulator lower every charger to `amps` through its real apply path (a fresh damper's
    first write), so the charger is genuinely throttled below its requested 16 A."""
    controller = world.controller
    controller._dampers.clear()
    controller.regulator_decisions = {
        charger_id: _handmade_decision(float(amps)) for charger_id in world.charger_ids
    }
    await controller._async_apply_active_control()
    for charger_id in world.charger_ids:
        assert world.ocpp.amps(world.devid(charger_id)) == amps
    world.ocpp.configure_calls.clear()


def v2(
    charger_id: str,
    *,
    changes: Any,
    expected: Any = None,
    api_version: Any = SITE_SETTINGS_API_VERSION,
) -> dict[str, Any]:
    return {
        "type": "spotnav/update_site_settings",
        "api_version": api_version,
        "charger_id": charger_id,
        "expected": {} if expected is None else expected,
        "changes": changes,
    }


async def call(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frame = await ws_call(client, payload)
    assert frame["success"] is True, frame
    return frame["result"]


# ------------------------------------------------------------- what today's toggle does


async def test_the_options_flow_toggle_reloads_the_site_and_restores_nothing(
    hass: HomeAssistant,
) -> None:
    """Item 1, pinned: the options flow's own mechanism (store the option, reload the site entry)
    replaces the controller object, forgets the damper, and leaves a throttled charger throttled."""
    world = await make_world(hass, prefix="legacy")
    await throttle(world)
    old_controller = world.controller

    hass.config_entries.async_update_entry(
        world.site_entry, data={**world.site_entry.data, CONF_ACTIVE_CONTROL_ENABLED: False}
    )
    await hass.config_entries.async_reload(world.site_entry.entry_id)
    await hass.async_block_till_done()

    assert world.controller is not old_controller
    assert world.controller._dampers == {}
    assert world.ocpp.configure_calls == []  # nothing restored
    assert world.ocpp.amps(world.devid(world.charger_ids[0])) == _THROTTLED_A
    assert world.charger(world.charger_ids[0]).requested_current_a == _REQUESTED_A


# --------------------------------------------- the options flow goes through the same transition


async def _save_through_options_flow(world: World, *, active: bool) -> Any:
    """Open the site's options flow, submit its first step with `active_control_enabled` set as
    given (everything else as stored) and accept every following step's defaults."""
    hass, data = world.hass, world.site_entry.data
    result = await hass.config_entries.options.async_init(world.site_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "main_fuse_a": data["main_fuse_a"],
            "safety_margin_a": data["safety_margin_a"],
            "measurement_mode": MEASUREMENT_MODE_DERIVED,
            "charger_entry_ids": list(world.charger_ids),
            "site_enabled": True,
            "active_control_enabled": active,
            "max_age_s": data["max_age_s"],
        },
    )
    for _ in range(6):
        if result["type"] == "create_entry":
            break
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    return result


async def test_options_flow_disable_while_throttled_restores_through_the_charger_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = await make_world(hass, prefix="flowoff")
    await throttle(world)
    charger_id = world.charger_ids[0]
    charger = world.charger(charger_id)
    old_controller = world.controller
    order: list[str] = []
    seen: list[tuple[int, bool]] = []
    original = ChargingController._async_assign_current_outcome

    async def spy(self, amps, *, verify=False):
        order.append(f"write:stored={world.stored()}")
        seen.append((amps, verify))
        return await original(self, amps, verify=verify)

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", spy)
    lock_states: list[bool] = []
    world.ocpp.lock_probe = lambda _devid: lock_states.append(charger._assign_lock.locked())
    lock_held: list[bool] = []
    world.ocpp.on_configure = lambda: _record(lock_held, old_controller.transition_lock.locked())

    await _save_through_options_flow(world, active=False)

    assert seen == [(_REQUESTED_A, True)]
    assert lock_states == [True] and lock_held == [True]
    # The restore was written while the stored option still said "on": restore first, persist after.
    assert order == ["write:stored=True"]
    assert world.ocpp.configure_calls == [(world.devid(charger_id), "1.16,2.10")]
    assert world.ocpp.amps(world.devid(charger_id)) == _REQUESTED_A
    assert world.stored() is False
    assert world.controller is not old_controller and world.controller.active_control_enabled is False
    assert "restore failed" not in caplog.text and not old_controller.transition_lock.locked()


async def _record(target: list[bool], value: bool) -> None:
    target.append(value)


async def test_options_flow_disable_with_a_failed_restore_is_logged_and_surfaced(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    world = await make_world(hass, prefix="flowfail")
    await throttle(world)
    charger_id = world.charger_ids[0]
    world.ocpp.fail_configure = True

    with caplog.at_level("WARNING"):
        await _save_through_options_flow(world, active=False)

    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "could not be restored" in r.getMessage()]
    assert len(warnings) == 1
    assert charger_id in warnings[0].getMessage() and "write_failed" in warnings[0].getMessage()
    assert world.ocpp.amps(world.devid(charger_id)) == _THROTTLED_A  # honestly still throttled
    assert world.stored() is False  # the opt-in is off regardless
    notifications = hass.data.get("persistent_notification", {})
    assert f"{DOMAIN}_restore_failed_{world.site_entry.entry_id}" in notifications


async def test_options_flow_disable_with_nothing_lowered_writes_nothing_and_stays_quiet(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    world = await make_world(hass, prefix="flowquiet")
    with caplog.at_level("WARNING"):
        await _save_through_options_flow(world, active=False)
    assert world.ocpp.configure_calls == []
    assert world.stored() is False
    assert [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and r.name.startswith(
            ("custom_components.spotnav.flows", "custom_components.spotnav.site.")
        )
    ] == []
    assert f"{DOMAIN}_restore_failed_{world.site_entry.entry_id}" not in hass.data.get(
        "persistent_notification", {}
    )


async def test_options_flow_enable_is_unchanged_reload_with_no_write(hass: HomeAssistant) -> None:
    world = await make_world(hass, prefix="flowon", active=False)
    old_controller = world.controller
    with pytest.MonkeyPatch.context() as mp:
        calls: list[str] = []
        original = SiteCapacityController.async_disable_active_control

        async def spy(self):
            calls.append("disable")
            return await original(self)

        mp.setattr(SiteCapacityController, "async_disable_active_control", spy)
        await _save_through_options_flow(world, active=True)

    assert calls == []  # no off transition on the way on
    assert world.stored() is True
    assert world.controller is not old_controller and world.controller.active_control_enabled is True
    # Unchanged from before this round: the rebuilt controller's ordinary apply pass (the empty
    # damper's first write, through every existing gate) is the only writer -- the flow adds none.
    assert world.ocpp.configure_calls == [(world.devid(world.charger_ids[0]), "1.16,2.10")]


# ------------------------------------------------------------------ resolution and authority


async def test_a_charger_without_a_site_is_refused_no_site(hass: HomeAssistant, hass_ws_client) -> None:
    hass.states.async_set("switch.lonely", "off")
    lonely = make_entry(
        hass,
        entry_id="lonely",
        charge_control="switch.lonely",
        current_limit=None,
        webhook_id="webhook-lonely",
        title="Lonely",
    )
    assert await hass.config_entries.async_setup(lonely.entry_id)
    await hass.async_block_till_done()
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2("lonely", changes={"active_control_enabled": True}))

    assert result == {
        "api_version": 1,
        "ok": False,
        "error": ERROR_NO_SITE,
        "site": None,
        "restore": None,
    }


async def test_a_read_only_caller_is_refused_and_nothing_moves(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    world = await make_world(hass, prefix="ro")
    await throttle(world)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    result = await call(
        client, v2(world.charger_ids[0], changes={"active_control_enabled": False})
    )

    assert result == {
        "api_version": 1,
        "ok": False,
        "error": ERROR_NOT_ADMIN,
        "site": None,
        "restore": None,
    }
    assert world.controller.active_control_enabled is True
    assert world.ocpp.configure_calls == []


# --------------------------------------------- all four available x enabled combinations


async def test_available_and_off_can_be_turned_on_and_the_card_sees_it_at_once(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="ao", active=False)
    assert world.active() == {"available": True, "enabled": False, "reason": None, "writable": True}
    client = await admin(hass, hass_ws_client)

    result = await call(
        client,
        v2(
            world.charger_ids[0],
            changes={"active_control_enabled": True},
            expected={"active_control_enabled": False},
        ),
    )

    assert result["ok"] is True and result["error"] is None and result["restore"] is None
    assert result["site"]["active_control"] == {"available": True, "enabled": True, "reason": None, "writable": True}
    assert world.active() == {"available": True, "enabled": True, "reason": None, "writable": True}
    assert world.stored() is True and world.controller.active_control_enabled is True


async def test_available_and_on_can_be_turned_off_with_nothing_to_restore(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="aa")
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": False}))

    assert result["ok"] is True
    assert result["site"]["active_control"]["enabled"] is False
    assert result["restore"]["outcome"] == "not_needed"
    assert [c["outcome"] for c in result["restore"]["chargers"]] == ["not_needed"]
    assert world.stored() is False and world.controller.active_control_enabled is False


async def test_unavailable_and_off_cannot_be_newly_turned_on(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="uo", active=False, healthy=False)
    assert world.active()["available"] is False and world.active()["enabled"] is False
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": True}))

    assert result["ok"] is False and result["error"] == ERROR_ACTIVE_CONTROL_UNAVAILABLE
    assert result["site"]["active_control"]["enabled"] is False
    assert result["site"]["active_control"]["available"] is False
    assert world.controller.active_control_enabled is False and world.stored() is False
    assert world.ocpp.configure_calls == []


async def test_unavailable_and_on_stays_on_and_can_be_turned_off(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """Capability lost while enabled: the switch stays on with the stable reason -- never silently
    turned off -- and disabling is still allowed, restoring through the charger boundary."""
    world = await make_world(hass, prefix="ua")
    await throttle(world)
    world.make_measurement_unavailable()
    await hass.async_block_till_done()
    active = world.active()
    assert active["enabled"] is True and active["available"] is False and active["reason"]
    assert world.ocpp.configure_calls == []  # the gates hold: nothing written while unhealthy
    client = await admin(hass, hass_ws_client)

    same = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": True}))
    assert same["ok"] is True and same["site"]["active_control"]["enabled"] is True

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": False}))

    assert result["ok"] is True and result["restore"]["outcome"] == "restored"
    assert result["site"]["active_control"]["enabled"] is False
    assert world.ocpp.amps(world.devid(world.charger_ids[0])) == _REQUESTED_A


# --------------------------------------------- two chargers on one site, concurrent cards


async def test_two_chargers_on_one_site_share_one_switch_and_both_are_restored(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="two", chargers=2)
    await throttle(world)
    client = await admin(hass, hass_ws_client)
    first, second = world.charger_ids

    result = await call(client, v2(first, changes={"active_control_enabled": False}))

    assert result["restore"]["outcome"] == "restored"
    assert {c["charger_id"]: (c["outcome"], c["from_a"], c["to_a"]) for c in result["restore"]["chargers"]} == {
        first: ("restored", _THROTTLED_A, _REQUESTED_A),
        second: ("restored", _THROTTLED_A, _REQUESTED_A),
    }
    assert world.active(first)["enabled"] is False and world.active(second)["enabled"] is False
    for charger_id in (first, second):
        assert world.ocpp.amps(world.devid(charger_id)) == _REQUESTED_A
    # The other card, asking with its own charger id, converges from the same accepted state.
    again = await call(client, v2(second, changes={"active_control_enabled": True}))
    assert again["ok"] is True
    assert world.active(first)["enabled"] is True and world.active(second)["enabled"] is True


async def test_concurrent_cards_are_serialized_one_disable_restores_the_other_is_a_no_op(
    hass: HomeAssistant,
) -> None:
    world = await make_world(hass, prefix="conc", chargers=2)
    await throttle(world)
    first, second = world.charger_ids

    results = await asyncio.gather(
        async_update_site_settings(
            hass, first, expected={}, changes={"active_control_enabled": False}
        ),
        async_update_site_settings(
            hass, second, expected={}, changes={"active_control_enabled": False}
        ),
    )

    outcomes = sorted(restore["outcome"] for _site, restore in results)
    assert outcomes == ["not_needed", "restored"]
    # Exactly one write per charger: the second call found the site already off.
    assert sorted(devid for devid, _ in world.ocpp.configure_calls) == sorted(
        world.devid(charger_id) for charger_id in world.charger_ids
    )
    assert world.stored() is False


async def test_a_stale_expected_is_a_conflict_carrying_the_current_state(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """A card that last saw `enabled: false` while another card already turned it on."""
    world = await make_world(hass, prefix="stale", active=False)
    client = await admin(hass, hass_ws_client)
    first = world.charger_ids[0]
    assert (await call(client, v2(first, changes={"active_control_enabled": True})))["ok"] is True

    late = await call(
        client,
        v2(
            first,
            changes={"active_control_enabled": True},
            expected={"active_control_enabled": False},
        ),
    )
    late_disable = await call(
        client,
        v2(
            first,
            changes={"active_control_enabled": False},
            expected={"active_control_enabled": False},
        ),
    )

    for answer in (late, late_disable):
        assert answer["ok"] is False and answer["error"] == ERROR_CONFLICT
        assert answer["site"]["active_control"]["enabled"] is True  # the current truth, not the request
        assert answer["restore"] is None
    assert world.controller.active_control_enabled is True and world.ocpp.configure_calls == []


# ------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "changes",
    [
        {"active_control_enabled": "yes"},
        {"active_control_enabled": 1},
        {"active_control_enabled": None},
        {"active_control_enabled": True, "solar_priority": SOLAR_PRIORITY_BATTERY_FIRST},
        {"active_control_enabled": True, "something_else": 1},
    ],
)
async def test_invalid_active_control_changes_are_refused_and_change_nothing(
    hass: HomeAssistant, hass_ws_client, changes: dict[str, Any]
) -> None:
    world = await make_world(hass, prefix="inv", active=False)
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(world.charger_ids[0], changes=changes))

    assert result["ok"] is False and result["error"] == ERROR_INVALID_VALUE
    assert result["site"]["active_control"]["enabled"] is False
    assert world.controller.active_control_enabled is False and world.stored() is False


async def test_a_non_boolean_expected_is_invalid_not_a_conflict(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="expinv", active=False)
    client = await admin(hass, hass_ws_client)

    result = await call(
        client,
        v2(
            world.charger_ids[0],
            changes={"active_control_enabled": True},
            expected={"active_control_enabled": "no"},
        ),
    )

    assert result["error"] == ERROR_INVALID_VALUE and world.controller.active_control_enabled is False


async def test_confirmation_failure_keeps_the_previous_value_everywhere(
    hass: HomeAssistant, hass_ws_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reading the accepted state back does not show what was asked (the store did not take
    it), the answer is `confirmation_failed`, the stored value is unchanged and the running
    controller is put back to its previous state."""
    world = await make_world(hass, prefix="conf", active=False)
    client = await admin(hass, hass_ws_client)
    monkeypatch.setattr(hass.config_entries, "async_update_entry", lambda *a, **k: False)

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": True}))

    assert result["ok"] is False and result["error"] == ERROR_CONFIRMATION_FAILED
    assert result["site"]["active_control"]["enabled"] is False
    assert world.controller.active_control_enabled is False
    assert world.stored() is False


# ------------------------------------------------------------ the on transition


async def test_enable_never_writes_inside_the_call_and_starts_from_a_clean_slate(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="on", active=False)
    controller = world.controller
    # Stale learned state that an enable must not inherit.
    controller._damper_for(world.charger_ids[0])._write(7.0)
    controller._direction_history["L1"].extend(["net_import"] * 5)
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": True}))
    await hass.async_block_till_done()

    assert result["ok"] is True
    assert world.ocpp.configure_calls == []  # no write, not even a scheduled one, inside the call
    assert controller._apply_task is None
    assert controller._dampers == {}
    assert all(len(history) == 0 for history in controller._direction_history.values())
    assert world.ocpp.amps(world.devid(world.charger_ids[0])) == _REQUESTED_A


async def test_after_enable_the_first_write_goes_through_the_normal_gates_and_damper(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="gate", active=False)
    client = await admin(hass, hass_ws_client)
    await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": True}))
    controller = world.controller

    # Unhealthy measurement: the next trigger schedules a pass that every gate refuses.
    world.make_measurement_unavailable()
    await hass.async_block_till_done()
    controller._recompute()
    await hass.async_block_till_done()
    assert world.ocpp.configure_calls == []

    # Healthy again: the next trigger's pass reaches the damper, whose first write is the
    # decision's own proposal, written through the charger's one write path.
    world.restore_measurement()
    await hass.async_block_till_done()
    controller._recompute()
    await hass.async_block_till_done()
    damper = controller._dampers.get(world.charger_ids[0])
    assert damper is None or damper.last_written_a is not None
    for devid, value in world.ocpp.configure_calls:
        assert devid == world.devid(world.charger_ids[0])
        assert value.startswith("1.")


async def test_enable_is_refused_while_a_membership_conflict_blocks_the_site(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="mem", active=False)
    make_site_entry(
        hass,
        entry_id="mem_rival",
        charger_entry_ids=list(world.charger_ids),
        active_control_enabled=False,
        title="Rival",
    )
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": True}))

    assert result["ok"] is False and result["error"] == ERROR_ACTIVE_CONTROL_UNAVAILABLE
    assert world.controller.active_control_enabled is False


# ------------------------------------------------------------ the off transition


async def test_disable_while_throttled_restores_through_the_one_charger_boundary(
    hass: HomeAssistant, hass_ws_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(hass, prefix="off")
    await throttle(world)
    charger_id = world.charger_ids[0]
    charger = world.charger(charger_id)

    # The restore's one write is `ChargingController._async_assign_current_outcome`, holding the
    # charger's own `_assign_lock` (the very lock the regulator's writes take) with verification.
    seen: list[tuple[int, bool]] = []
    original = ChargingController._async_assign_current_outcome

    async def spy(self, amps, *, verify=False):
        seen.append((amps, verify))
        return await original(self, amps, verify=verify)

    monkeypatch.setattr(ChargingController, "_async_assign_current_outcome", spy)
    lock_states: list[bool] = []
    world.ocpp.lock_probe = lambda _devid: lock_states.append(charger._assign_lock.locked())
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(charger_id, changes={"active_control_enabled": False}))

    assert result["ok"] is True
    assert result["restore"] == {
        "outcome": "restored",
        "chargers": [
            {
                "charger_id": charger_id,
                "charger_name": "Charger 1",
                "outcome": "restored",
                "code": None,
                "from_a": _THROTTLED_A,
                "to_a": _REQUESTED_A,
            }
        ],
    }
    assert seen == [(_REQUESTED_A, True)]
    assert lock_states == [True]
    assert world.ocpp.configure_calls == [(world.devid(charger_id), "1.16,2.10")]
    assert world.ocpp.amps(world.devid(charger_id)) == _REQUESTED_A
    assert result["site"]["active_control"]["enabled"] is False and world.stored() is False

    # Balancing is off: further triggers modulate nothing.
    world.ocpp.configure_calls.clear()
    world.controller.regulator_decisions = {charger_id: _handmade_decision(8.0)}
    world.controller._recompute()
    await hass.async_block_till_done()
    await world.controller._async_apply_active_control()
    assert world.ocpp.configure_calls == []
    assert world.controller._dampers == {}
    # Observation continues.
    assert world.controller.result is not None


async def test_disable_retires_an_admitted_write_that_is_still_in_flight(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """A regulator pass that already reached the charger's write when the switch flips is
    cancelled, awaited, and cannot write after it; only the restore writes afterwards."""
    world = await make_world(hass, prefix="fly")
    await throttle(world)
    charger_id = world.charger_ids[0]
    controller = world.controller
    world.ocpp.hold = asyncio.Event()
    world.ocpp.entered.clear()
    controller._dampers.clear()
    controller.regulator_decisions = {charger_id: _handmade_decision(8.0)}
    controller._schedule_apply_active_control()
    await asyncio.wait_for(world.ocpp.entered.wait(), 5)  # the pass is inside its write
    assert len(world.ocpp.configure_calls) == 1
    pass_task = controller._apply_task
    world.ocpp.hold = None
    client_task = asyncio.ensure_future(
        async_update_site_settings(
            hass, charger_id, expected={}, changes={"active_control_enabled": False}
        )
    )
    site, restore = await asyncio.wait_for(client_task, 10)

    assert pass_task is not None and pass_task.cancelled()
    assert restore["outcome"] in {"restored", "not_needed"}
    # After the disable, the charger carries the authoritative current and nothing else writes.
    assert world.ocpp.amps(world.devid(charger_id)) == _REQUESTED_A
    count = len(world.ocpp.configure_calls)
    await hass.async_block_till_done()
    assert len(world.ocpp.configure_calls) == count
    assert site["active_control"]["enabled"] is False


async def test_a_refused_restore_is_reported_failed_never_restored(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="refuse")
    await throttle(world)
    charger_id = world.charger_ids[0]
    world.ocpp.fail_configure = True
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(charger_id, changes={"active_control_enabled": False}))

    assert result["ok"] is True  # the opt-in is off and was accepted
    assert result["restore"]["outcome"] == "failed"
    only = result["restore"]["chargers"][0]
    assert (only["outcome"], only["code"], only["from_a"], only["to_a"]) == (
        "failed",
        "write_failed",
        _THROTTLED_A,
        _REQUESTED_A,
    )
    assert world.ocpp.amps(world.devid(charger_id)) == _THROTTLED_A  # honestly still throttled
    assert result["site"]["active_control"]["enabled"] is False and world.stored() is False


async def test_an_unconfirmed_restore_is_reported_failed(hass: HomeAssistant, hass_ws_client) -> None:
    """The charger accepts the write but a read-back does not show it: not `restored`."""
    world = await make_world(hass, prefix="unconf")
    await throttle(world)
    world.ocpp.ignore_configure = True
    client = await admin(hass, hass_ws_client)

    result = await call(
        client, v2(world.charger_ids[0], changes={"active_control_enabled": False})
    )

    only = result["restore"]["chargers"][0]
    assert result["restore"]["outcome"] == "failed"
    assert (only["outcome"], only["code"]) == ("failed", ASSIGN_UNCONFIRMED)


async def test_an_unreadable_charger_is_failed_not_not_needed_when_balancing_had_written(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="unread")
    await throttle(world)
    world.ocpp.fail_get = True
    client = await admin(hass, hass_ws_client)

    result = await call(
        client, v2(world.charger_ids[0], changes={"active_control_enabled": False})
    )

    only = result["restore"]["chargers"][0]
    assert only["outcome"] == "failed" and only["code"] == "assigned_current_unreadable"


async def test_a_charger_that_is_no_longer_loaded_is_reported_failed_if_it_was_throttled(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="gone", chargers=2)
    await throttle(world)
    first, second = world.charger_ids
    assert await hass.config_entries.async_unload(second)
    await hass.async_block_till_done()
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(first, changes={"active_control_enabled": False}))

    by_id = {c["charger_id"]: c for c in result["restore"]["chargers"]}
    assert by_id[first]["outcome"] == "restored"
    assert (by_id[second]["outcome"], by_id[second]["code"]) == ("failed", "charger_not_loaded")
    assert result["restore"]["outcome"] == "failed"


async def test_no_authoritative_current_means_failed_for_a_throttled_charger(
    hass: HomeAssistant,
) -> None:
    world = await make_world(hass, prefix="noauth")
    await throttle(world)
    charger = world.charger(world.charger_ids[0])
    charger._requested_current_a = None
    hass.states.async_set(f"number.{world.charger_ids[0]}_connector_1_session_current_limit", "unavailable")

    _site, restore = await async_update_site_settings(
        hass,
        world.charger_ids[0],
        expected={},
        changes={"active_control_enabled": False},
    )

    only = restore["chargers"][0]
    assert restore["outcome"] == "failed"
    assert (only["code"], only["to_a"]) == ("no_authoritative_current", None)


async def test_a_reload_forgot_what_was_written_so_the_charger_itself_is_read(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """After a restart/reload the damper is empty, yet a charger that is really below its
    authoritative current is still restored; one that is above it (not ours to touch) is not."""
    world = await make_world(hass, prefix="fresh", chargers=2)
    first, second = world.charger_ids
    world.ocpp.assigned[world.devid(first)] = f"1.{_THROTTLED_A},2.10"  # lowered in a past session
    world.ocpp.assigned[world.devid(second)] = "1.32,2.10"  # above requested; never ours
    world.controller._dampers.clear()  # the damper knows nothing of the past session
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(first, changes={"active_control_enabled": False}))

    by_id = {c["charger_id"]: c["outcome"] for c in result["restore"]["chargers"]}
    assert by_id == {first: "restored", second: "not_needed"}
    assert world.ocpp.amps(world.devid(first)) == _REQUESTED_A
    assert world.ocpp.amps(world.devid(second)) == 32


async def test_disable_on_an_already_off_site_writes_nothing(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="idle", active=False)
    world.ocpp.assigned[world.devid(world.charger_ids[0])] = f"1.{_THROTTLED_A},2.10"
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": False}))

    assert result["ok"] is True and result["restore"] == {"outcome": "not_needed", "chargers": []}
    assert world.ocpp.configure_calls == []


# ----------------------------------------------------------------------- unload


async def test_a_command_after_the_site_was_unloaded_is_unavailable(
    hass: HomeAssistant, hass_ws_client
) -> None:
    world = await make_world(hass, prefix="unl")
    assert await hass.config_entries.async_unload(world.site_entry.entry_id)
    await hass.async_block_till_done()
    client = await admin(hass, hass_ws_client)

    result = await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": False}))

    assert result["ok"] is False
    assert result["error"] in {ERROR_UNAVAILABLE, ERROR_NO_SITE}
    assert world.stored() is True  # the previous value is kept


async def test_unload_during_a_disable_keeps_the_stored_value_and_says_unavailable(
    hass: HomeAssistant,
) -> None:
    world = await make_world(hass, prefix="mid")
    await throttle(world)
    charger_id = world.charger_ids[0]

    async def unload_now() -> None:
        # The site is unloaded while the restore write is in flight.
        await hass.config_entries.async_unload(world.site_entry.entry_id)

    world.ocpp.on_configure = unload_now
    with pytest.raises(SiteSettingsRefusal) as refusal:
        await async_update_site_settings(
            hass, charger_id, expected={}, changes={"active_control_enabled": False}
        )

    assert refusal.value.code == ERROR_UNAVAILABLE
    assert refusal.value.restore is not None  # the restore that did happen is not hidden
    assert world.stored() is True  # nothing persisted as "disabled"


# -------------------------------------------------- solar changes keep working, under the lock


async def test_a_solar_priority_write_reloads_under_the_lock(hass: HomeAssistant, hass_ws_client) -> None:
    world = await make_world(hass, prefix="sol")
    client = await admin(hass, hass_ws_client)

    result = await call(
        client,
        v2(world.charger_ids[0], changes={"solar_priority": SOLAR_PRIORITY_BATTERY_FIRST}),
    )

    assert result["ok"] is True and result["restore"] is None
    assert result["site"]["solar_priority"] == SOLAR_PRIORITY_BATTERY_FIRST
    assert result["site"]["active_control"]["enabled"] is True


# -------------------------------------------------------------- contract fixtures

Builder = Callable[..., Awaitable[dict[str, Any]]]


async def _fx_disable_restored(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_restored")
    await throttle(world)
    client = await admin(hass, hass_ws_client)
    return await call(
        client,
        v2(
            world.charger_ids[0],
            changes={"active_control_enabled": False},
            expected={"active_control_enabled": True},
        ),
    )


async def _fx_disable_not_needed(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_notneeded")
    client = await admin(hass, hass_ws_client)
    return await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": False}))


async def _fx_disable_restore_failed(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_failed")
    await throttle(world)
    world.ocpp.fail_configure = True
    client = await admin(hass, hass_ws_client)
    return await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": False}))


async def _fx_enable(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_enable", active=False)
    client = await admin(hass, hass_ws_client)
    return await call(
        client,
        v2(
            world.charger_ids[0],
            changes={"active_control_enabled": True},
            expected={"active_control_enabled": False},
        ),
    )


async def _fx_enable_unavailable(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_unavail", active=False, healthy=False)
    client = await admin(hass, hass_ws_client)
    return await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": True}))


async def _fx_conflict(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_conflict")
    client = await admin(hass, hass_ws_client)
    return await call(
        client,
        v2(
            world.charger_ids[0],
            changes={"active_control_enabled": False},
            expected={"active_control_enabled": False},
        ),
    )


async def _fx_invalid_value(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_invalid")
    client = await admin(hass, hass_ws_client)
    return await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": "off"}))


async def _fx_not_admin(hass: HomeAssistant, hass_ws_client, token: str) -> dict[str, Any]:
    world = await make_world(hass, prefix="fx_notadmin")
    client = await non_admin(hass, hass_ws_client, token)
    return await call(client, v2(world.charger_ids[0], changes={"active_control_enabled": False}))


async def _fx_no_site(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    hass.states.async_set("switch.fx_lonely", "off")
    lonely = make_entry(
        hass,
        entry_id="fx_lonely",
        charge_control="switch.fx_lonely",
        current_limit=None,
        webhook_id="webhook-fx-lonely",
        title="fx_lonely",
    )
    assert await hass.config_entries.async_setup(lonely.entry_id)
    await hass.async_block_till_done()
    client = await admin(hass, hass_ws_client)
    return await call(client, v2("fx_lonely", changes={"active_control_enabled": True}))


async def _fx_solar_success(hass: HomeAssistant, hass_ws_client, _token: str) -> dict[str, Any]:
    roof = forecast_entry(hass, entry_id="fx_roof", title="Roof")
    charger, _site = await setup_site_with_charger(
        hass, charger_entry_id="fx_solar", site_entry_id="fx_solar_site"
    )
    client = await admin(hass, hass_ws_client)
    return await call(
        client,
        update_site_settings_message(
            charger_id=charger.entry_id,
            expected={"solar_priority": SOLAR_PRIORITY_CAR_FIRST, "solar_forecast": []},
            changes={
                "solar_priority": SOLAR_PRIORITY_BATTERY_FIRST,
                "solar_forecast": [roof.entry_id],
            },
        ),
    )


SITE_SETTINGS_FIXTURES: Final[dict[str, Builder]] = {
    "success.json": _fx_solar_success,
    "disable_restored.json": _fx_disable_restored,
    "disable_not_needed.json": _fx_disable_not_needed,
    "disable_restore_failed.json": _fx_disable_restore_failed,
    "enable.json": _fx_enable,
    "enable_unavailable.json": _fx_enable_unavailable,
    "conflict.json": _fx_conflict,
    "invalid_value.json": _fx_invalid_value,
    "not_admin.json": _fx_not_admin,
    "no_site.json": _fx_no_site,
}


async def test_the_committed_site_settings_fixtures_are_the_commands_own_output(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    """Every fixture equals what the real command produces, or this test fails. Set
    `SPOTNAV_WRITE_FIXTURES=1` to (re)write the committed files."""
    SITE_SETTINGS_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    produced: dict[str, Any] = {}
    for name, builder in SITE_SETTINGS_FIXTURES.items():
        produced[name] = await builder(hass, hass_ws_client, hass_read_only_access_token)

    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        for name, payload in produced.items():
            (SITE_SETTINGS_FIXTURE_DIR / name).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return

    committed = sorted(path.name for path in SITE_SETTINGS_FIXTURE_DIR.glob("*.json"))
    assert committed == sorted(SITE_SETTINGS_FIXTURES)
    for name, payload in produced.items():
        stored = json.loads((SITE_SETTINGS_FIXTURE_DIR / name).read_text(encoding="utf-8"))
        assert stored == payload, f"{name} no longer matches the command's own output"
