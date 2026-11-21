"""The hold of a site's chargers while a person's hardware test runs (`hardware_test.py`).

The test's battery part charges the home battery from the grid for a few minutes under the HA-down grid charge cap
(`battery/gate.py`): a cap reckoned for Home Assistant stopping in the middle of the charge (Sigenergy has no
dead-man: the command stays), so every charger on the site counts at the current it may draw if nothing writes it
again (`battery/control.py` `_cars_allowed_a`). On a small fuse that leaves no room at all. While the test runs this
hold keeps every charger of the site stopped, and the cap may then count a charger at 0 A where that is sound
(`counts_nothing`).

**What the hold does** (`ChargerTestHold`, one per site, memory only):

* A test whose battery part would run does not start while a charger is on (`charging_now`: charging, or its charge
  control on): `blocked` `car_charging` (`hardware_test.py`).
* From the test's start until it ends (`begin` .. `release`), and never past `until` (the test's deadline and a
  minute: a hold that nothing released ends by itself, so no charger is ever left stuck), every start of every
  charger on the site is refused where it is sent (`ChargingController._start_command_locked`, through the probe
  the site sets, `set_test_hold`): the plan's windows, the sun, a person's Start, load balancing's resume, all of
  them. The charger is not started and the start reads as one that did not go out. Load balancing's resume and its
  battery probe do not even try meanwhile (`SiteCapacityController`).
* A charge the charger begins by itself (a free-charging OCPP charger at plug-in, a person in the charger's own
  app) is stopped (`observe`, at each of the test's polls), as the person hold stops one under a person's Stop
  (`execution/person_hold.py`): at most one stop per `STOP_GAP_S` and `MAX_STOPS` in all per charger; one that
  begins again after that is left alone, and stays counted at its current in the cap.
* At the end of the battery part, by any path (done, failed, deadline, the person's stop, an error, the unload:
  `hardware_test.py` releases it when the battery part is over, in the run's `finally`, after a stop and at the
  shutdown), the hold ends and each charger whose start it refused
  has its plan armed again (`ChargingController.async_rearm`): a window open now starts as at a restore. The sun and
  load balancing decide again by themselves at their next pass. A restart ends the hold with Home Assistant.

**Counted at 0 A** (`counts_nothing`): the cap is reckoned for Home Assistant stopping while the battery charges, so
a charger may count as drawing nothing only where it cannot begin a charge with Home Assistant gone. All of:

* the hold is on, and either an OCPP charger's station ceiling at 0 A was accepted and reads 0 A now (below), or
* the charger reads stopped now: its charge control readable and off, its status not charging, and no Start of
  ours on its way (`ChargingController.control_confirmed_off`, `start_pending`);
* its stop is a setting the charger keeps by itself and that holds every later session off as well
  (`PlatformProfile.stop_kept_without_ha`, on the profile's own start/stop path, not an OCPP charger). With Home
  Assistant gone nobody writes it back, so the charger stays stopped whatever the car does.

**An OCPP charger: its station ceiling at 0 A** (owner 2026-11-21). An OCPP stop ends the transaction only, and a
free-charging charge point (or one that authorizes offline) starts the next one at the plug-in with nobody there to
stop it. So while the hold is on, each OCPP charger of the site with the `ocpp` integration's station "Maximum
Current" number (`OcppCurrentControls.station_maximum_entity`; the integration's 0.13 sends it as the
`ChargePointMaxProfile` on connector 0, at its highest stack level, and raises unless the charge point answers
`Accepted`; an older integration moved the number whatever the answer, so with one older than 0.13.0 nothing is
written, `ocpp_confirms`) is set to 0 A (`_async_lower_ceilings`), and counted at 0 A only once that write was accepted and the
number reads 0 now (`counts_nothing`); a write refused, failed or not there leaves it counted at its current.
Why it holds when Home Assistant stops: a charging profile is the charge point's own state, applied by it to every
transaction whether the central system is connected or not (an offline or free-charging one included), until the
central system replaces or clears it; OCPP 1.6 asks a charge point to keep its profiles across its own reboot as
well, and the integration itself relies on the charger keeping it across Home Assistant's restarts. What is left:
a charge point that drops its profiles at its own reboot, rebooting in the minutes of the test while Home Assistant
is down too, or one that takes a 0 A limit as its minimum current; neither is seen on a real charger yet.
The number's value before is kept in this hold's store (`store_key`) **before** the 0 A is sent, and written back at
every release (`async_restore_ceilings`, from `hardware_test.py`: done, failed, deadline, stop, an error, the unload),
retried every `RESTORE_RETRY_S` until it is taken; after a restart in the middle of a test (Home Assistant died with
the ceiling at 0 A) the store still holds it, and it is written back at the site's next start (`async_load`). A
number that read no value before is set back to its own maximum. The integration's own slider is this same number:
while the test runs it reads 0 A, and afterwards the person's value.

Every other control kind keeps its current counted, because there the stop ends or pauses the running session
only and the charger can begin a charge again by itself without Home Assistant: Easee (its pause service; a charger without authorization charges at the plug-in), start/stop buttons (a session
command; the next plug-in starts a new session), pause switches and the 0 A pause (the session's own state), go-e's
force state (back to neutral at the unplug). A charger that begins charging during the test is counted again at
once, at the next look, whatever kind it is (it no longer reads stopped).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import CONF_CHARGER_ENTRY_IDS, DOMAIN

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: The least time between two stops of one charger under the hold (s), and the most stops of one charger in a test.
STOP_GAP_S: Final = 30.0
MAX_STOPS: Final = 3
#: How long a write of an OCPP charger's station ceiling may take (s), and how long until a restore not taken is
#: tried again (s).
CEILING_CALL_S: Final = 30.0
RESTORE_RETRY_S: Final = 60.0
#: What a station ceiling that read no value before is set back to, when its number states no maximum (A).
CEILING_FALLBACK_A: Final = 32.0
STORE_VERSION: Final = 1


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.charger_test_hold.{entry_id}"


def _amps(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


#: The `ocpp` integration from which its station "Maximum Current" raises when the charge point does not accept the
#: limit (0.13.0); an older one moved the number whatever the answer, so its write confirms nothing.
OCPP_CONFIRMING_VERSION: Final = (0, 13, 0)


def ocpp_confirms(hass: Any) -> bool:
    """Whether the loaded `ocpp` integration's station number raises on a limit the charge point refused (its version
    at least `OCPP_CONFIRMING_VERSION`). `False` when that cannot be read."""
    from homeassistant.loader import async_get_loaded_integration  # noqa: PLC0415 - kept beside its only use

    try:
        version = str(async_get_loaded_integration(hass, "ocpp").version or "")
        parts = tuple(int(part) for part in version.split(".")[:3])
    except Exception:  # noqa: BLE001 - not known: nothing is confirmed
        return False
    return len(parts) == 3 and parts >= OCPP_CONFIRMING_VERSION


def station_ceiling_entity(controller: Any) -> str | None:
    """The `ocpp` integration's station "Maximum Current" number of an OCPP charger, `None` for any other charger or
    when it is not there. Never raises."""
    try:
        if getattr(controller, "ocpp_target", None) is None:
            return None
        controls = controller.ocpp_controls
        return None if controls is None else controls.station_maximum_entity or None
    except Exception:  # noqa: BLE001 - not found: the charger stays counted
        _LOGGER.debug("station_ceiling_entity: ignored failure", exc_info=True)
        return None


def stop_kept_without_ha(controller: Any) -> bool:
    """Whether this charger's stop is a setting it keeps by itself that holds every later session off
    (`PlatformProfile.stop_kept_without_ha`): its profile says so, it runs on the profile's own start/stop path, and it
    is not an OCPP charger. `False` whenever any of it cannot be read."""
    from ..execution.charger_profiles import profile_for  # noqa: PLC0415 - the charger side imports the site

    try:
        if getattr(controller, "ocpp_target", None) is not None:
            return False
        adapter = controller.adapter
        profile = profile_for(adapter.platform)
        if profile is None or not profile.stop_kept_without_ha or profile.start_stop is None:
            return False
        return getattr(adapter.path, "kind", None) == profile.start_stop.kind
    except Exception:  # noqa: BLE001 - not known: the charger stays counted
        _LOGGER.debug("stop_kept_without_ha: ignored failure (not known: the charger stays counted)", exc_info=True)
        return False


class ChargerTestHold:
    """The hold of one site's chargers during a hardware test (see the module)."""

    def __init__(self, site: SiteCapacityController, *, controller_for: Callable[[str], Any] | None = None) -> None:
        self._site = site
        self._controller_for = controller_for
        #: When the hold ends by itself, `None` while none is on.
        self._until: datetime | None = None
        #: The chargers whose start the hold refused (their plans are armed again at the release).
        self._refused: set[str] = set()
        #: Per charger, the stops sent under this hold and when the last one was.
        self._stops: dict[str, tuple[int, datetime]] = {}
        #: Per OCPP charger: its station ceiling's number and value before (kept in the store until written back),
        #: and the chargers whose 0 A was accepted under this hold.
        self._ceilings: dict[str, dict[str, Any]] = {}
        self._lowered: set[str] = set()
        self._lower_task: asyncio.Task[None] | None = None
        self._restore_lock = asyncio.Lock()
        self._retry_cancel: Callable[[], None] | None = None
        self._closed = False
        hass = getattr(site, "hass", None)
        self._store: Store[dict[str, Any]] | None = (
            None if hass is None else Store(hass, STORE_VERSION, store_key(site.entry_id))
        )

    # ---- state

    @property
    def active(self) -> bool:
        until = self._until
        return until is not None and dt_util.utcnow() < until

    @property
    def until(self) -> datetime | None:
        return self._until if self.active else None

    def _charger_ids(self) -> list[str]:
        return list(self._site.config.get(CONF_CHARGER_ENTRY_IDS) or [])

    def _controller(self, charger_entry_id: str) -> Any:
        if self._controller_for is not None:
            return self._controller_for(charger_entry_id)
        from ..runtime import controller_for  # noqa: PLC0415 - the charger side imports the site

        return controller_for(self._site.hass, charger_entry_id)

    def holds(self, charger_entry_id: str) -> bool:
        """Whether a start of this charger is refused now (the charger's probe, `set_test_hold`)."""
        return self.active and charger_entry_id in self._charger_ids()

    def note_refused(self, charger_entry_id: str) -> None:
        if charger_entry_id not in self._refused:
            _LOGGER.info(
                "SpotNav site %s: a start of charger %s waits for the hardware test to end",
                self._site.entry_id, charger_entry_id,
            )
        self._refused.add(charger_entry_id)

    @staticmethod
    def _on(controller: Any) -> bool:
        return bool(controller.charging or controller.charge_control_on)

    def charging_now(self) -> list[str]:
        """The site's chargers that are on now: charging, or their charge control on (a Start in effect). A charger
        that cannot be read is not on by this (it stays counted in the cap)."""
        found: list[str] = []
        for charger_entry_id in self._charger_ids():
            controller = self._controller(charger_entry_id)
            if controller is None:
                continue
            try:
                if self._on(controller):
                    found.append(charger_entry_id)
            except Exception:  # noqa: BLE001 - not known: not taken as on, and counted in the cap
                _LOGGER.debug("charging_now: ignored failure", exc_info=True)
        return found

    def counts_nothing(self, charger_entry_id: str) -> bool:
        """Whether the HA-down grid charge cap may count this charger at 0 A now (see the module): the hold on, and
        either an OCPP charger whose station ceiling at 0 A was accepted and reads 0 A now, or a charger read stopped
        with no Start of ours on its way whose stop it keeps without Home Assistant. `False` whenever any of it cannot
        be read."""
        if not self.holds(charger_entry_id):
            return False
        controller = self._controller(charger_entry_id)
        if controller is None:
            return False
        if charger_entry_id in self._lowered:
            # An OCPP charger whose station ceiling at 0 A was accepted, and that reads 0 A now (see the module).
            entity_id = (self._ceilings.get(charger_entry_id) or {}).get("entity_id")
            state = None if entity_id is None else self._site.hass.states.get(entity_id)
            return state is not None and _amps(state.state) == 0.0
        try:
            stopped = controller.control_confirmed_off and not controller.start_pending
        except Exception:  # noqa: BLE001 - not known: counted
            _LOGGER.debug("counts_nothing: ignored failure (not known: counted)", exc_info=True)
            return False
        return bool(stopped) and stop_kept_without_ha(controller)

    # ---- the hold's life

    def begin(self, until: datetime) -> None:
        """Hold every charger of the site stopped until `release`, and never past `until`."""
        self._until = until
        self._refused = set()
        self._stops = {}
        self._lowered = set()
        _LOGGER.info("SpotNav site %s: the chargers are held stopped for the hardware test", self._site.entry_id)
        self.observe()
        if self._store is not None and any(
            station_ceiling_entity(self._controller(charger_entry_id)) for charger_entry_id in self._charger_ids()
        ):
            self._lower_task = self._site.hass.async_create_task(self._async_lower_ceilings())

    def release(self) -> list[str]:
        """End the hold at once; the chargers whose start it refused (for `async_rearm_refused`). Never raises."""
        was_on = self._until is not None
        self._until = None
        refused, self._refused = sorted(self._refused), set()
        self._stops = {}
        self._lowered = set()
        if was_on:
            _LOGGER.info("SpotNav site %s: the hardware test's hold of the chargers ended", self._site.entry_id)
        return refused

    async def async_rearm_refused(self, refused: list[str]) -> None:
        """Arm the plans of the chargers whose start the hold refused again, each on its own (one that fails leaves
        the others). Never raises."""
        for charger_entry_id in refused:
            controller = self._controller(charger_entry_id)
            rearm = getattr(controller, "async_rearm", None)
            if rearm is None:
                continue
            try:
                await rearm()
            except Exception:  # noqa: BLE001 - its next timer or report decides again
                _LOGGER.warning(
                    "SpotNav site %s: the plan of charger %s could not be armed again after the hardware test",
                    self._site.entry_id, charger_entry_id, exc_info=True,
                )

    # ---- an OCPP charger's station ceiling

    async def async_load(self) -> None:
        """The ceilings a test left at 0 A when Home Assistant stopped in its middle: written back from now on, in
        the background, until each is taken. Never raises."""
        if self._store is None:
            return
        try:
            raw = await self._store.async_load()
        except Exception:  # noqa: BLE001 - an unreadable store holds nothing to write back
            _LOGGER.warning("SpotNav site %s: the hardware test's kept ceilings could not be read", self._site.entry_id)
            return
        kept = (raw or {}).get("ceilings") if isinstance(raw, dict) else None
        if not isinstance(kept, dict):
            return
        for charger_entry_id, row in kept.items():
            if isinstance(row, dict) and isinstance(row.get("entity_id"), str):
                self._ceilings[str(charger_entry_id)] = {"entity_id": row["entity_id"], "previous": _amps(row.get("previous"))}
        if self._ceilings:
            _LOGGER.warning(
                "SpotNav site %s: a hardware test was cut short with an OCPP charger's maximum current at 0 A; "
                "it is set back now", self._site.entry_id,
            )
            self._site.hass.async_create_task(self.async_restore_ceilings())

    async def _async_save(self) -> None:
        if self._store is not None:
            await self._store.async_save({"ceilings": {key: dict(row) for key, row in self._ceilings.items()}})

    async def _async_set(self, entity_id: str, amps: float) -> None:
        async with asyncio.timeout(CEILING_CALL_S):
            await self._site.hass.services.async_call(
                "number", "set_value", {"entity_id": entity_id, "value": amps}, blocking=True
            )

    async def _async_lower_ceilings(self) -> None:
        """Each OCPP charger's station ceiling to 0 A (see the module): its value before kept first, then the write;
        counted at 0 A only once the write was taken. Nothing where the `ocpp` integration's write confirms nothing
        (`ocpp_confirms`): those chargers stay counted. Never raises."""
        if not ocpp_confirms(self._site.hass):
            _LOGGER.info(
                "SpotNav site %s: the ocpp integration is older than 0.13.0, whose maximum current says whether the "
                "charge point took it; its chargers stay counted at their current during the hardware test",
                self._site.entry_id,
            )
            return
        for charger_entry_id in self._charger_ids():
            if not self.active or self._closed:
                return
            entity_id = station_ceiling_entity(self._controller(charger_entry_id))
            state = None if entity_id is None else self._site.hass.states.get(entity_id)
            if entity_id is None or state is None or state.state in ("unavailable", "unknown"):
                continue
            if charger_entry_id not in self._ceilings:
                self._ceilings[charger_entry_id] = {"entity_id": entity_id, "previous": _amps(state.state)}
            try:
                # Kept before the 0 A goes out: Home Assistant stopping right after it must still find what to set back.
                await self._async_save()
                await self._async_set(entity_id, 0.0)
            except Exception as err:  # noqa: BLE001 - not taken: the charger stays counted, and is set back anyway
                _LOGGER.warning(
                    "SpotNav site %s: the maximum current of OCPP charger %s could not be set to 0 A for the hardware "
                    "test (%s); it stays counted at its current", self._site.entry_id, charger_entry_id, err,
                )
                continue
            now = self._site.hass.states.get(entity_id)
            if self.active and now is not None and _amps(now.state) == 0.0:
                self._lowered.add(charger_entry_id)
                _LOGGER.info(
                    "SpotNav site %s: OCPP charger %s holds 0 A for the hardware test", self._site.entry_id,
                    charger_entry_id,
                )

    async def async_restore_ceilings(self) -> None:
        """Every station ceiling this hold (or one cut short by a restart) set to 0 A, back to its value before; one
        not taken is tried again every `RESTORE_RETRY_S` and stays in the store meanwhile. Never raises."""
        task, self._lower_task = self._lower_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        async with self._restore_lock:
            failed = False
            for charger_entry_id, row in list(self._ceilings.items()):
                entity_id = row["entity_id"]
                state = self._site.hass.states.get(entity_id)
                value = row.get("previous")
                if value is None:
                    value = _amps(None if state is None else state.attributes.get("max")) or CEILING_FALLBACK_A
                if self.active and charger_entry_id in self._lowered:
                    continue  # a new hold holds it again
                try:
                    if state is None or state.state == "unavailable":
                        raise RuntimeError("the number is not there yet")
                    await self._async_set(entity_id, float(value))
                except Exception as err:  # noqa: BLE001 - tried again; kept in the store meanwhile
                    failed = True
                    _LOGGER.warning(
                        "SpotNav site %s: the maximum current of OCPP charger %s could not be set back to %s A after "
                        "the hardware test (%s); tried again in %s s", self._site.entry_id, charger_entry_id, value,
                        err, int(RESTORE_RETRY_S),
                    )
                    continue
                self._ceilings.pop(charger_entry_id, None)
                _LOGGER.info(
                    "SpotNav site %s: the maximum current of OCPP charger %s is back at %s A", self._site.entry_id,
                    charger_entry_id, value,
                )
            try:
                await self._async_save()
            except Exception:  # noqa: BLE001 - the next write saves it
                _LOGGER.debug("Saving the hardware test's ceilings failed", exc_info=True)
            if failed and not self._closed and self._retry_cancel is None:
                self._retry_cancel = async_call_later(self._site.hass, RESTORE_RETRY_S, self._retry)

    @callback
    def _retry(self, _now: datetime) -> None:
        self._retry_cancel = None
        if not self._closed:
            self._site.hass.async_create_task(self.async_restore_ceilings())

    @property
    def ceilings_kept(self) -> list[str]:
        """The OCPP chargers whose station ceiling is still to be set back."""
        return sorted(self._ceilings)

    async def async_close(self) -> None:
        """The site unloads: no more retries; what is still to be set back stays in the store for the next start."""
        self._closed = True
        cancel, self._retry_cancel = self._retry_cancel, None
        if cancel is not None:
            cancel()
        task, self._lower_task = self._lower_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @staticmethod
    async def async_remove_stored(hass: Any, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    def observe(self) -> None:
        """Stop a charge that began under the hold (see the module), bounded per charger. Never raises."""
        if not self.active:
            return
        now = dt_util.utcnow()
        for charger_entry_id in self._charger_ids():
            controller = self._controller(charger_entry_id)
            if controller is None:
                continue
            try:
                if not self._on(controller) or controller.stop_settling:
                    continue
            except Exception:  # noqa: BLE001 - not read: nothing sent
                _LOGGER.debug("observe: ignored failure", exc_info=True)
                continue
            count, last = self._stops.get(charger_entry_id, (0, None))
            if count >= MAX_STOPS:
                continue
            if last is not None and (now - last).total_seconds() < STOP_GAP_S:
                continue
            self._stops[charger_entry_id] = (count + 1, now)
            if count + 1 == MAX_STOPS:
                _LOGGER.warning(
                    "SpotNav site %s: charger %s began charging during the hardware test; the last stop is sent, "
                    "it stays counted at its current",
                    self._site.entry_id, charger_entry_id,
                )
            else:
                _LOGGER.info(
                    "SpotNav site %s: charger %s began charging during the hardware test; stopping it",
                    self._site.entry_id, charger_entry_id,
                )
            self._site.hass.async_create_task(self._async_stop(charger_entry_id, controller))

    async def _async_stop(self, charger_entry_id: str, controller: Any) -> None:
        if not self.active:
            return
        try:
            await controller.async_stop(urgent=False)
        except Exception as err:  # noqa: BLE001 - tried again after the gap, while the hold lasts
            _LOGGER.info(
                "SpotNav site %s: the hardware test's stop of charger %s failed (%s)",
                self._site.entry_id, charger_entry_id, type(err).__name__,
            )

    def as_dict(self) -> dict[str, Any]:
        until = self.until
        return {
            "active": until is not None,
            "until": None if until is None else until.isoformat(),
            "refused": sorted(self._refused),
            "ocpp_at_zero": sorted(self._lowered),
            "ceilings_to_restore": self.ceilings_kept,
            "stops": {charger_entry_id: count for charger_entry_id, (count, _last) in self._stops.items()},
        }
