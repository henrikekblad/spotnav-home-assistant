"""The hold of a site's chargers while a person's hardware test runs (`hardware_test.py`).

The test's battery part charges the home battery from the grid for a few minutes under the HA-down grid charge cap
(`battery/gate.py`): a cap reckoned for Home Assistant stopping in the middle of the charge (Sigenergy has no
dead-man: the command stays), so every charger on the site counts at the current it may draw if nothing writes it
again (`battery/control.py` `_cars_allowed_a`). On a small fuse that leaves no room at all. While the test runs this
hold keeps every charger of the site stopped, and the cap may then count a charger at 0 A where that is sound
(`counts_nothing`).

**What the hold does** (`ChargerTestHold`, one per site, memory only):

* A test whose battery part would run does not start while a charger is on (`charging_now`: charging, its charge
  control on, or drawing by a measurement): `blocked` `car_charging` (`hardware_test.py`).
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

* the hold is on, and either an OCPP charger is out of service by its own report and not charging (below), or
* the charger reads stopped now: its charge control readable and off with its stop option itself read back
  (`enabled_state` false: a GARO in Schedule, a Charge Amps on its schedule or a zappi in Eco reads neither, and may
  begin a charge by itself), none of its own modes on (`charger_conflicts`), its status not charging, nothing
  drawing by a measurement (`_drawing`), and no Start of ours on its way (`ChargingController.control_confirmed_off`,
  `start_pending`);
* its stop is a setting the charger keeps by itself and that holds every later session off as well
  (`PlatformProfile.stop_kept_without_ha`, on the profile's own start/stop path, not an OCPP charger). With Home
  Assistant gone nobody writes it back, so the charger stays stopped whatever the car does.

**An OCPP charger: the charge point taken out of service** (owner 2026-11-21). An OCPP stop ends the transaction
only, and a free-charging charge point (or one that authorizes offline) starts the next one at the plug-in with nobody
there to stop it. So while the hold is on, each OCPP charger of the site is set unavailable with the `ocpp`
integration's station availability switch (`station_availability_entity`: platform `ocpp`, unique id
`switch.ocpp.<cpid>.availability`; it sends `ChangeAvailability` `Inoperative` on connector 0, the whole charge
point), and counted at 0 A only once the switch reads off, the charger's connector reports itself `Unavailable`
(`connector_out_of_service`) and the charger neither charges nor draws (`counts_nothing`). The connector's status is
the charge point's own word that it took the change: the integration counts `Scheduled` (a transaction runs, out of
service only once it ends) as taken, and on a charge point that reports no station status the switch reads off for a
connector merely `Preparing`. A switch that is not there, does
not read on before (a person's own `Inoperative` is never taken over, nor turned on after) or does not come to read
off within `CONFIRM_S` leaves the charger counted at its current. **No charging profile and no station maximum current
are ever written** (owner rule: repeated `SetChargingProfile` writes once left a charge point's profile handling
broken).
Why it holds when Home Assistant stops: OCPP 1.6 makes availability persistent: a charge point set `Inoperative`
stays so, also across its own reboot, until the central system sets it `Operative` again; an inoperative charge point
starts no transaction, free charging or offline authorization included. The side effect: the charger shows as
unavailable for the minutes of the test, and if Home Assistant dies in the middle it stays unavailable until Home
Assistant is back and sets it operative (the safe direction: no charge rather than an unguarded one).
That it was taken out of service is kept in this hold's store (`store_key`) **before** the command goes out, and it is
set back to operative at every release (`async_restore_availability`, from `hardware_test.py`: done, failed, deadline,
stop, an error, the unload), confirmed by the switch reading on again, retried every `RESTORE_RETRY_S` (after
`RESTORE_TRIES` of them every `RESTORE_SLOW_RETRY_S`, until it is on); after a restart in the middle of a test the
store still holds it, and it is set back at the site's next start (`async_load`). A switch that reads on again by the
time it is set back (a person turned it on) is left as it is. After `ISSUE_AFTER_TRIES` setting backs not confirmed
the person is told in Repairs (turn the switch on yourself), gone once it is on. At the removal of the site's entry the
record goes only once every switch reads on.

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

from ..const import CONF_CHARGER_ENTRY_IDS, DOMAIN, HARDWARE_TEST_OUT_OF_SERVICE_ISSUE

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: The least time between two stops of one charger under the hold (s), and the most stops of one charger in a test.
STOP_GAP_S: Final = 30.0
MAX_STOPS: Final = 3
#: How long a command to an OCPP charger's availability switch may take (s), how long its switch is given to read
#: what was asked (s), how long until a setting back not confirmed is tried again (s), and how many times a start.
CALL_S: Final = 30.0
CONFIRM_S: Final = 30.0
RESTORE_RETRY_S: Final = 60.0
RESTORE_TRIES: Final = 30
#: And then how often, until it is on (s).
RESTORE_SLOW_RETRY_S: Final = 600.0
#: After this many setting backs not confirmed in a row the person is told in Repairs (they can turn it on themselves).
ISSUE_AFTER_TRIES: Final = 3
#: A measurement from which a charger counts as drawing (A on a phase, W).
DRAWING_A: Final = 1.0
DRAWING_W: Final = 230.0
STORE_VERSION: Final = 1
#: The `ocpp` integration's station availability switch: its unique id's last part (the per-connector one is
#: `connnector_availability`, the integration's own spelling, and is not used: the whole charge point is taken out).
AVAILABILITY_KEY: Final = "availability"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.charger_test_hold.{entry_id}"


#: The OCPP status of a connector of a charge point out of service (`ChargePointStatus.unavailable`; not Home
#: Assistant's own lower-case `unavailable`, an entity it cannot read).
OCPP_UNAVAILABLE: Final = "Unavailable"


def connector_out_of_service(hass: Any, controller: Any) -> bool:
    """Whether the OCPP charger's connector reports itself `Unavailable` (its status sensor): the charge point's own
    word that it took `Inoperative`. The availability switch alone is no proof: the integration answers `Scheduled`
    (a transaction runs: out of service only once it ends) as taken, and on a charge point that reports no station
    status the switch reads off for a connector merely `Preparing`. Never raises."""
    from ..vehicles.ocpp_identity import STATUS_CONNECTOR_KEY, connector_sensor_entity_id  # noqa: PLC0415

    try:
        target = getattr(controller, "ocpp_target", None)
        if target is None:
            return False
        state = hass.states.get(connector_sensor_entity_id(hass, target, STATUS_CONNECTOR_KEY))
        return state is not None and state.state == OCPP_UNAVAILABLE
    except Exception:  # noqa: BLE001 - not known: not out of service
        _LOGGER.debug("connector_out_of_service: ignored failure", exc_info=True)
        return False


def station_availability_entity(hass: Any, controller: Any) -> str | None:
    """The `ocpp` integration's station availability switch of an OCPP charger (unique id
    `switch.ocpp.<cpid>.availability`, by its charge point id), `None` for any other charger or when it is not there.
    Never raises."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415 - kept beside its only use

    try:
        target = getattr(controller, "ocpp_target", None)
        if target is None:
            return None
        return er.async_get(hass).async_get_entity_id(
            "switch", "ocpp", f"switch.ocpp.{target.charge_point_id}.{AVAILABILITY_KEY}"
        )
    except Exception:  # noqa: BLE001 - not found: the charger stays counted
        _LOGGER.debug("station_availability_entity: ignored failure", exc_info=True)
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
        #: The availability switches taken out of service, by entity id (one charge point's switch is shared by every
        #: connector's charger, so it is kept, and set back, once), kept in the store until set back; each charger's
        #: switch; and the chargers whose switch read off under this hold.
        self._parked: dict[str, dict[str, Any]] = {}
        self._switch_of: dict[str, str] = {}
        self._out: set[str] = set()
        self._park_task: asyncio.Task[None] | None = None
        self._restore_lock = asyncio.Lock()
        self._retry_cancel: Callable[[], None] | None = None
        self._tries = 0
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

    def _drawing(self, charger_entry_id: str, controller: Any) -> bool:
        """Whether a measurement says the charger draws now (`DRAWING_A` on a phase, or `DRAWING_W`): its own measured
        current, its power sensor, or the site's measured current for it, whichever it has. A profile without a
        status sensor says it charges only for its start option (a zappi charging in Eco reads neither charging nor
        on), so this is what shows such a charge. Never raises; nothing read is not drawing."""
        values: list[float] = []
        with contextlib.suppress(Exception):
            amps = controller.measured_current_a()
            if amps is not None:
                values.append(float(amps))
        with contextlib.suppress(Exception):
            watts = controller.measured_power_w()
            if watts is not None and float(watts) >= DRAWING_W:
                return True
        with contextlib.suppress(Exception):
            measured = self._site.charger_measured_current(charger_entry_id)
            for value in () if measured is None else (measured.get(phase) for phase in ("L1", "L2", "L3")):
                if value is not None and value.value is not None and value.problem is None:
                    values.append(float(value.value))
        return any(value >= DRAWING_A for value in values)

    def charging_now(self) -> list[str]:
        """The site's chargers that are on now: charging, their charge control on (a Start in effect), or drawing by a
        measurement (`_drawing`). A charger that cannot be read is not on by this (it stays counted in the cap)."""
        found: list[str] = []
        for charger_entry_id in self._charger_ids():
            controller = self._controller(charger_entry_id)
            if controller is None:
                continue
            try:
                if self._on(controller) or self._drawing(charger_entry_id, controller):
                    found.append(charger_entry_id)
            except Exception:  # noqa: BLE001 - not known: not taken as on, and counted in the cap
                _LOGGER.debug("charging_now: ignored failure", exc_info=True)
        return found

    def counts_nothing(self, charger_entry_id: str) -> bool:
        """Whether the HA-down grid charge cap may count this charger at 0 A now (see the module): the hold on, and
        either an OCPP charger out of service by its own report and not charging, or a charger read stopped
        with no Start of ours on its way whose stop it keeps without Home Assistant. `False` whenever any of it cannot
        be read."""
        if not self.holds(charger_entry_id):
            return False
        controller = self._controller(charger_entry_id)
        if controller is None:
            return False
        if charger_entry_id in self._out:
            # An OCPP charger taken out of service: its switch reads off, its connector reports itself `Unavailable`,
            # and it neither charges nor draws now (see the module).
            entity_id = self._switch_of.get(charger_entry_id)
            state = None if entity_id is None else self._site.hass.states.get(entity_id)
            try:
                return (
                    state is not None
                    and state.state == "off"
                    and connector_out_of_service(self._site.hass, controller)
                    and not controller.charging
                    and not self._drawing(charger_entry_id, controller)
                )
            except Exception:  # noqa: BLE001 - not known: counted
                return False
        try:
            # The stop option itself read back (a mode the charger keeps, not a schedule or an eco mode of its own that
            # may begin a charge), none of its own modes on, no Start of ours on its way, and nothing drawing.
            stopped = (
                controller.control_confirmed_off
                and controller.adapter.enabled_state() is False
                and not controller.charger_conflicts
                and not controller.start_pending
                and not self._drawing(charger_entry_id, controller)
            )
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
        self._out = set()
        # No stop is written here: the test does not start while a charger is on (`charging_now`); the test's polls
        # stop one that begins later (`observe`).
        _LOGGER.info("SpotNav site %s: the chargers are held stopped for the hardware test", self._site.entry_id)
        if self._store is not None and any(
            station_availability_entity(self._site.hass, self._controller(charger_entry_id))
            for charger_entry_id in self._charger_ids()
        ):
            self._park_task = self._site.hass.async_create_task(self._async_take_out_of_service())

    def release(self) -> list[str]:
        """End the hold at once; the chargers whose start it refused (for `async_rearm_refused`). Never raises."""
        was_on = self._until is not None
        self._until = None
        refused, self._refused = sorted(self._refused), set()
        self._stops = {}
        self._out = set()
        self._switch_of = {
            charger: entity for charger, entity in self._switch_of.items() if entity in self._parked
        }
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

    # ---- an OCPP charger taken out of service

    async def async_load(self) -> None:
        """The OCPP chargers a test left out of service when Home Assistant stopped in its middle: set back to
        operative from now on, in the background. Never raises."""
        if self._store is None:
            return
        try:
            raw = await self._store.async_load()
        except Exception:  # noqa: BLE001 - an unreadable store holds nothing to set back
            _LOGGER.warning("SpotNav site %s: the hardware test's kept chargers could not be read", self._site.entry_id)
            return
        kept = (raw or {}).get("unavailable") if isinstance(raw, dict) else None
        if not isinstance(kept, dict):
            return
        for entity_id, row in kept.items():
            if isinstance(entity_id, str) and entity_id.startswith("switch.") and isinstance(row, dict):
                chargers = row.get("chargers")
                self._parked[entity_id] = {"chargers": [str(item) for item in chargers] if isinstance(chargers, list)
                                           else []}
        if self._parked:
            _LOGGER.warning(
                "SpotNav site %s: a hardware test was cut short with an OCPP charger out of service; it is set back "
                "to operative now", self._site.entry_id,
            )
            self._site.hass.async_create_task(self.async_restore_availability())

    async def _async_save(self) -> None:
        if self._store is not None:
            await self._store.async_save({"unavailable": {key: dict(row) for key, row in self._parked.items()}})

    async def _async_switch(self, entity_id: str, on: bool, confirmed: Callable[[], bool] | None = None) -> bool:
        """Turn the availability switch on or off and wait up to `CONFIRM_S` for it to read so and for `confirmed` (the
        charge point's own report); whether they do. A call that fails raises."""
        hass = self._site.hass
        async with asyncio.timeout(CALL_S):
            await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": entity_id},
                                           blocking=True)
        wanted = "on" if on else "off"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_S
        while True:
            state = hass.states.get(entity_id)
            if state is not None and state.state == wanted and (confirmed is None or confirmed()):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(1.0)

    async def _async_take_out_of_service(self) -> None:
        """Each OCPP charger whose availability switch reads on taken out of service (see the module): kept in the
        store first, then the command; counted at 0 A only once its switch reads off. Never raises."""
        hass = self._site.hass
        for charger_entry_id in self._charger_ids():
            if not self.active or self._closed:
                return
            entity_id = station_availability_entity(hass, self._controller(charger_entry_id))
            state = None if entity_id is None else hass.states.get(entity_id)
            if entity_id is not None and entity_id in self._parked:
                # Another connector of the same charge point: taken out with it, and set back once.
                if charger_entry_id not in self._parked[entity_id]["chargers"]:
                    self._parked[entity_id]["chargers"].append(charger_entry_id)
                self._switch_of[charger_entry_id] = entity_id
                if any(self._switch_of.get(other) == entity_id for other in self._out):
                    self._out.add(charger_entry_id)
                with contextlib.suppress(Exception):
                    await self._async_save()
                continue
            if entity_id is None or state is None or state.state != "on":
                # Not there, not reachable, or already out of service by someone else's word: left as it is.
                continue
            self._parked[entity_id] = {"chargers": [charger_entry_id]}
            self._switch_of[charger_entry_id] = entity_id
            try:
                # Kept before the command goes out: Home Assistant stopping right after it must still set it back.
                await self._async_save()
                controller = self._controller(charger_entry_id)
                confirmed = await self._async_switch(
                    entity_id, on=False, confirmed=lambda c=controller: connector_out_of_service(hass, c)
                )
            except Exception as err:  # noqa: BLE001 - not taken: the charger stays counted, and is set back anyway
                _LOGGER.warning(
                    "SpotNav site %s: OCPP charger %s could not be taken out of service for the hardware test (%s); "
                    "it stays counted at its current", self._site.entry_id, charger_entry_id, err,
                )
                continue
            if not confirmed:
                _LOGGER.warning(
                    "SpotNav site %s: OCPP charger %s did not report itself unavailable within %s s; it stays counted "
                    "at its current", self._site.entry_id, charger_entry_id, int(CONFIRM_S),
                )
            elif self.active:
                self._out.add(charger_entry_id)
                _LOGGER.info(
                    "SpotNav site %s: OCPP charger %s is out of service for the hardware test", self._site.entry_id,
                    charger_entry_id,
                )

    async def async_restore_availability(self) -> None:
        """Every OCPP charger this hold (or one cut short by a restart) took out of service set back to operative,
        confirmed by its switch reading on (one that reads on already is left as it is); one not confirmed is tried
        again (`RESTORE_RETRY_S`, later `RESTORE_SLOW_RETRY_S`), told in Repairs from `ISSUE_AFTER_TRIES`, and stays
        in the store meanwhile. Never raises."""
        if not self.active:
            # A release: the taking out still on its way is ended first. A retry while a new test holds the chargers
            # leaves that test's taking out alone (it sets back only what that test does not hold).
            task, self._park_task = self._park_task, None
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        async with self._restore_lock:
            failed = False
            for entity_id, row in list(self._parked.items()):
                charger_entry_id = ", ".join(row.get("chargers") or []) or entity_id
                if self.active and any(item in self._out for item in row.get("chargers") or []):
                    continue  # a new hold holds it again
                state = self._site.hass.states.get(entity_id)
                if state is not None and state.state == "on":
                    # On already: a person (or the charge point) set it back meanwhile. Nothing is written over it.
                    self._parked.pop(entity_id, None)
                    continue
                try:
                    if state is None or state.state == "unavailable":
                        raise RuntimeError("the charge point is not connected")
                    if not await self._async_switch(entity_id, on=True):
                        raise RuntimeError("it did not report itself available")
                except Exception as err:  # noqa: BLE001 - tried again; kept in the store meanwhile
                    failed = True
                    _LOGGER.warning(
                        "SpotNav site %s: OCPP charger %s could not be set back to operative after the hardware test "
                        "(%s)", self._site.entry_id, charger_entry_id, err,
                    )
                    continue
                self._parked.pop(entity_id, None)
                _LOGGER.info("SpotNav site %s: OCPP charger %s is operative again", self._site.entry_id, charger_entry_id)
            try:
                await self._async_save()
            except Exception:  # noqa: BLE001 - the next write saves it
                _LOGGER.debug("Saving the hardware test's kept chargers failed", exc_info=True)
            if not failed:
                self._tries = 0
            elif not self._closed and self._retry_cancel is None:
                self._tries += 1
                if self._tries >= ISSUE_AFTER_TRIES:
                    self._raise_issue()
                if self._tries == RESTORE_TRIES:
                    _LOGGER.error(
                        "SpotNav site %s: %s still out of service after the hardware test; turn it on yourself (tried "
                        "again every %s s from now)", self._site.entry_id, ", ".join(sorted(self._parked)),
                        int(RESTORE_SLOW_RETRY_S),
                    )
                delay = RESTORE_RETRY_S if self._tries < RESTORE_TRIES else RESTORE_SLOW_RETRY_S
                self._retry_cancel = async_call_later(self._site.hass, delay, self._retry)

            if not self._parked:
                self._clear_issue()

    def _issue_id(self) -> str:
        return f"{HARDWARE_TEST_OUT_OF_SERVICE_ISSUE}_{self._site.entry_id}"

    def _raise_issue(self) -> None:
        """Tell the person in Repairs: the switches are theirs to turn on (actionable), until they are on."""
        from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415 - kept beside its only use

        with contextlib.suppress(Exception):
            ir.async_create_issue(
                self._site.hass, DOMAIN, self._issue_id(), is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=HARDWARE_TEST_OUT_OF_SERVICE_ISSUE,
                translation_placeholders={"switches": ", ".join(sorted(self._parked))},
            )

    def _clear_issue(self) -> None:
        from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415 - kept beside its only use

        with contextlib.suppress(Exception):
            ir.async_delete_issue(self._site.hass, DOMAIN, self._issue_id())

    @callback
    def _retry(self, _now: datetime) -> None:
        self._retry_cancel = None
        if not self._closed:
            self._site.hass.async_create_task(self.async_restore_availability())

    @property
    def unavailable_kept(self) -> list[str]:
        """The availability switches still to be set back to operative."""
        return sorted(self._parked)

    async def async_close(self) -> None:
        """The site unloads: no more retries; what is still to be set back stays in the store for the next start."""
        self._closed = True
        cancel, self._retry_cancel = self._retry_cancel, None
        if cancel is not None:
            cancel()
        task, self._park_task = self._park_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @staticmethod
    async def async_remove_stored(hass: Any, entry_id: str) -> None:
        """The site's entry is removed: each availability switch still kept is set back to on first, once; the record
        goes only when every one is on (else it stays, with a warning: the person turns it on, or a site set up again
        with this entry id sets it back). Never raises."""
        store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(entry_id))
        try:
            raw = await store.async_load()
        except Exception:  # noqa: BLE001 - unreadable: nothing to set back
            raw = None
        kept = (raw or {}).get("unavailable") if isinstance(raw, dict) else None
        left: list[str] = []
        for entity_id in kept if isinstance(kept, dict) else {}:
            state = hass.states.get(entity_id)
            if state is not None and state.state == "on":
                continue
            try:
                async with asyncio.timeout(CALL_S):
                    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
            except Exception:  # noqa: BLE001 - left in the record
                _LOGGER.debug("Setting %s back at the removal failed", entity_id, exc_info=True)
            state = hass.states.get(entity_id)
            if state is None or state.state != "on":
                left.append(entity_id)
        if left:
            _LOGGER.warning(
                "SpotNav: %s could not be set back to available as its site was removed; turn it on yourself",
                ", ".join(left),
            )
            return
        with contextlib.suppress(Exception):
            await store.async_remove()

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
            "ocpp_out_of_service": sorted(self._out),
            "ocpp_to_set_back": self.unavailable_kept,
            "stops": {charger_entry_id: count for charger_entry_id, (count, _last) in self._stops.items()},
        }
