"""One-shot diagnostic: what does the charger do when `AssignedCurrent` names a value below 6 A?

Not a feature and no options flow. The regulator sometimes computes that a charger should draw no
current, but `rewrite_assigned_current` refuses values below 1 A and the charger keeps its previous
assignment. `AssignedCurrent` is a vendor key outside OCPP 1.6, so only the charger can say whether
it clamps to the IEC 61851 floor of 6 A, suspends, or refuses; this probe asks it once during a real
charge.

Rules:

* it only ever lowers a current, never touches the charge-control switch or starts anything;
* it runs once per installation and any failure ends it for good, no retries;
* total perturbation is about a hundred seconds;
* while in flight `_async_assign_current` writes nothing, so the regulator cannot overwrite a step.

The restore must never fail: it runs from a `finally` for every exit, including cancellation, and
writes the remembered slot verbatim rather than a rebuilt one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import CURRENT_CONTROL_CHANGE_CONFIGURATION


_LOGGER = logging.getLogger(__name__)

# Every log line starts with this token so one run is one grep.
PROBE_TOKEN = "PILOT_FLOOR_PROBE"

# Strictly descending, never repeated. 5 A is the first value below the IEC 61851 floor;
# 3 and 1 separate "clamps to the floor" from "suspends" from "refuses".
PROBE_STEPS: tuple[int, ...] = (5, 3, 1)

# Hold per step before readings are taken; the whole probe is about a hundred seconds.
PROBE_HOLD_S = 30.0

# How much the car must draw, and for how long, before the probe may start. A car at 0.5 A says
# nothing about the pilot.
FLOOR_CURRENT_A = 5.0
FLOOR_FOR_S = 60.0

# Key of the probe's record inside the controller's existing `Store`.
STORE_KEY = "pilot_floor_probe"


def probe_refusal(
    *,
    completed: bool,
    started: bool,
    current_control: str,
    has_current_limit: bool,
    plan_active: bool,
    charging: bool,
    at_or_above_floor_for_s: float,
) -> str | None:
    """Why the probe may not run right now, or `None`.

    Pure; the reason is the first applicable condition in report order.
    """
    if completed:
        return "already_completed"
    if started:
        # A run began and did not finish (restart or crash); it is not retried.
        return "already_started"
    if current_control != CURRENT_CONTROL_CHANGE_CONFIGURATION:
        return "current_control_is_not_change_configuration"
    if not has_current_limit:
        return "no_current_limit_entity"
    if not plan_active:
        return "no_active_plan"
    if not charging:
        return "charge_not_running"
    if at_or_above_floor_for_s < FLOOR_FOR_S:
        return f"drawing_below_{FLOOR_CURRENT_A:g}a_for_{FLOOR_FOR_S:g}s"
    return None

class ProbeHost(Protocol):
    """What the probe needs from its controller (a protocol, since the controller imports this module)."""

    hass: HomeAssistant
    entry_id: str
    current_control: str
    current_limit: str | None
    charging: bool
    plan: Any

    def probe_records(self) -> dict[str, Any]:
        """The probe's persisted record, as last loaded or written."""

    async def probe_save(self, record: dict[str, Any]) -> None:
        """Persist the probe's record through the controller's own Store."""

    async def probe_read_assigned_current(self) -> tuple[str, int, str] | None:
        """(devid, connector, AssignedCurrent) or None when it cannot be read."""

    async def probe_write_amps(self, devid: str, connector: int, amps: int) -> str | None:
        """Ask for connector to be assigned `amps`; the value written, or None."""

    async def probe_write_assigned_current(self, devid: str, value: str) -> bool:
        """Write the AssignedCurrent string verbatim, reporting whether it went."""

def connector_entity_id(devid: str, connector: int, kind: str) -> str:
    """One of a connector's OCPP sensors: `sensor.<devid>_connector_<n>_<kind>`."""
    return f"sensor.{devid}_connector_{connector}_{kind}"

class PilotFloorProbe:
    """Runs the sequence once; while in flight the site regulator cannot interrupt it."""

    def __init__(
        self,
        host: ProbeHost,
        *,
        now: Callable[[], datetime] = dt_util.utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._host = host
        self._now = now
        self._sleep = sleep
        self._in_flight = False
        self._above_since: datetime | None = None

    @property
    def in_flight(self) -> bool:
        """True while a probe is running: `_async_assign_current` writes nothing."""
        return self._in_flight

    def note_current_import(self, amps: float | None) -> None:
        """Track how long the car has been drawing at or above the floor."""
        if amps is None or amps < FLOOR_CURRENT_A:
            self._above_since = None
            return
        if self._above_since is None:
            self._above_since = self._now()

    def at_or_above_floor_for_s(self) -> float:
        if self._above_since is None:
            return 0.0
        return (self._now() - self._above_since).total_seconds()

    def refusal(self) -> str | None:
        """Why the probe may not run now, or None when it may."""
        if self._in_flight:
            # Checked first: two reports in one loop iteration would both pass the gate, and the
            # second probe would remember and later restore the first one's written value.
            return "already_running"
        record = self._host.probe_records()
        return probe_refusal(
            completed=bool(record.get("completed")),
            started=bool(record.get("started")),
            current_control=self._host.current_control,
            has_current_limit=bool(self._host.current_limit),
            plan_active=self._host.plan is not None,
            charging=bool(self._host.charging),
            at_or_above_floor_for_s=self.at_or_above_floor_for_s(),
        )

    async def async_maybe_run(self) -> None:
        """Run the probe when every condition holds, and say why not when one does not."""
        reason = self.refusal()
        if reason is not None:
            _LOGGER.debug("%s not starting: %s", PROBE_TOKEN, reason)
            return
        # Claimed before the first await so two reports cannot both start a probe.
        self._in_flight = True
        await self._async_run_claimed()

    async def async_run(self) -> None:
        """Run the whole sequence, unless one is already running. Never raises."""
        if self._in_flight:
            _LOGGER.warning("%s is already running; not starting a second", PROBE_TOKEN)
            return
        self._in_flight = True
        await self._async_run_claimed()

    async def _async_run_claimed(self) -> None:
        """The sequence itself. The caller has already set `_in_flight`."""
        host = self._host
        remembered: str | None = None
        devid: str | None = None
        connector: int | None = None

        _LOGGER.info(
            "%s starting: steps=%s hold=%ss", PROBE_TOKEN, list(PROBE_STEPS), PROBE_HOLD_S
        )
        try:
            read = await host.probe_read_assigned_current()
            if read is None:
                _LOGGER.warning(
                    "%s cannot start: AssignedCurrent could not be read, so there would be "
                    "nothing to restore afterwards",
                    PROBE_TOKEN,
                )
                return
            devid, connector, remembered = read
            # Recorded before the first write so a restart never leaves the original slot unknown.
            await self._async_record(
                {
                    "started": True,
                    "remembered": remembered,
                    "devid": devid,
                    "connector": connector,
                }
            )
            _LOGGER.info(
                "%s remembered %s connector %s as %r", PROBE_TOKEN, devid, connector, remembered
            )
            for step in PROBE_STEPS:
                if not await self._async_step(devid, connector, step):
                    return
        except asyncio.CancelledError:
            _LOGGER.warning("%s cancelled; restoring before the task ends", PROBE_TOKEN)
            raise
        except Exception as error:  # noqa: BLE001 - every failure ends the probe
            _LOGGER.warning("%s ended early: %s", PROBE_TOKEN, error)
        finally:
            try:
                await self._async_restore(devid, remembered)
            finally:
                self._in_flight = False
                self._above_since = None
                await self._async_record(
                    {"started": True, "completed": True, "completed_at": self._now().isoformat()}
                )

    async def _async_step(self, devid: str, connector: int, amps: int) -> bool:
        """One step: write, hold, read back, log. False ends the probe."""
        written = await self._host.probe_write_amps(devid, connector, amps)
        if written is None:
            _LOGGER.warning("%s step=%sA: the write did not go; ending", PROBE_TOKEN, amps)
            return False
        # Recorded per step so a restart can say what the slot was left at.
        await self._async_record({"last_written": written})
        await self._sleep(PROBE_HOLD_S)
        read_back = await self._host.probe_read_assigned_current()
        _LOGGER.info(
            "%s step=%sA written=%r read_back=%r status=%s current_import=%s transaction_id=%s",
            PROBE_TOKEN,
            amps,
            written,
            read_back[2] if read_back is not None else None,
            self._reading(connector_entity_id(devid, connector, "status_connector")),
            self._reading(connector_entity_id(devid, connector, "current_import")),
            self._reading(connector_entity_id(devid, connector, "transaction_id")),
        )
        return True

    async def _async_restore(self, devid: str | None, remembered: str | None) -> None:
        """Put the remembered slot back, verbatim. Runs for every exit."""
        if devid is None or remembered is None:
            return
        if await self._host.probe_write_assigned_current(devid, remembered):
            _LOGGER.info("%s restored %r", PROBE_TOKEN, remembered)
            return
        # If this did not go, the charger is still where the last step left it.
        _LOGGER.error(
            "%s could not restore AssignedCurrent: set %s back to %r by hand",
            PROBE_TOKEN,
            devid,
            remembered,
        )

    def _reading(self, entity_id: str) -> str:
        state = self._host.hass.states.get(entity_id)
        return state.state if state is not None else "missing"

    async def _async_record(self, fields: dict[str, Any]) -> None:
        """Merge into the persisted record and save. Never raises: a failed probe must still
        be able to say so, and persistence trouble is no reason to leave a charger mid-step.
        """
        record = dict(self._host.probe_records())
        record.update(fields)
        try:
            await self._host.probe_save(record)
        except Exception:  # noqa: BLE001 - see above
            _LOGGER.warning("%s could not persist its record", PROBE_TOKEN)
