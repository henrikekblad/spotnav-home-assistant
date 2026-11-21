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

* the hold is on;
* the charger reads stopped now: its charge control readable and off, its status not charging, and no Start of
  ours on its way (`ChargingController.control_confirmed_off`, `start_pending`);
* its stop is a setting the charger keeps by itself and that holds every later session off as well
  (`PlatformProfile.stop_kept_without_ha`, on the profile's own start/stop path, not an OCPP charger). With Home
  Assistant gone nobody writes it back, so the charger stays stopped whatever the car does.

Every other control kind keeps its current counted, because there the stop ends or pauses the running session
only and the charger can begin a charge again by itself without Home Assistant: OCPP (a remote stop ends the
transaction; a free-charging charge point, or one that authorizes offline, starts the next one at the plug-in),
Easee (its pause service; a charger without authorization charges at the plug-in), start/stop buttons (a session
command; the next plug-in starts a new session), pause switches and the 0 A pause (the session's own state), go-e's
force state (back to neutral at the unplug). A charger that begins charging during the test is counted again at
once, at the next look, whatever kind it is (it no longer reads stopped).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.util import dt as dt_util

from ..const import CONF_CHARGER_ENTRY_IDS

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: The least time between two stops of one charger under the hold (s), and the most stops of one charger in a test.
STOP_GAP_S: Final = 30.0
MAX_STOPS: Final = 3


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
        """Whether the HA-down grid charge cap may count this charger at 0 A now (see the module): the hold on, the
        charger read stopped with no Start of ours on its way, and its stop one it keeps without Home Assistant.
        `False` whenever any of it cannot be read."""
        if not self.holds(charger_entry_id):
            return False
        controller = self._controller(charger_entry_id)
        if controller is None:
            return False
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
        _LOGGER.info("SpotNav site %s: the chargers are held stopped for the hardware test", self._site.entry_id)
        self.observe()

    def release(self) -> list[str]:
        """End the hold at once; the chargers whose start it refused (for `async_rearm_refused`). Never raises."""
        was_on = self._until is not None
        self._until = None
        refused, self._refused = sorted(self._refused), set()
        self._stops = {}
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
            "stops": {charger_entry_id: count for charger_entry_id, (count, _last) in self._stops.items()},
        }
