"""OCPP: the current is set by `ChangeConfiguration` on `AssignedCurrent`, one connector's entry at a
time, through the OCPP integration's `get_configuration` and `configure` services.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from ...const import (
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    DEFAULT_MIN_CURRENT_A,
)
from ...vehicles.ocpp_identity import OcppConnectorTarget
from ..charger_profiles import OCPP_POLICY
from .base import (
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_NO_TARGET,
    ASSIGN_READ_FAILED,
    ASSIGN_REBOOT_REQUIRED,
    ASSIGN_UNCONFIRMED,
    ASSIGN_WRITE_FAILED,
    current_description,
    CurrentPath,
)
from .registry import ChargerContext, register_current

# The OCPP logs keep the controller's logger: they are the same messages they always were.
_LOGGER = logging.getLogger("custom_components.spotnav.execution.controller")

_OCPP_DOMAIN = "ocpp"
_OCPP_GET_CONFIGURATION_SERVICE = "get_configuration"
_OCPP_CONFIGURE_SERVICE = "configure"
_OCPP_ASSIGNED_CURRENT_KEY = "AssignedCurrent"


def rewrite_assigned_current(current: str, connector: int, amps: int) -> str:
    """`current` with one connector's assignment replaced, or a `ValueError`.

    `AssignedCurrent` is a comma-separated list of `<connector>.<amps>` pairs that the OCPP integration
    validates strictly, so a reading not of that shape is refused before anything is sent. The list is
    rebuilt in ascending connector order (so it compares with what was read); an unlisted connector is
    added and every other entry keeps its value.
    """
    if connector < 1:
        raise ValueError("A connector to assign must be a positive number")
    if amps < 1:
        raise ValueError("A current to assign must be a positive number")
    entries: dict[int, int] = {}
    for part in current.split(","):
        connector_text, dot, amps_text = part.partition(".")
        if not dot or not connector_text.isdigit() or not amps_text.isdigit():
            raise ValueError("AssignedCurrent is not a list this integration can read")
        other_connector = int(connector_text)
        if other_connector < 1:
            raise ValueError("AssignedCurrent is not a list this integration can read")
        entries[other_connector] = int(amps_text)
    entries[connector] = amps
    return ",".join(
        f"{number}.{value}" for number, value in sorted(entries.items())
    )

def assigned_amps_for_connector(current: str, connector: int) -> int | None:
    """The amps `AssignedCurrent` carries for one connector, or `None`.

    `None` for a string not exactly the `<connector>.<amps>` list, or a connector with no entry. Used
    to confirm a write by reading it back, never to guess.
    """
    found: int | None = None
    for part in current.split(","):
        connector_text, dot, amps_text = part.partition(".")
        if not dot or not connector_text.isdigit() or not amps_text.isdigit():
            return None
        if int(connector_text) == connector:
            found = int(amps_text)
    return found


async def read_assigned_current_value(hass: HomeAssistant, devid: str) -> str | None:
    """The charge point's raw `AssignedCurrent` answer, or `None` when it answered without a value.

    Unlike `OcppAssignedCurrent.async_read_assigned_current`, a call that could not be made or that
    failed raises, so a caller can tell "no answer" from "answered without the key". Read only.
    """
    configuration = await hass.services.async_call(
        _OCPP_DOMAIN,
        _OCPP_GET_CONFIGURATION_SERVICE,
        {"devid": devid, "ocpp_key": _OCPP_ASSIGNED_CURRENT_KEY},
        blocking=True,
        return_response=True,
    )
    value = configuration.get("value") if isinstance(configuration, dict) else None
    return value if isinstance(value, str) else None


class OcppAssignedCurrent(CurrentPath):
    """OCPP `ChangeConfiguration` on `AssignedCurrent`: read, rewrite one connector's entry, write.

    The original path, unchanged: the same two service calls with the same payloads in the same order
    and the same log lines, only moved out of the controller. It has no policy of its own.
    """

    kind = "ocpp"
    can_set = True

    def __init__(self, hass: HomeAssistant, target: Callable[[], OcppConnectorTarget | None]) -> None:
        super().__init__(OCPP_POLICY)
        self.hass = hass
        self._target = target
        #: The last write's answer said a reboot is required (`ocpp.configure`'s `reboot_required`).
        self.reboot_required = False

    async def async_set(self, amps: int, *, reason: str, verify: bool = False) -> str:
        if amps < DEFAULT_MIN_CURRENT_A:
            # Nothing to send, and nothing failed: below the floor there is no valid pilot current
            # to assign, so the value cannot be expressed and no service call is attempted.
            _LOGGER.debug(
                "Not assigning %sA: below the %sA floor there is no valid pilot current, "
                "so nothing was sent",
                amps,
                DEFAULT_MIN_CURRENT_A,
            )
            return ASSIGN_BELOW_MINIMUM
        # One gate, about identity rather than any slider: without a unique connector target there
        # is no connector to command, so the request is recorded without being applied.
        if self._target() is None:
            _LOGGER.warning(
                "This charger has no unique OCPP connector target, so its current cannot be set "
                "through ChangeConfiguration"
            )
            return ASSIGN_NO_TARGET
        read = await self.async_read_assigned_current()
        if read is None:
            _LOGGER.warning(
                "Could not set the charger's current through ChangeConfiguration; "
                "the request is recorded but not applied"
            )
            return ASSIGN_READ_FAILED
        devid, connector, current = read
        try:
            # Raises for a string this integration cannot read; same answer as a failed read: do not write.
            assigned = rewrite_assigned_current(current, connector, amps)
        except Exception:
            _LOGGER.warning(
                "Could not set the charger's current through ChangeConfiguration; "
                "the request is recorded but not applied"
            )
            return ASSIGN_READ_FAILED
        if not await self.async_write_assigned_current(devid, assigned):
            # Deliberately every failure: a missing, unreachable or refusing charger integration is
            # one situation here, the current was not assigned.
            _LOGGER.warning(
                "Could not set the charger's current through ChangeConfiguration; "
                "the request is recorded but not applied"
            )
            return ASSIGN_WRITE_FAILED
        if self.reboot_required:
            # Stored, not applied: the charge point wants a reboot first. Said, never rebooted.
            _LOGGER.warning(
                "The charger stored its assigned current but needs a reboot before it applies it"
            )
            return ASSIGN_REBOOT_REQUIRED
        if verify:
            confirmed = await self.async_read_assigned_current()
            if confirmed is None or assigned_amps_for_connector(confirmed[2], connector) != amps:
                return ASSIGN_UNCONFIRMED
        self.last_written_a = amps
        return ASSIGN_ASSIGNED

    async def async_read_assigned_current(self) -> tuple[str, int, str] | None:
        """Read the charger's `AssignedCurrent` slot."""
        target = self._target()
        if target is None:
            return None
        devid, connector = target.devid, target.connector_id
        try:
            configuration = await self.hass.services.async_call(
                _OCPP_DOMAIN,
                _OCPP_GET_CONFIGURATION_SERVICE,
                {"devid": devid, "ocpp_key": _OCPP_ASSIGNED_CURRENT_KEY},
                blocking=True,
                return_response=True,
            )
        except Exception:
            return None
        current = configuration.get("value") if isinstance(configuration, dict) else None
        if not isinstance(current, str) or not current:
            return None
        return devid, connector, current

    async def async_write_assigned_current(self, devid: str, value: str) -> bool:
        """Write the slot verbatim, reporting whether it went. Writes nothing else.

        The service's answer is asked for and read: lbbrhzn answers `{"reboot_required": bool}` and
        nothing else (a `Rejected` or `NotSupported` reply is only logged by that integration, so it
        cannot be told apart here; `verify` reads the slot back for that). `reboot_required` is kept
        for the caller to report. An integration whose service gives no response is called plainly.
        """
        data = {"devid": devid, "ocpp_key": _OCPP_ASSIGNED_CURRENT_KEY, "value": value}
        self.reboot_required = False
        try:
            try:
                response = await self.hass.services.async_call(
                    _OCPP_DOMAIN, _OCPP_CONFIGURE_SERVICE, data, blocking=True, return_response=True
                )
            except ServiceValidationError as error:
                if error.translation_key != "service_does_not_support_response":
                    raise
                response = None
                await self.hass.services.async_call(
                    _OCPP_DOMAIN, _OCPP_CONFIGURE_SERVICE, data, blocking=True
                )
        except Exception:
            return False
        self.reboot_required = isinstance(response, dict) and response.get("reboot_required") is True
        return True

    def available(self) -> str | None:
        return ASSIGN_NO_TARGET if self._target() is None else None

    def describe(self) -> dict[str, Any]:
        return current_description(self.kind, service=f"{_OCPP_DOMAIN}.{_OCPP_CONFIGURE_SERVICE}")


def _ocpp_current(context: ChargerContext) -> CurrentPath | None:
    return OcppAssignedCurrent(context.hass, context.ocpp_target)


register_current(CURRENT_CONTROL_CHANGE_CONFIGURATION, _ocpp_current)
