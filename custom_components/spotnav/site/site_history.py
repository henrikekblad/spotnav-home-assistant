"""A site's last 24 hours, one row a minute, for the debug bundle (`history_24h`).

Cheap: each row reads what the site controller and its member chargers already hold (no recorder query, no service
call). The site's row is the grid's total power and where it came from, the site's current per phase and the
battery's power; each member charger has a row of its own at the same minute (`CHARGER_COLUMNS`): its measured
current per phase, then what the charger's controller holds (`CONTROLLER_COLUMNS`): charge control, charging,
connection, connector status, the car's state of charge and where it came from, the strategy, what solar decided
(state, action, reason), the phases the charger reads back and whether we switched it to one, the current last
written and the current asked for. No PV power: a site knows no PV sensor, so there is none to state. A charger in
no site keeps the same row from its controller alone (`debug_day.ChargerDay`, `CHARGER_ALONE_COLUMNS`).

The import target's columns come last (`TARGET_SITE_COLUMNS`, `TARGET_CHARGER_COLUMNS`): on the site's row the level
the hour is held under (kW, with its margin), what the chargers may draw (W), the house's power the budget used (W)
and why the target is not applied (empty while it is, or while no target is set); on a charger's row what the
target does to it (`free`, `held`, `paused`, `released`, `exempt`) and the amps it caps it at. Rows kept from before
these columns end earlier: a reader takes a missing value as empty.

The import target's change log (`SiteCapacityController._note_target`, `note_target`) is kept in the same store:
what it did to each charger, when it was not applied and why, the departure notice told, told again and cleared, and
every release with its source and its end. Entries older than `HISTORY_S` are dropped, and at most
`TARGET_LOG_KEPT`.

Rows are compact text (`debug_day.format_row`), kept across restarts in the site's own store (`debug_day.DayStore`,
written at most every five minutes) and dropped after 24 hours. Never raises; a charger that cannot be read is a row
of its time and `error=<kind>`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import CONF_CHARGER_ENTRY_IDS
from ..debug_day import (
    DayStore,
    format_row,
    HISTORY_S,
    history_block,
    prune_rows,
    row_time,
    rows_from_store,
    SAMPLE_COUNT,
    SAMPLE_INTERVAL_S,
    store_key,
)
from .site_capacity import PHASES

if TYPE_CHECKING:
    from .site_capacity_controller import SiteCapacityController

__all__ = ["SAMPLE_COUNT", "SAMPLE_INTERVAL_S", "SiteHistory"]

_LOGGER = logging.getLogger(__name__)

#: The import target's columns on the site's row, after its own, and on a member charger's row.
TARGET_SITE_COLUMNS: Final = ("target_hold_kw", "target_allowed_w", "target_house_w", "target_not_applied")
TARGET_CHARGER_COLUMNS: Final = ("target_action", "target_cap_a")
#: The site's row.
SITE_COLUMNS: Final = ("time", "grid_w", "grid_source", "L1_a", "L2_a", "L3_a", "battery_w", *TARGET_SITE_COLUMNS)
#: The import target's change log: entries kept, and the keys one may have.
TARGET_LOG_KEPT: Final = 500
_TARGET_LOG_MAX_KEYS: Final = 24
#: What a charger's controller holds, in every charger row after its current.
CONTROLLER_COLUMNS: Final = (
    "control",
    "charging",
    "connection",
    "connector",
    "soc",
    "soc_source",
    "strategy",
    "solar_state",
    "solar_action",
    "solar_reason",
    "phases_read",
    "phases_switched",
    "setpoint_a",
    "requested_a",
)
#: A member charger's row in its site's history.
CHARGER_COLUMNS: Final = ("time", "L1_a", "L2_a", "L3_a", *CONTROLLER_COLUMNS, *TARGET_CHARGER_COLUMNS)
#: A charger in no site: the current its controller measures (its highest phase) instead of the site's per phase.
CHARGER_ALONE_COLUMNS: Final = ("time", "current_a", *CONTROLLER_COLUMNS)


def _rounded(value: Any, digits: int = 2) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), digits)


class SiteHistory:
    """The site's rows and its member chargers' rows of the last 24 hours, oldest first."""

    def __init__(self, hass: HomeAssistant | None = None, entry_id: str | None = None) -> None:
        self._rows: deque[str] = deque(maxlen=SAMPLE_COUNT)
        self._chargers: dict[str, deque[str]] = {}
        self._target_log: deque[dict[str, Any]] = deque(maxlen=TARGET_LOG_KEPT)
        self._store = (
            None if hass is None or entry_id is None else DayStore(hass, store_key(entry_id), self._to_store)
        )

    def _to_store(self) -> dict[str, Any]:
        return {
            "rows": list(self._rows),
            "chargers": {key: list(rows) for key, rows in self._chargers.items()},
            "import_target_log": list(self._target_log),
        }

    async def async_load(self) -> None:
        """Read the rows kept before the restart; the ones taken meanwhile go after them."""
        if self._store is None:
            return
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        try:
            self._restore(stored)
        except Exception:  # noqa: BLE001 - what was kept is a diagnostic: a site never fails to start on it
            _LOGGER.debug("SpotNav site history: the kept rows could not be read", exc_info=True)

    def _restore(self, stored: dict[str, Any]) -> None:
        now = dt_util.utcnow()
        rows = rows_from_store(stored.get("rows"), now)
        chargers = stored.get("chargers")
        restored: dict[str, Any] = {}
        for charger_entry_id, raw in (chargers.items() if isinstance(chargers, dict) else ()):
            if not isinstance(charger_entry_id, str):
                continue
            kept = rows_from_store(raw, now)
            kept.extend(self._chargers.get(charger_entry_id, ()))
            if kept:
                restored[charger_entry_id] = kept
        rows.extend(self._rows)
        self._rows = rows
        self._chargers.update(restored)
        kept_log = _target_log_from_store(stored.get("import_target_log"), now)
        kept_log.extend(self._target_log)
        self._target_log = deque(kept_log, maxlen=TARGET_LOG_KEPT)

    @property
    def target_log(self) -> list[dict[str, Any]]:
        """The import target's change log of the last 24 hours, oldest first."""
        _prune_target_log(self._target_log, dt_util.utcnow())
        return list(self._target_log)

    def note_target(self, entry: dict[str, Any]) -> None:
        """Keep one entry of the import target's change log (it carries its `at`, ISO); the oldest beyond the day,
        or beyond `TARGET_LOG_KEPT`, are dropped."""
        self._target_log.append(entry)
        _prune_target_log(self._target_log, dt_util.utcnow())
        if self._store is not None:
            self._store.changed()

    async def async_close(self) -> None:
        """The site's shutdown: write what waits, then nothing more."""
        if self._store is not None:
            await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget its rows."""
        await DayStore.async_remove(hass, store_key(entry_id))

    @property
    def samples(self) -> dict[str, Any]:
        """The bundle's `history_24h`: the site's table, and each charger's at the same minutes."""
        block = history_block(SITE_COLUMNS, self._rows)
        block["chargers"] = {
            charger_entry_id: history_block(CHARGER_COLUMNS, rows)
            for charger_entry_id, rows in self._chargers.items()
        }
        return block

    def sample(self, site: SiteCapacityController) -> None:
        """Take one row of `site` now. A failure is logged and skipped, never raised."""
        try:
            now = dt_util.utcnow()
            time = row_time(now)
            self._rows.append(format_row([time, *site_values(site)]))
            prune_rows(self._rows, now)
            for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                rows = self._chargers.setdefault(charger_entry_id, deque(maxlen=SAMPLE_COUNT))
                try:
                    rows.append(format_row([time, *_member_values(site, charger_entry_id)]))
                except Exception as err:  # noqa: BLE001 - one charger must not cost the row
                    rows.append(format_row([time, f"error={type(err).__name__}"]))
            for charger_entry_id, rows in list(self._chargers.items()):
                prune_rows(rows, now)
                if not rows:
                    del self._chargers[charger_entry_id]
            if self._store is not None:
                self._store.changed()
        except Exception:  # noqa: BLE001 - a diagnostic sample must never disturb the site
            _LOGGER.debug("Could not sample the site's history", exc_info=True)


def _entry_moment(entry: Any) -> datetime | None:
    if not isinstance(entry, dict):
        return None
    at = entry.get("at")
    moment = dt_util.parse_datetime(at) if isinstance(at, str) else None
    if moment is None or moment.tzinfo is None:
        return None
    return moment


def _prune_target_log(log: deque[dict[str, Any]], now: datetime) -> None:
    oldest = now - timedelta(seconds=HISTORY_S)
    while log:
        moment = _entry_moment(log[0])
        if moment is not None and moment >= oldest:
            return
        log.popleft()


def _target_log_from_store(raw: Any, now: datetime) -> list[dict[str, Any]]:
    """Stored entries, read as untrusted: flat ones of the last `HISTORY_S` with plain values, oldest first."""
    oldest = now - timedelta(seconds=HISTORY_S)
    kept: list[tuple[datetime, dict[str, Any]]] = []
    for entry in raw if isinstance(raw, list) else ():
        moment = _entry_moment(entry)
        if moment is None or not oldest <= moment <= now + timedelta(minutes=1):
            continue
        if len(entry) > _TARGET_LOG_MAX_KEYS or not all(
            isinstance(key, str) and (value is None or isinstance(value, (str, int, float, bool)))
            for key, value in entry.items()
        ):
            continue
        kept.append((moment, dict(entry)))
    kept.sort(key=lambda item: item[0])
    return [entry for _moment, entry in kept[-TARGET_LOG_KEPT:]]


def _grid(site: SiteCapacityController) -> tuple[float | None, str | None]:
    """The grid's total power (W, positive = import) and where it came from: the meter's total (`total`),
    else the sum of the three phases' signed active power (`phases`), else unknown."""
    total = site.grid_total_power()
    if total is not None:
        return _rounded(total.value, 1), "total"
    phases = site.result.phase_signed_active_power_w
    values = [phases.get(phase) for phase in PHASES]
    if values and all(isinstance(value, (int, float)) for value in values):
        return _rounded(sum(values), 1), "phases"  # type: ignore[arg-type]
    return None, None


def site_values(site: SiteCapacityController) -> list[Any]:
    """The site's row after its time (`SITE_COLUMNS`)."""
    grid_w, grid_source = _grid(site)
    battery = site.battery_aggregate_power()
    current = site.result.measured_phase_current_a
    return [
        grid_w,
        grid_source,
        *(_rounded(current.get(phase)) for phase in PHASES),
        None if battery is None else _rounded(battery.value, 1),
        *site.import_target_row(),
    ]


def _member_values(site: SiteCapacityController, charger_entry_id: str) -> list[Any]:
    measured = site.charger_measured_current(charger_entry_id)
    currents = [None, None, None] if measured is None else [_rounded(measured.get(phase).value) for phase in PHASES]
    return [
        *currents,
        *controller_values(site.hass, charger_entry_id),
        *site.import_target_charger_row(charger_entry_id),
    ]


def controller_values(hass: HomeAssistant, charger_entry_id: str) -> list[Any]:
    """What the charger's controller holds (`CONTROLLER_COLUMNS`); all empty while the charger is not loaded."""
    from ..runtime import charger_data, domain_data

    data = charger_data(hass, charger_entry_id)
    if data is None:
        return [None] * len(CONTROLLER_COLUMNS)
    controller = data.controller
    control = hass.states.get(controller.charge_control)
    store = domain_data(hass).auto_store
    settings = None if store is None else store.settings(charger_entry_id)
    vehicle_id = None if settings is None else settings.target.vehicle_id
    # Only shown: never moves the anchor of the car the charger plans for (`SocReader.peek`).
    reading = data.soc_reader.peek(vehicle_id)
    solar = None if data.solar is None else data.solar.state
    switch = controller.adapter.phase_switch
    return [
        None if control is None else control.state,
        controller.charging,
        controller.connection()[0],
        controller.charge_progress_facts().connector_status,
        None if reading is None else _rounded(reading.soc_percent, 1),
        None if reading is None else reading.source,
        None if settings is None else settings.strategy,
        None if solar is None else solar.state,
        None if solar is None else solar.action,
        None if solar is None else solar.reason,
        None if switch is None else switch.phases_now(),
        controller.switched_phases,
        controller.adapter.last_current_a,
        controller.requested_current_a,
    ]


def charger_alone_values(hass: HomeAssistant, charger_entry_id: str) -> list[Any] | None:
    """A charger's row after its time while it is in no site (`CHARGER_ALONE_COLUMNS`), else `None`."""
    from ..planning.first_run import site_for_charger
    from ..runtime import controller_for

    if site_for_charger(hass, charger_entry_id) is not None:
        return None
    controller = controller_for(hass, charger_entry_id)
    if controller is None:
        return None
    return [_rounded(controller.measured_current_a()), *controller_values(hass, charger_entry_id)]
