"""The charger adapter: one small surface over every way SpotNav can drive a charger.

`ChargerAdapter` answers what the controller needs and nothing else: start, stop, set a current,
whether the charger is charging, what current it measures, its energy register, and what it supports
(`AdapterCapabilities`) under which write policy (`charger_profiles.WritePolicy`). It is built from
two interchangeable parts plus read-only facts:

* a **start/stop path**: a switch (optionally inverted), a select option, a button pair, or a
  vendor's own service (`generic.py`, `easee.py`);
* a **current path**: none, OCPP's `ChangeConfiguration` on `AssignedCurrent` (`ocpp.py`),
  `number.set_value` under the write policy, or a vendor's own service; each is built by the factory
  its module registered (`registry.py`);
* read-only facts: a status sensor and the values that mean charging, measured-current sensors (A or
  mA) and the energy register.

The controller keeps its locks, deadband, dwell and yield stepping; this module only decides whether
one write may go out *now* and sends it. Nothing here waits for a rate limit: a write the policy does
not allow is refused with a stable code and the controller decides what that means for the fuse
(`ChargingController.async_apply_regulated_current`). Every write is reported as written only after
the service call returned.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...const import DEFAULT_MIN_CURRENT_A
from ...vehicles.ocpp_identity import OcppConnectorTarget
from ...vehicles.soc_estimate import read_energy_register_kwh
from ..charger_profiles import OCPP_NUMBER_POLICY, OCPP_POLICY, WritePolicy
from .base import (
    _amps_factor,
    _finite,
    _lower,
    AdapterCapabilities,
    CurrentPath,
    StartStopPath,
    WriteRateLimiter,
)
from .generic import NoCurrent
from .ocpp import OcppAssignedCurrent
from .registry import build_current, build_start_stop, ChargerContext


#: After a Start, how long a charger that has not yet reported itself enabled is still treated as
#: started for the one rule that depends on it (a current is not written while paused). Its report
#: lags the command; a start that really failed leaves the value stored, not lost.
START_GRACE_S: Final = 30.0

#: The status values the progress check reads, in OCPP's own spelling (`charge_progress.py`).
PROGRESS_CHARGING: Final = "Charging"
PROGRESS_SUSPENDED_EV: Final = "SuspendedEV"


class ChargerAdapter:
    """One charger as the controller drives it. See the module docstring."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        platform: str | None,
        path: StartStopPath,
        current: CurrentPath,
        policy: WritePolicy,
        status_entity_id: str | None = None,
        charging_values: tuple[str, ...] = (),
        vehicle_idle_values: tuple[str, ...] = (),
        held_values: tuple[str, ...] = (),
        min_start_current_a: float | None = None,
        current_entity_ids: tuple[str, ...] = (),
        energy_entity_id: str | None = None,
        current_enabled: bool = False,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self.hass = hass
        self._now = now
        self._started_at: datetime | None = None
        #: The person's explicit opt-in to writing a current (`CONF_CURRENT_CONTROL`); a path that
        #: could write is still never used without it.
        self.current_enabled = current_enabled
        self.platform = platform
        self.path = path
        self.current = current
        self.policy = policy
        self.status_entity_id = status_entity_id
        self._charging_values = tuple(value.lower() for value in charging_values)
        self._idle_values = tuple(value.lower() for value in vehicle_idle_values)
        self._held_values = tuple(value.lower() for value in held_values)
        #: The lowest current a charge is started at: the profile's, never below the 6 A floor.
        self.min_start_current_a = max(DEFAULT_MIN_CURRENT_A, min_start_current_a or 0.0)
        self.current_entity_ids = current_entity_ids
        self.energy_entity_id = energy_entity_id

    # -- commands

    async def async_start(self) -> bool:
        """Start the charge; `False` when the command was not executed (its entity is unavailable)."""
        self._started_at = self._now()
        executed = await self.path.async_start()
        if not executed:
            self._started_at = None
        return executed

    async def async_stop(self) -> bool:
        """Stop the charge; `False` when the command was not executed (its entity is unavailable)."""
        self._started_at = None
        return await self.path.async_stop()

    async def async_set_current(self, amps: int, *, reason: str, verify: bool = False) -> str:
        return await self.current.async_set(amps, reason=reason, verify=verify)

    # -- reads

    def enabled_state(self) -> bool | None:
        return self.path.enabled_state()

    def enabled_for_writes(self) -> bool | None:
        """`enabled_state`, except that a charger just started counts as enabled for
        `START_GRACE_S` while it has not yet said so.
        """
        state = self.path.enabled_state()
        if state is False and self._started_at is not None:
            if (self._now() - self._started_at).total_seconds() < START_GRACE_S:
                return True
        return state

    def charging_state(self) -> bool:
        """Whether the charger is charging: the status sensor when one is configured and readable,
        otherwise the control (the switch is on, the option is the start one).
        """
        status = self._status()
        if status is not None:
            return status in self._charging_values
        return self.path.enabled_state() is True

    def _status(self) -> str | None:
        if not self.status_entity_id:
            return None
        return _lower(self.hass.states.get(self.status_entity_id))

    def held_by_charger(self) -> bool:
        """Whether the charger's own scheduler or load balancer holds the charge (Easee's
        `awaiting_scheduled_start`, `awaiting_smart_start`, `awaiting_load_balancing`,
        `paused_due_to_equalizer`): a Start was taken, and sending it again releases nothing.
        """
        status = self._status()
        return status is not None and status in self._held_values

    def progress_status(self) -> str | None:
        """The status in the progress check's vocabulary: `Charging`, `SuspendedEV` (connected, the
        vehicle asks for no current) or the raw text; `None` with no readable status sensor.
        """
        status = self._status()
        if status is None:
            return None
        if status in self._charging_values:
            return PROGRESS_CHARGING
        if status in self._idle_values:
            return PROGRESS_SUSPENDED_EV
        return status

    def measured_current_a(self) -> float | None:
        """The highest phase current measured, in A, or `None` (none configured or unreadable)."""
        values: list[float] = []
        for entity_id in self.current_entity_ids:
            state = self.hass.states.get(entity_id)
            value = _finite(state.state) if state is not None else None
            factor = _amps_factor(state)
            if value is None or factor is None:
                continue
            values.append(value / factor)
        return max(values) if values else None

    def energy_register_kwh(self) -> float | None:
        return read_energy_register_kwh(self.hass, self.energy_entity_id)

    def state_entity_ids(self) -> tuple[str, ...]:
        entity_ids = list(self.path.entity_ids())
        if self.status_entity_id:
            entity_ids.append(self.status_entity_id)
        return tuple(entity_ids)

    # -- what it supports

    @property
    def capabilities(self) -> AdapterCapabilities:
        can_set = self.current.can_set and self.current_enabled
        return AdapterCapabilities(
            start_stop=True,
            set_current=can_set and self.current.available() is None,
            regulated_current=(
                can_set and self.policy.regulator_writes and self.current.available() is None
            ),
            reads_charging_state=bool(self.status_entity_id),
            reads_measured_current=bool(self.current_entity_ids),
            reads_energy_register=bool(self.energy_entity_id),
        )

    @property
    def is_ocpp(self) -> bool:
        return isinstance(self.current, OcppAssignedCurrent)

    def describe(self) -> dict[str, Any]:
        """The control path and its write policy, as the entity configuration reports them."""
        return {
            "platform": self.platform,
            "start_stop": self.path.describe(),
            "current": self.current.describe(),
            "charging_state": {
                "source": "status" if self.status_entity_id else "control",
                "entity_id": self.status_entity_id,
            },
            "policy": self.policy.as_dict(),
            "capabilities": self.capabilities.as_dict(),
        }


def build_adapter(
    hass: HomeAssistant,
    config: dict[str, Any],
    *,
    ocpp_target: Callable[[], OcppConnectorTarget | None],
    energy_entity_id: str | None,
    now: Callable[[], datetime] = dt_util.utcnow,
) -> ChargerAdapter:
    """The adapter a charger's stored configuration describes.

    A charger without the detected-charger keys gets exactly what it always had: its charge-control
    switch as the start/stop path and the charging state, and (only when it opted in) OCPP's
    `ChangeConfiguration` for the current. `CONF_CURRENT_CONTROL` stays the explicit opt-in for every
    path that writes a current; the profile only says what such a write is allowed to do. The paths
    themselves come from the registry (`registry.py`).
    """
    from ...const import (
        CONF_CHARGE_CONTROL,
        CONF_CHARGER_CURRENT_ENTITIES,
        CONF_CHARGER_PLATFORM,
        CONF_CHARGING_STATE,
        CONF_CONTROL_PATH,
        CONF_CURRENT_CONTROL,
        CONF_CURRENT_LIMIT,
        CONF_MODE,
        CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CURRENT_CONTROL_NUMBER,
        MODE_OCPP,
    )
    from ..charger_profiles import policy_for, profile_for

    platform = config.get(CONF_CHARGER_PLATFORM) or None
    charge_control: str = config[CONF_CHARGE_CONTROL]
    control = config.get(CONF_CURRENT_CONTROL) or ""
    current_limit = config.get(CONF_CURRENT_LIMIT) or None
    raw_path = config.get(CONF_CONTROL_PATH)
    raw_path = raw_path if isinstance(raw_path, dict) else {}

    if control == CURRENT_CONTROL_NUMBER and config.get(CONF_MODE) == MODE_OCPP:
        policy = OCPP_NUMBER_POLICY
    elif control == CURRENT_CONTROL_CHANGE_CONFIGURATION or config.get(CONF_MODE) == MODE_OCPP:
        policy = OCPP_POLICY
    else:
        policy = policy_for(platform)
    limiter = WriteRateLimiter(policy, now)

    raw_state = config.get(CONF_CHARGING_STATE)
    raw_state = raw_state if isinstance(raw_state, dict) else {}
    status_entity_id = raw_state.get("entity_id") or None
    profile = profile_for(platform)
    charging_values = tuple(str(v) for v in (raw_state.get("charging_values") or ()))
    idle_values = tuple(profile.vehicle_idle_values) if profile is not None else ()
    held_values = tuple(profile.held_values) if profile is not None else ()
    min_start_a = profile.min_start_current_a if profile is not None else None

    holder: dict[str, ChargerAdapter] = {}
    context = ChargerContext(
        hass=hass,
        config=config,
        raw_path=raw_path,
        profile=profile,
        policy=policy,
        limiter=limiter,
        now=now,
        charge_control=charge_control,
        current_limit=current_limit,
        status_entity_id=status_entity_id,
        status=lambda: holder["adapter"]._status(),  # noqa: SLF001 - the adapter reads its own sensor
        read_back=lambda: holder["adapter"].current.read_back_a(),
        enabled_for_writes=lambda: holder["adapter"].enabled_for_writes(),
        ocpp_target=ocpp_target,
    )
    path = build_start_stop(context)

    current: CurrentPath | None = None
    if control == CURRENT_CONTROL_CHANGE_CONFIGURATION or (
        config.get(CONF_MODE) == MODE_OCPP and not control
    ):
        # An OCPP charger that has not opted in still has its path (callers gate on the opt-in).
        current = build_current(CURRENT_CONTROL_CHANGE_CONFIGURATION, context)
    else:
        current = build_current(control, replace(context, path=path))
    if current is None:
        current = NoCurrent()

    current_entities = config.get(CONF_CHARGER_CURRENT_ENTITIES)
    adapter = ChargerAdapter(
        hass,
        platform=platform,
        path=path,
        current=current,
        policy=policy,
        status_entity_id=status_entity_id,
        charging_values=charging_values,
        vehicle_idle_values=idle_values,
        held_values=held_values,
        min_start_current_a=min_start_a,
        current_entity_ids=tuple(current_entities) if isinstance(current_entities, (list, tuple)) else (),
        energy_entity_id=energy_entity_id,
        current_enabled=bool(control),
        now=now,
    )
    holder["adapter"] = adapter
    return adapter
