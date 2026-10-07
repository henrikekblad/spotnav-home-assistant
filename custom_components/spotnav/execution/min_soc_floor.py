"""The car's minimum charge level: a car is never left below the floor set for it, whatever the price or the sun.

* `effective_floor`, `known_soc` and `decide_floor` are pure: the whole policy lives there.
* `MinSocFloor` is the shell, one per charger: it reads the car the charger plans for and the charger, decides, and
  acts through the execution boundary (`AutoExecutor.async_min_soc_start`/`async_min_soc_end`), which feeds the
  charge-ownership core (`core/ownership.py`, `min_soc_start`/`min_soc_end`). It sends no command of its own.

The rules:

* The floor is the car's own (`vehicle_properties.KEY_MIN`), read from the car the charger plans for, so it
  follows the car to whichever charger it is at. It is capped at the car's target (under a target) and at the
  car's own charge limit: SpotNav never charges past either for the floor.
* While the car's known state of charge is below it, the charge starts at once at the full current set (the
  person's amps, capped by load balancing as any start), under every strategy: the plan's windows and the sun do
  not hold it back, and a charge the plan or the sun runs is taken over. A person's pause or Stop wins (they
  decided), and a charge a person started stays theirs.
* Known: a fresh reading, an estimate carried from delivered energy (`soc_estimate`), or a reading taken while
  the car has been plugged in (it cannot have been driven since, and nothing was delivered since, or it would be
  an estimate). With none of these the floor does not apply.
* At the floor (floor + 0) the strategy decides from there: a plan window open now takes the charge over, the sun
  takes it (solar, hybrid) and decides it on its next reading, else it is stopped once. A floor charge that
  ended at the floor starts again in the same plug-in only `FLOOR_RESTART_MARGIN_PERCENT` below it, so a reading's
  wobble never cycles the charger; one that ended short of the floor (the car or the charger ended it) is tried
  again after `FLOOR_RETRY_S`.
* It is decided again whenever the charger's state changes, the plan is calculated again (a new reading) and at
  least every `EVALUATE_INTERVAL`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ..planning.auto_settings import AutoSettingsStore
from .target_stop import charge_ceiling_percent, SOC_FRESH_MAX_AGE_S

_LOGGER = logging.getLogger(__name__)

#: The charge origin of a floor charge (`ChargingController.charge_origin`), and the core's owner of the same name.
ORIGIN_MIN_SOC: Final = "min_soc"

FLOOR_START: Final = "start"
FLOOR_END: Final = "end"
FLOOR_NONE: Final = "none"

#: A floor charge that ended at the floor starts again in the same plug-in only this far below it.
FLOOR_RESTART_MARGIN_PERCENT: Final = 2.0
#: A floor charge that ended short of the floor (the car or the charger ended it) is tried again after this long.
FLOOR_RETRY_S: Final = 300.0
#: How often the floor is decided again with nothing else changing.
EVALUATE_INTERVAL: Final = timedelta(seconds=30)


def effective_floor(
    min_percent: float | None, *, target_percent: float | None, vehicle_max_percent: float | None
) -> float | None:
    """The floor that applies: the car's own, no higher than its target or its own charge limit; `None` when off."""
    if min_percent is None:
        return None
    floor = float(min_percent)
    if target_percent is not None:
        floor = min(floor, float(target_percent))
    floor = min(floor, charge_ceiling_percent(vehicle_max_percent))
    return floor if floor > 0 else None


def known_soc(
    *,
    soc_percent: float | None,
    estimated: bool,
    age_s: float | None,
    plugged_in_at: datetime | None,
    now: datetime,
) -> float | None:
    """The state of charge the floor decides on, or `None` when it is not known well enough."""
    if soc_percent is None:
        return None
    if estimated:
        return soc_percent
    if age_s is None:
        return None
    if age_s <= SOC_FRESH_MAX_AGE_S:
        return soc_percent
    if plugged_in_at is not None and now - timedelta(seconds=age_s) >= plugged_in_at:
        return soc_percent
    return None


@dataclass(frozen=True, slots=True)
class FloorFacts:
    """What one decision reads."""

    floor_percent: float | None
    soc_percent: float | None
    connected: bool | None
    #: A person's pause or Stop (or Start) holds Auto (`pause_blocks_execution`).
    paused: bool
    #: Who started the running charge (`ChargingController.charge_origin`).
    origin: str | None
    #: Load balancing holds a floor charge back already: its regulator gives it back.
    balancing_holds_floor: bool = False
    #: A floor charge ended at the floor in this plug-in.
    ended_at_floor: bool = False
    #: A floor charge ended short of the floor less than `FLOOR_RETRY_S` ago.
    retry_wait: bool = False


def decide_floor(facts: FloorFacts) -> str:
    """`FLOOR_START`, `FLOOR_END` (the floor's own charge is over) or `FLOOR_NONE`."""
    floor, soc = facts.floor_percent, facts.soc_percent
    below = floor is not None and soc is not None and soc < floor
    if facts.origin == ORIGIN_MIN_SOC:
        if facts.paused or not below or facts.connected is False:
            return FLOOR_END
        return FLOOR_NONE
    if not below or facts.paused or facts.connected is False:
        return FLOOR_NONE
    assert floor is not None and soc is not None
    if facts.ended_at_floor and soc >= floor - FLOOR_RESTART_MARGIN_PERCENT:
        return FLOOR_NONE
    if facts.origin == "manual" or facts.balancing_holds_floor or facts.retry_wait:
        return FLOOR_NONE
    return FLOOR_START


@dataclass(frozen=True, slots=True)
class FloorInputs:
    """What the car the charger plans for says now: its minimum charge level, the target the charger plans to (under
    a target), its own charge limit, and its state of charge as the planner reads it."""

    min_percent: float | None
    target_percent: float | None
    vehicle_max_percent: float | None
    soc_percent: float | None
    soc_estimated: bool = False
    soc_age_s: float | None = None


class MinSocFloor:
    """One charger's floor. `inputs` reads the car; `interval=None` arms no timer (a test decides when)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        executor: Any,
        store: AutoSettingsStore,
        inputs: Callable[[], FloorInputs | None],
        *,
        interval: timedelta | None = EVALUATE_INTERVAL,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._executor = executor
        self._store = store
        self._inputs = inputs
        self._interval = interval
        self._now: Callable[[], datetime] = dt_util.utcnow
        self._unsubs: list[Callable[[], None]] = []
        self._evaluating = False
        self._again = False
        self._stopped = False
        # The plug-in a floor charge ended at the floor in, and when one last went out (`FLOOR_RETRY_S`).
        self._ended_at_floor_in: datetime | None = None
        self._started_at: datetime | None = None
        self._floor: float | None = None

    @property
    def charging_percent(self) -> float | None:
        """The floor while the floor's own charge runs (the status line), else `None`."""
        controller = self._executor.controller
        if controller.charge_origin != ORIGIN_MIN_SOC or not controller.charge_control_on:
            return None
        return self._floor

    @callback
    def async_start(self) -> None:
        """Listen to the charger and arm the interval."""
        controller = self._executor.controller
        self._unsubs.append(controller.add_listener(self.async_poke))
        if self._interval is not None:
            self._unsubs.append(
                async_track_time_interval(self._hass, self.async_poke, self._interval)
            )

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        while self._unsubs:
            self._unsubs.pop()()

    @callback
    def async_poke(self, *_args: Any) -> None:
        """Decide again soon (once, however many changes came meanwhile)."""
        if self._stopped:
            return
        if self._evaluating:
            self._again = True
            return
        self._hass.async_create_background_task(self.async_evaluate(), f"{self._entry_id} minimum charge level")

    def _facts(self) -> FloorFacts | None:
        controller = self._executor.controller
        if not controller.restored:
            return None
        inputs = self._inputs()
        settings = self._store.settings(self._entry_id)
        now = self._now()
        plugged_in_at = controller.plugged_in_for_count
        if inputs is None:
            floor, soc = None, None
        else:
            floor = effective_floor(
                inputs.min_percent,
                target_percent=inputs.target_percent,
                vehicle_max_percent=inputs.vehicle_max_percent,
            )
            soc = known_soc(
                soc_percent=inputs.soc_percent,
                estimated=inputs.soc_estimated,
                age_s=inputs.soc_age_s,
                plugged_in_at=plugged_in_at,
                now=now,
            )
        from .auto_execution import pause_blocks_execution  # noqa: PLC0415 - the boundary imports this module's users

        started = self._started_at
        return FloorFacts(
            floor_percent=floor,
            soc_percent=soc,
            connected=controller.adapter.vehicle_connected(),
            paused=pause_blocks_execution(settings),
            origin=controller.charge_origin,
            balancing_holds_floor=controller.balancing_holds(ORIGIN_MIN_SOC),
            ended_at_floor=self._ended_at_floor_in is not None and self._ended_at_floor_in == plugged_in_at,
            # Only while nothing charges: a charge the plan or the sun took over meanwhile is taken back at once.
            retry_wait=started is not None
            and not controller.charge_control_on
            and (now - started).total_seconds() < FLOOR_RETRY_S,
        )

    async def async_evaluate(self) -> None:
        """Decide once, and act through the execution boundary. Never raises."""
        if self._evaluating:
            self._again = True
            return
        self._evaluating = True
        try:
            while True:
                self._again = False
                await self._evaluate_once()
                if not self._again or self._stopped:
                    break
        except Exception as err:  # noqa: BLE001 - the next change or interval decides again
            _LOGGER.warning("SpotNav charger %s: the minimum charge level failed: %s", self._entry_id, type(err).__name__)
        finally:
            self._evaluating = False

    async def _evaluate_once(self) -> None:
        facts = self._facts()
        if facts is None:
            return
        self._floor = facts.floor_percent
        decision = decide_floor(facts)
        if decision == FLOOR_START:
            settings = self._store.settings(self._entry_id)
            _LOGGER.info(
                "SpotNav charger %s: the car is at %.0f %%, below its minimum charge level of %.0f %%; charging now",
                self._entry_id, facts.soc_percent or 0.0, facts.floor_percent or 0.0,
            )
            if await self._executor.async_min_soc_start(settings.amps):
                self._started_at = self._now()
        elif decision == FLOOR_END:
            at_floor = (
                not facts.paused
                and facts.floor_percent is not None
                and facts.soc_percent is not None
                and facts.soc_percent >= facts.floor_percent
            )
            if await self._executor.async_min_soc_end() and at_floor:
                self._ended_at_floor_in = self._executor.controller.plugged_in_for_count
                self._started_at = None
